"""Local LLM roles (HEAD / REVIEWER) served from this machine (e.g. Ollama / llama.cpp / vLLM on an RTX GPU).

Talks to an OpenAI-compatible endpoint on loopback only. No pay-per-use API and no credentials are involved,
so the "no PAYG API" rule still holds. Non-loopback hosts are refused.
"""
import json
import time
import urllib.request
from datetime import datetime
from urllib.parse import urlparse

from agents.claude_head import ClaudeHead
from schemas.provenance import AgentProvenanceRecord
from schemas.review_result import ReviewResultSchema

DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"   # Ollama default
DEFAULT_MODEL = "qwen2.5-coder:14b"               # ~9 GB at Q4: fits a 16 GB RTX 5080 with room for context
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def check_base_url(base_url: str) -> str:
    host = urlparse(base_url).hostname
    if host not in LOOPBACK:
        raise ValueError(f"local_llm.base_url must be loopback (127.0.0.1/localhost), got: {host}")
    return base_url.rstrip("/")


class LocalHead(ClaudeHead):
    agent_family = "local"
    usage_role = "HEAD"

    def __init__(self, base_url=DEFAULT_BASE_URL, model=DEFAULT_MODEL, timeout=300, no_progress_timeout=180,
                 temperature=0.1, num_ctx=16384, **kwargs):
        kwargs.pop("executable", None)
        super().__init__(executable=None, model=model, timeout=timeout,
                         no_progress_timeout=no_progress_timeout, **kwargs)
        self.executable = "local-llm"
        self.provider = "local"
        self.base_url = check_base_url(base_url)
        self.temperature = temperature
        self.num_ctx = num_ctx

    def get_version(self) -> str:
        return self.model

    def _invoke_cli(self, prompt, cwd=None):
        body = {"model": self.model, "temperature": self.temperature,
                "messages": [{"role": "system",
                              "content": "Do not use tools. Use supplied evidence only. Answer with JSON when asked."},
                             {"role": "user", "content": prompt}],
                "options": {"num_ctx": self.num_ctx}}
        req = urllib.request.Request(self.base_url + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        start = time.time()
        code = 0
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = data["choices"][0]["message"]["content"]
        except Exception as exc:   # surface as a provider failure; callers fail closed
            code = 1
            raise RuntimeError(f"LOCAL_LLM_ERROR: {exc}") from exc
        finally:
            self.last_execution_info = {"invocation_command": f"POST {self.base_url}/chat/completions model={self.model}",
                                        "exit_code": code, "execution_duration_sec": round(time.time() - start, 2)}
        return text

    def get_provenance(self, exit_code=0):
        info = self.last_execution_info
        return AgentProvenanceRecord(
            agent="REVIEWER" if isinstance(self, LocalReviewer) else "HEAD", provider="local",
            runtime="local-llm", model=self.model, executable=self.base_url,
            invocation_command=info.get("invocation_command", "POST /chat/completions"),
            version=self.model, exit_code=info.get("exit_code"), duration_sec=info.get("execution_duration_sec"),
            authentication_mode="local", api_key_used=False, timestamp=datetime.utcnow().isoformat())


class LocalReviewer(LocalHead):
    usage_role = "REVIEWER"

    def review(self, order, git_diff, verification, project_context=None, worktree_path=None):
        prompt = ("Independently review the ORDER against the code diff and test evidence. "
                  "Never claim tests were run by you. Identify concrete defects and requirement violations. "
                  "File-count and diff-size limits are enforced by the Director: never report them. "
                  'Return only JSON: {"verdict":"pass or changes_required","p0":[],"p1":[],"p2":[],"summary":"..."}.\n'
                  + json.dumps({"order": order.model_dump(), "diff": git_diff,
                                "verification": {k: v for k, v in verification.items() if k != "git_diff"}},
                               ensure_ascii=False))
        try:
            return ReviewResultSchema(**self._extract_json(self._invoke_cli(prompt)))
        except Exception as exc:   # reviewer is fail-closed
            msg = f"LOCAL_REVIEW_FAILED: {exc}"
            return ReviewResultSchema(verdict="changes_required", p0=[msg], p1=[], p2=[], summary=msg)

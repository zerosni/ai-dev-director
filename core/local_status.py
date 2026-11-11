"""Read-only status of the local LLM (provider 'local') for the dashboard. Loopback only, short timeout."""
import json
import urllib.request

from agents.local_roles import DEFAULT_BASE_URL, DEFAULT_MODEL, check_base_url


def local_status(config, timeout=2):
    cfg = config.get("local_llm", {}) or {}
    base = cfg.get("base_url", DEFAULT_BASE_URL)
    model = cfg.get("model", DEFAULT_MODEL)
    roles = [r.upper() for r in ("head", "builder", "reviewer")
             if str((config.get(r) or {}).get("provider", "")).lower() == "local"]
    out = {"model": model, "roles": roles, "online": False, "model_ready": False, "error": None}
    try:
        base = check_base_url(base)
        with urllib.request.urlopen(base + "/models", timeout=timeout) as resp:
            ids = [m.get("id") for m in json.loads(resp.read().decode("utf-8")).get("data", [])]
        out["online"] = True
        out["model_ready"] = model in ids
    except Exception as exc:
        out["error"] = type(exc).__name__
    return out

import os
import json
import shutil
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, Callable

from core.state_machine import StateMachine, State, TERMINAL_STATES
from core.git_manager import GitManager
from core.safety import SafetyPolicy, ScopeViolationError
from core.verifier import Verifier
from core.provider_manager import ProviderManager, ProviderStatus, FailoverMode, ErrorCategory
from core import autopilot
from agents.head import BaseHead
from agents.claude_head import ClaudeHead
from agents.codex_head import CodexHead
from agents.mock_head import MockHead
from agents.antigravity_builder import AntigravityBuilder
from agents.codex_reviewer import CodexReviewer
from agents.claude_reviewer import ClaudeReviewer
from schemas.order import OrderSchema
from schemas.builder_result import BuilderResultSchema
from schemas.review_result import ReviewResultSchema
from schemas.decision import DecisionSchema

class Orchestrator:
    """
    Central Controller for AI DEV DIRECTOR.
    Directly orchestrates execution between agents in a strict unidirectional pipeline.
    Agents never monitor inboxes or alter states independently.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        state_machine: Optional[StateMachine] = None,
        head_agent: Optional[BaseHead] = None,
        builder_agent: Optional[AntigravityBuilder] = None,
        reviewer_agent: Optional[Any] = None
    ):
        self.config = config
        self.sm = state_machine or StateMachine()
        self.timeouts = self.config.get("timeouts", {"head": 300, "builder": 600, "reviewer": 300})
        self.pm = ProviderManager(timeouts=self.timeouts)
        
        # User-supplied agents (e.g. for unit tests) or default initialization
        self.head = head_agent or self._init_head()
        self.builder = builder_agent or self._init_builder()
        self.reviewer = reviewer_agent or self._init_reviewer()

    def _local_kwargs(self) -> Dict[str, Any]:
        cfg = self.config.get("local_llm", {}) or {}
        return {k: cfg[k] for k in ("base_url", "model", "temperature", "num_ctx") if k in cfg}

    def _init_head(self, provider_override: Optional[str] = None) -> BaseHead:
        head_cfg = self.config.get("head", {})
        provider = (provider_override or head_cfg.get("provider", "openai")).lower()
        model = head_cfg.get("model", "cli-default")
        executable = head_cfg.get("executable")
        timeout = self.timeouts.get("head", 300)
        no_progress_timeout = self.timeouts.get("no_progress", 180)
        heartbeat_interval = self.timeouts.get("heartbeat", 30)

        if provider == 'local':
            from agents.local_roles import LocalHead
            return LocalHead(timeout=timeout, no_progress_timeout=no_progress_timeout, **self._local_kwargs())
        if provider in ('google', 'antigravity'):
            from agents.google_roles import GoogleHead
            return GoogleHead(executable=executable, timeout=timeout, no_progress_timeout=no_progress_timeout)
        if provider in ("claude", "anthropic"):
            return ClaudeHead(executable=executable, model="claude-code-cli", timeout=timeout, no_progress_timeout=no_progress_timeout, heartbeat_interval=heartbeat_interval)
        elif provider in ("codex", "openai"):
            return CodexHead(executable=executable, model=model, timeout=timeout, no_progress_timeout=no_progress_timeout, heartbeat_interval=heartbeat_interval)
        elif provider == "mock":
            return MockHead(model=model)
        else:
            raise ValueError(f"Unknown head provider: {provider}")

    def _init_builder(self, provider_override: Optional[str] = None) -> AntigravityBuilder:
        b_cfg = self.config.get("builder", {})
        model = b_cfg.get("model", "antigravity-cli")
        executable = b_cfg.get("executable")
        approved = self.config.get("safety", {}).get("approved_commands")
        timeout = self.timeouts.get("builder", 600)
        no_progress_timeout = self.timeouts.get("no_progress", 180)
        heartbeat_interval = self.timeouts.get("heartbeat", 30)
        cls = AntigravityBuilder
        if b_cfg.get('mode') == 'proposal':
            from agents.proposal_builder import ProposalBuilder
            return ProposalBuilder(provider=provider_override or b_cfg.get('provider', 'google'), approved_commands=approved,
                model=model, executable=executable, timeout=timeout, no_progress_timeout=no_progress_timeout,
                heartbeat_interval=heartbeat_interval)
        return cls(approved_commands=approved, model=model, executable=executable, timeout=timeout, no_progress_timeout=no_progress_timeout, heartbeat_interval=heartbeat_interval)

    def _init_reviewer(self, provider_override: Optional[str] = None) -> Any:
        rev_cfg = self.config.get("reviewer", {})
        provider = (provider_override or rev_cfg.get("provider", "codex")).lower()
        model = rev_cfg.get("model", "codex-cli")
        executable = rev_cfg.get("executable")
        timeout = self.timeouts.get("reviewer", 300)
        no_progress_timeout = self.timeouts.get("no_progress", 180)
        heartbeat_interval = self.timeouts.get("heartbeat", 30)

        if provider == 'local':
            from agents.local_roles import LocalReviewer
            return LocalReviewer(timeout=timeout, no_progress_timeout=no_progress_timeout, **self._local_kwargs())
        if provider in ('google', 'antigravity'):
            from agents.google_roles import GoogleReviewer
            return GoogleReviewer(executable=executable, timeout=timeout, no_progress_timeout=no_progress_timeout)
        if provider in ("claude", "anthropic"):
            c_model = "claude-code-cli" if model in ("codex-cli", "gpt-4o") else model
            return ClaudeReviewer(provider="anthropic", model=c_model, executable=executable, timeout=timeout, no_progress_timeout=no_progress_timeout, heartbeat_interval=heartbeat_interval)
        return CodexReviewer(provider="openai", model=model, executable=executable, timeout=timeout, no_progress_timeout=no_progress_timeout, heartbeat_interval=heartbeat_interval)

    def _get_run_dir(self, run_id: str) -> Path:
        base_dir = Path(__file__).resolve().parent.parent
        run_dir = base_dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "logs").mkdir(exist_ok=True)
        return run_dir

    def _save_json(self, run_id: str, filename: str, data: Any) -> None:
        run_dir = self._get_run_dir(run_id)
        filepath = run_dir / filename
        with open(filepath, "w", encoding="utf-8") as f:
            if hasattr(data, "model_dump"):
                json.dump(data.model_dump(), f, indent=2, ensure_ascii=False)
            else:
                json.dump(data, f, indent=2, ensure_ascii=False)

    def _load_json(self, run_id: str, filename: str) -> Optional[dict]:
        run_dir = self._get_run_dir(run_id)
        filepath = run_dir / filename
        if not filepath.exists():
            return None
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)

    def _read_project_context(self, project_path: str) -> Dict[str, Any]:
        proj = Path(project_path).resolve()
        files = []
        for root, dirs, filenames in os.walk(proj):
            # Ignore git and worktree dirs
            if ".git" in dirs:
                dirs.remove(".git")
            if ".director_worktrees" in dirs:
                dirs.remove(".director_worktrees")
            for fn in filenames:
                rel = Path(root, fn).relative_to(proj).as_posix()
                files.append(rel)
        return {"project_path": str(proj), "existing_files": files[:100]}

    def _get_agent_domain(self, agent: Any) -> str:
        return ProviderManager.domain_for_provider(getattr(agent, "provider", ""))

    def _record_provider_success(self, agent: Any) -> None:
        """A real CLI call by this agent finished with exit_code 0 -> provider verified (may promote RECOVERING/failed -> HEALTHY)."""
        domain = self._get_agent_domain(agent)
        if domain in ("MOCK", "UNKNOWN"):
            return
        info = getattr(agent, "last_execution_info", None) or {}
        if info.get("exit_code") == 0:
            try:
                self.pm.record_success(domain)
            except Exception:
                pass

    def _handle_provider_failure(
        self,
        domain: str,
        error_msg: str,
        run_id: str,
        stage: str,
        logger: logging.Logger,
        provenance_records: list
    ) -> Optional[Dict[str, Any]]:
        """
        Unified handler for provider failures (quota, auth, timeout, crash).
        Updates provider status, evaluates failover mode.
        If FailoverMode.STOP, halts run with State.HUMAN_REQUIRED.
        Returns a run summary if execution must stop, or None if failover is possible.
        """
        if domain in ("MOCK", "UNKNOWN"):
            return None

        cat = self.pm.classify_error(error_msg)
        if cat == ErrorCategory.CONFIG_ERROR:
            stop_msg = f"CONFIG_ERROR: {stage} halted due to configuration error: {error_msg[:200]}"
            logger.error(stop_msg)
            self.sm.log_audit(run_id, "ORCHESTRATOR", "CONFIG_ERROR_HALT", stop_msg)
            self._save_json(run_id, "provenance.json", {"run_id": run_id, "records": provenance_records})
            self.sm.transition_to(run_id, State.HUMAN_REQUIRED, details=stop_msg)
            return self._build_run_summary(run_id, scope_violations=[stop_msg])

        if cat in (ErrorCategory.EXECUTION_ERROR, ErrorCategory.UNKNOWN):
            logger.error(f"[{stage}] Non-provider {cat.value}: {error_msg[:200]}")
            return None

        # Parse CLI error and update domain status
        status = self.pm.parse_cli_error(domain, error_msg)
        logger.warning(f"[{stage}] Provider {domain} marked as {status.value} due to: {error_msg[:200]}")
        if self.config.get('manual_roles') and not autopilot.enabled(self.config):
            return self._stop(run_id, State.HUMAN_REQUIRED,
                f'MANUAL_ROLE_UNAVAILABLE: {domain} at {stage}: {error_msg[:200]}', logger, provenance_records)

        mode, down = self.pm.get_failover_mode()
        if mode == FailoverMode.STOP:
            down_str = ", ".join(down)
            stop_msg = f"PROVIDER_FAILURE: {stage} failed on {domain} ({status.value}). Multiple providers down ({down_str}). Insufficient healthy providers for autonomous run."
            logger.error(stop_msg)
            self.sm.log_audit(run_id, "ORCHESTRATOR", "PROVIDER_STOP", stop_msg)
            self._save_json(run_id, "provenance.json", {"run_id": run_id, "records": provenance_records})
            self.sm.transition_to(run_id, State.HUMAN_REQUIRED, details=stop_msg)
            return self._build_run_summary(run_id, scope_violations=[stop_msg])

        return None

    def generate_run_id(self) -> str:
        """RUN-<yymmddHHMMSS>-<uuid4 full hex (128-bit)>: unique within the same second."""
        import uuid
        timestamp = datetime.now().strftime("%y%m%d%H%M%S")
        return f"RUN-{timestamp}-{uuid.uuid4().hex.upper()}"

    def _create_run_with_retry(self, run_id: Optional[str], proj_path: str, goal: str, max_attempts: int = 5) -> str:
        """Inserts the run row; on a PRIMARY KEY collision of an auto-generated id, regenerates and retries."""
        import sqlite3
        explicit = run_id is not None
        rid = run_id or self.generate_run_id()
        reserved = getattr(self, "_reserved_run_ids", set())
        for attempt in range(max_attempts):
            try:
                if explicit and rid in reserved:
                    reserved.discard(rid)
                    return rid          # pre-reserved through reserve_run_id() on this same orchestrator
                self.sm.create_run(rid, proj_path, goal)
                return rid
            except sqlite3.IntegrityError:
                if explicit:
                    raise
                rid = self.generate_run_id()
        raise RuntimeError(f"Could not allocate a unique run_id after {max_attempts} attempts")

    def reserve_run_id(self, proj_path: str, goal: str) -> str:
        """Allocates a unique run id (IDLE row, collision-retried) that a later run(run_id=...) on this
        orchestrator will adopt instead of inserting again. Used by the dashboard; never by hand."""
        rid = self._create_run_with_retry(None, proj_path, goal)
        if not hasattr(self, "_reserved_run_ids"):
            self._reserved_run_ids = set()
        self._reserved_run_ids.add(rid)
        return rid

    def _claim_trial_for(self, agent: Any, run_id: str, stage: str, logger: logging.Logger) -> Optional[Dict[str, Any]]:
        """Claims a recovery trial for the provider this agent is about to call (only when it is RECOVERING).
        Returns a stop summary (HUMAN_REQUIRED) when the trial cannot be claimed (cooldown / other owner)."""
        domain = self._get_agent_domain(agent)
        if domain in ("MOCK", "UNKNOWN"):
            return None
        if self.pm.status_map.get(domain) != ProviderStatus.RECOVERING:
            return None
        if self.pm.claim_trial(domain):
            logger.warning(f"[Recovery Trial] {stage}: {domain} is RECOVERING; this call is its single verification attempt")
            self.sm.log_audit(run_id, "ORCHESTRATOR", "RECOVERY_TRIAL", f"{stage}: {domain} trial claimed")
            return None
        stop_msg = f"PROVIDER_TRIAL_UNAVAILABLE: {stage} needs {domain} which is RECOVERING but no trial is available (cooldown or another process owns the trial)."
        logger.error(stop_msg)
        self.sm.log_audit(run_id, "ORCHESTRATOR", "PROVIDER_STOP", stop_msg)
        self.sm.transition_to(run_id, State.HUMAN_REQUIRED, details=stop_msg)
        return self._build_run_summary(run_id, scope_violations=[stop_msg])

    # ==================================================================
    # HEAD proxy autonomy (autonomy.head_proxy): retry / replace / re-route instead of halting.
    # ==================================================================
    def _proxy(self) -> bool:
        return autopilot.enabled(self.config)

    def _provider_ready(self, agent: Any, run_id: str, stage: str, logger: logging.Logger) -> bool:
        """Non-halting counterpart of _claim_trial_for: a RECOVERING provider is usable only if its single trial
        can be claimed now; otherwise the caller picks another provider."""
        domain = self._get_agent_domain(agent)
        if domain in ("MOCK", "UNKNOWN") or self.pm.status_map.get(domain) != ProviderStatus.RECOVERING:
            return True
        if self.pm.claim_trial(domain):
            self.sm.log_audit(run_id, "ORCHESTRATOR", "RECOVERY_TRIAL", f"{stage}: {domain} trial claimed")
            return True
        logger.warning(f"[{stage}] {domain} is RECOVERING without an available trial; choosing another provider")
        return False

    def _domain_of(self, alias: str) -> str:
        return ProviderManager.domain_for_provider(alias)

    def _autonomous_role_assignment(self, req_head: str, req_builder: str, req_reviewer: str,
                                    run_id: str, logger: logging.Logger) -> Dict[str, Any]:
        """Keeps the requested roles where their CLI is usable; replaces an unusable one with another usable
        provider (preferring one not already holding a role). Never silent: every swap is audited."""
        usable = set(self.pm.usable_domains())
        roles = {"head": req_head, "builder": req_builder, "reviewer": req_reviewer}
        notes = []
        for role in ("head", "reviewer", "builder"):
            dom = self._domain_of(roles[role])
            if dom in usable or dom in ("MOCK", "UNKNOWN"):
                continue
            if role == "builder" and self.config.get("builder", {}).get("mode") != "proposal":
                alt = None
            else:
                others = {self._domain_of(roles[r]) for r in roles if r != role}
                alt = autopilot.pick_alternative(self.pm, exclude={dom}, avoid=others)
            if alt is None:
                return {"mode": FailoverMode.STOP, "down_providers": sorted(set(autopilot.DOMAIN_PREFERENCE) - usable),
                        "head_provider": req_head, "builder_provider": req_builder, "reviewer_provider": req_reviewer,
                        "can_proceed": False,
                        "reason": f"No usable provider for {role.upper()} ({dom} unavailable; usable: {sorted(usable)})"}
            notes.append(f"{role.upper()}: {dom} -> {self._domain_of(alt)}")
            roles[role] = alt
        if notes:
            msg = "HEAD proxy role failover (visible, not silent): " + "; ".join(notes)
            logger.warning(msg)
            self.sm.log_audit(run_id, "ORCHESTRATOR", "FAILOVER", msg)
        try:   # dashboard role bookkeeping only
            self.pm.resolve_roles(roles["head"], roles["builder"], roles["reviewer"], strict=True)
        except Exception:
            pass
        return {"mode": FailoverMode.DEGRADED if notes else FailoverMode.NORMAL,
                "down_providers": sorted(set(autopilot.DOMAIN_PREFERENCE) - usable),
                "head_provider": roles["head"], "builder_provider": roles["builder"],
                "reviewer_provider": roles["reviewer"], "can_proceed": True,
                "reason": "; ".join(notes) or "All selected providers usable."}

    def _swap_agent(self, role: str, run_id: str, logger: logging.Logger, tried: set, stage: str, why: str) -> bool:
        """Replace HEAD/BUILDER/REVIEWER with another usable provider. Returns False when none is left."""
        others = {self._get_agent_domain(a) for r, a in (("head", self.head), ("builder", self.builder),
                                                           ("reviewer", self.reviewer)) if r != role}
        if role == "builder" and self.config.get("builder", {}).get("mode") != "proposal":
            return False
        alt = autopilot.pick_alternative(self.pm, exclude=set(tried), avoid=others)
        if alt is None:
            return False
        old = self._get_agent_domain(getattr(self, role))
        setattr(self, role, {"head": self._init_head, "builder": self._init_builder, "reviewer": self._init_reviewer}[role](alt))
        if role == "head":
            self.current_head_name = self._domain_of(alt)
        if role == "reviewer":
            self.current_reviewer_name = self._domain_of(alt)
        tried.add(self._domain_of(alt))
        msg = f"{stage}: {role.upper()} {old} -> {self._domain_of(alt)} ({why[:200]})"
        logger.warning(f"[FAILOVER] {msg}")
        self.sm.log_audit(run_id, "ORCHESTRATOR", f"FAILOVER_{role.upper()}", msg)
        return True

    def apply_safety_caps(self, order: OrderSchema) -> list:
        """Clamps ORDER limits to the global config safety caps: effective = min(order, global).
        Returns a list of human-readable adjustments (empty when nothing changed)."""
        caps = (self.config.get("safety") or {})
        adjustments = []
        for field in ("max_files_changed", "max_diff_lines"):
            cap = caps.get(field)
            if cap is None:
                continue
            try:
                cap = int(cap)
            except (TypeError, ValueError):
                continue
            current = getattr(order, field, None)
            if current is None or current > cap:
                adjustments.append(f"{field}: {current} -> {cap} (global safety cap)")
                setattr(order, field, cap)
        return adjustments

    def _setup_logger(self, run_id: str) -> logging.Logger:
        run_dir = self._get_run_dir(run_id)
        log_file = run_dir / "logs" / "run.log"
        logger = logging.getLogger(f"director.{run_id}")
        logger.setLevel(logging.INFO)
        # Avoid duplicate handlers on resume
        if not any(isinstance(h, logging.FileHandler) for h in logger.handlers):
            fh = logging.FileHandler(str(log_file), encoding="utf-8")
            formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
            fh.setFormatter(formatter)
            logger.addHandler(fh)
        return logger


    # ==================================================================
    # Shared safety pipeline (used by BOTH run() and resume() so parity cannot drift)
    # ==================================================================
    def _stop(self, run_id: str, state: str, details: str, logger: logging.Logger,
              provenance_records: Optional[list] = None, audit_action: Optional[str] = None,
              violations: Optional[list] = None) -> Dict[str, Any]:
        if audit_action:
            self.sm.log_audit(run_id, "ORCHESTRATOR", audit_action, details)
        if provenance_records is not None:
            self._save_json(run_id, "provenance.json", {"run_id": run_id, "records": provenance_records})
        (logger.error if state != State.PASS else logger.info)(details)
        self.sm.transition_to(run_id, state, details=details)
        return self._build_run_summary(run_id, scope_violations=violations if violations is not None else [details])

    def _verification_cmds(self):
        v = self.config.get("verification", {}) or {}
        return v.get("build_command"), v.get("test_command"), v.get("lint_command")

    def prepare_order_for_execution(self, run_id: str, order: OrderSchema, logger: logging.Logger,
                                    provenance_records: list, worktree_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Global safety caps (min(order, global)) + fail-closed path validation. With a worktree the paths must
        also resolve canonically inside it (symlink/junction escapes). Returns a stop summary or None."""
        if self._proxy():
            fixed, notes = autopilot.sanitize_order(order, (self.sm.get_run(run_id) or {}).get("project_path"))
            if notes:
                for field in ("allowed_paths", "protected_paths", "allowed_deletions", "required_outputs",
                              "reference_paths", "acceptance_criteria"):
                    setattr(order, field, getattr(fixed, field))
                logger.info("[HEAD proxy] ORDER auto-repair: " + "; ".join(notes))
                self.sm.log_audit(run_id, "ORCHESTRATOR", "ORDER_AUTOREPAIR", "; ".join(notes))
        cap_notes = self.apply_safety_caps(order)
        if cap_notes:
            logger.warning(f"[Safety Cap] {'; '.join(cap_notes)}")
            self.sm.log_audit(run_id, "ORCHESTRATOR", "SAFETY_CAP_APPLIED", "; ".join(cap_notes))
        self._save_json(run_id, "order.json", order)
        violations = SafetyPolicy.validate_order_paths(order, worktree_path=worktree_path)
        if violations:
            msg = "ORDER_PATH_VIOLATION: " + "; ".join(violations)
            return self._stop(run_id, State.HUMAN_REQUIRED, msg, logger, provenance_records, "ORDER_PATH_VIOLATION", violations)
        return None

    def baseline_evidence(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Explicit baseline evidence: the persisted baseline_verification.json with a `passed` flag and a
        `test_counts` dict. Anything else (missing file, malformed artifact) is MISSING, never "0 tests"."""
        data = self._load_json(run_id, "baseline_verification.json")
        if not isinstance(data, dict) or "passed" not in data or not isinstance(data.get("test_counts"), dict):
            return None
        return data

    def _worktree_is_pristine(self, worktree_path: Optional[str]) -> bool:
        """True only when the worktree exists and git reports no modifications (proof that the builder has not run)."""
        try:
            if not worktree_path or not Path(worktree_path).exists():
                return False
            return GitManager.get_status(worktree_path).strip() == ""
        except Exception:
            return False

    def run_baseline_if_required(self, run_id: str, worktree_path: str, logger: logging.Logger,
                                 provenance_records: list, force: bool = False, allow_measure: bool = True):
        """Baseline verification on the clean worktree BEFORE the builder. Reuses persisted evidence unless force.
        Measuring is only allowed while the worktree is provably untouched (allow_measure and git status clean);
        a worktree the builder may already have modified is never turned into a baseline.
        A failing or missing baseline never lets the builder run. Returns (baseline_pass_count, stop)."""
        baseline = None if force else self.baseline_evidence(run_id)
        if baseline is None:
            if not allow_measure:
                msg = "BASELINE_MISSING: no persisted baseline evidence for this run and the worktree may already be modified; a baseline cannot be measured now."
                return None, self._stop(run_id, State.HUMAN_REQUIRED, msg, logger, provenance_records, "BASELINE_MISSING")
            if not self._worktree_is_pristine(worktree_path):
                msg = "BASELINE_MISSING: worktree is not provably untouched (git status not clean); refusing to measure a baseline on possibly modified files."
                return None, self._stop(run_id, State.HUMAN_REQUIRED, msg, logger, provenance_records, "BASELINE_MISSING")
            build_cmd, test_cmd, lint_cmd = self._verification_cmds()
            logger.info("[Baseline Gate] Running tests on clean worktree before builder execution...")
            if self.config.get('bootstrap', {}).get('enabled') and (test_cmd == 'python -m pytest tests/' or isinstance(test_cmd, list) and test_cmd[1:] == ['-m', 'pytest', 'tests/']):
                from core.bootstrap import bootstrap_tests
                created = bootstrap_tests(worktree_path)
                if created:
                    self._save_json(run_id, 'bootstrap.json', {'test_path': created, 'coverage': 'syntax_only_not_product_acceptance'})
                    self.sm.log_audit(run_id, 'ORCHESTRATOR', 'BOOTSTRAP_TESTS', created)
            baseline = self._verify(run_id, worktree_path)
            self._save_json(run_id, "baseline_verification.json", baseline)
            self.sm.log_audit(run_id, "ORCHESTRATOR", "BASELINE_MEASURED", json.dumps(baseline.get("test_counts") or {}))
        if not baseline.get("passed", False):
            if self._proxy():
                # Pre-existing failures are recorded, not fatal: post-build must not add NEW failures and must not
                # lose passing tests (validate_post_build / final_evidence enforce that against this baseline).
                failing = sorted(autopilot.failing_tests(baseline))
                msg = f"BASELINE_PREEXISTING_FAILURES: {baseline.get('summary')}; failing: {', '.join(failing[:20]) or 'unparsed'}"
                logger.warning("[HEAD proxy] " + msg)
                self.sm.log_audit(run_id, "ORCHESTRATOR", "BASELINE_PREEXISTING_FAILURES", msg)
                return int((baseline.get("test_counts") or {}).get("passed", 0) or 0), None
            msg = f"BASELINE_FAILED: baseline verification failed on the untouched worktree ({baseline.get('summary')}). Builder not executed."
            return None, self._stop(run_id, State.HUMAN_REQUIRED, msg, logger, provenance_records, "BASELINE_FAILED")
        count = int((baseline.get("test_counts") or {}).get("passed", 0) or 0)
        logger.info(f"[Baseline Gate] Baseline tests passed: {count}")
        return count, None

    def run_builder_stage(self, run_id: str, order: OrderSchema, worktree_path: str, builder_fn, logger: logging.Logger,
                          provenance_records: list):
        """Trial claim (real builder only), builder execution, provenance, BUSY/PERMISSION/provider failure handling.
        Returns (builder_result, stop)."""
        bootstrap = self._load_json(run_id, 'bootstrap.json') or {}
        if bootstrap.get('test_path'):
            if bootstrap['test_path'] not in order.protected_paths:
                order.protected_paths.append(bootstrap['test_path'])
            smoke_note = 'Add real product behavior tests; the initial syntax smoke test alone is not acceptance.'
            if smoke_note not in order.acceptance_criteria:
                order.acceptance_criteria.append(smoke_note)
            self._save_json(run_id, 'order.json', order)
        base = self.baseline_evidence(run_id) if self._proxy() else None
        if base is not None and not base.get("passed", False):
            failing = sorted(autopilot.failing_tests(base))
            note = ("Pre-existing failing tests before this change: " + (", ".join(failing[:15]) or base.get("summary", ""))
                    + ". Fix them when the cause is inside allowed_paths; never make any other test fail.")
            if note not in order.acceptance_criteria:
                order.acceptance_criteria.append(note)
                self._save_json(run_id, 'order.json', order)
        if builder_fn is None and self._proxy():
            return self._run_builder_autonomous(run_id, order, worktree_path, logger, provenance_records)
        if builder_fn is None:
            stop_trial = self._claim_trial_for(self.builder, run_id, "BUILDING", logger)
            if stop_trial:
                return None, stop_trial
        try:
            builder_result = self.builder.run(order, worktree_path, builder_fn=builder_fn)
        except Exception as e:
            builder_result = BuilderResultSchema(status="FAILED", files_modified=[], commands_run=[],
                                                 summary="Builder raised an exception", error=f"BUILDER_EXCEPTION: {str(e)[:300]}")
        self._save_json(run_id, "builder.json", builder_result)
        cli_evidence = getattr(self.builder, 'last_execution_info', None)
        if cli_evidence and builder_fn is None:
            self._save_json(run_id, 'builder_cli.json', cli_evidence)
        self.sm.log_audit(run_id, getattr(self.builder, 'provider', 'google').upper(), "BUILDER_EXECUTION", json.dumps(builder_result.model_dump()))

        if builder_fn is not None:
            provenance_records.append({
                "agent": "BUILDER", "provider": "custom", "runtime": "builder_fn", "model": "builder_fn",
                "executable": "none", "invocation_command": "builder_fn", "version": "n/a",
                "exit_code": 0 if builder_result.status == "COMPLETED" else 1, "duration_sec": 0.0,
                "authentication_mode": "mock", "api_key_policy": "disabled", "api_key_used": False,
                "observed_fields": [], "declared_fields": ["authentication_mode", "api_key_policy", "api_key_used"],
                "timestamp": datetime.utcnow().isoformat()
            })
        else:
            b_exit_code = 0 if builder_result.status == "COMPLETED" else 1
            if hasattr(self.builder, "get_provenance"):
                prov = self.builder.get_provenance(exit_code=b_exit_code)
                provenance_records.append(prov.model_dump())
                if getattr(prov, "duration_sec", None) is not None:
                    self.pm.update_latency(getattr(self.builder, "provider", "GOOGLE"), prov.duration_sec)
            if builder_result.status == "COMPLETED":
                self._record_provider_success(self.builder)

        err = str(builder_result.error or "")
        if builder_result.status != "COMPLETED":
            if err.startswith("PERMISSION_"):
                action = "PERMISSION_RESTORE_FAILED" if err.startswith("PERMISSION_RESTORE_FAILED") else "PERMISSION_SETUP_FAILED"
                return builder_result, self._stop(run_id, State.HUMAN_REQUIRED, err, logger, provenance_records, action)
            if err.startswith("BUILDER_BUSY"):
                return builder_result, self._stop(run_id, State.HUMAN_REQUIRED, f"BUILDER_BUSY: {err}", logger, provenance_records, "BUILDER_BUSY")
            logger.warning(f"Builder failed: {err}")
            stop_summary = self._handle_provider_failure(getattr(self.builder, 'provider', 'google').upper(), err or "Builder failed", run_id, "BUILDING", logger, provenance_records)
            if stop_summary:
                return builder_result, stop_summary
            state = State.TIMEOUT if "TIMEOUT" in err else State.FAIL
            return builder_result, self._stop(run_id, state, f"Builder {'timeout' if state == State.TIMEOUT else 'failed'}: {err}", logger, provenance_records)
        return builder_result, None

    PROVIDER_DOWN = (ErrorCategory.QUOTA, ErrorCategory.AUTH, ErrorCategory.PROVIDER_UNAVAILABLE, ErrorCategory.CONFIG_ERROR)

    def _run_builder_autonomous(self, run_id: str, order: OrderSchema, worktree_path: str, logger: logging.Logger,
                                provenance_records: list):
        """HEAD proxy Builder stage: a malformed/empty/truncated proposal or a timeout is retried with concrete
        feedback; an unavailable provider (or the final attempt) moves the Builder to another provider.
        The proposal is still validated as one batch before any write, so a failed attempt leaves no files."""
        attempts = max(1, int(autopilot.setting(self.config, "builder_attempts", 3)))
        # Attempt history is cumulative over the run's correction rounds, so a provider that already failed twice in
        # this run is not rotated back in while another provider is still untried.
        history = list(self._load_json(run_id, "builder_attempts.json") or [])
        failures = {}
        for h in history:
            if h.get("category") != "ORDER_INPUT":
                failures[h.get("provider")] = failures.get(h.get("provider"), 0) + 1
        tried = {self._get_agent_domain(self.builder)} | {d for d, n in failures.items() if n >= 2}
        if failures.get(self._get_agent_domain(self.builder), 0) >= 2:
            self._swap_agent("builder", run_id, logger, tried, "BUILDING", "failed repeatedly earlier in this run")
        round_start = len(history)
        builder_result = None
        for attempt in range(1, attempts + 1):
            attempt_order = order
            if len(history) > round_start:
                attempt_order = order.model_copy(deep=True)
                attempt_order.context = (order.context or "") + "\n" + autopilot.builder_retry_feedback(history[-1]["error"], order)
            domain = self._get_agent_domain(self.builder)
            if not self._provider_ready(self.builder, run_id, "BUILDING", logger):
                builder_result = BuilderResultSchema(status="FAILED", files_modified=[], commands_run=[],
                                                     summary="Provider not ready", error=f"PROVIDER_UNAVAILABLE: {domain} recovering, no trial")
            else:
                try:
                    builder_result = self.builder.run(attempt_order, worktree_path, builder_fn=None)
                except Exception as e:
                    builder_result = BuilderResultSchema(status="FAILED", files_modified=[], commands_run=[],
                                                         summary="Builder raised an exception", error=f"BUILDER_EXCEPTION: {str(e)[:300]}")
                cli_evidence = getattr(self.builder, 'last_execution_info', None)
                if cli_evidence:
                    self._save_json(run_id, 'builder_cli.json', cli_evidence)
                    every = list(self._load_json(run_id, 'builder_cli_attempts.json') or [])
                    every.append({"provider": domain, "status": builder_result.status, "error": builder_result.error,
                                  **{k: v for k, v in cli_evidence.items() if k != "attempts"},
                                  "output": str(cli_evidence.get("output") or "")[:4000]})
                    self._save_json(run_id, 'builder_cli_attempts.json', every)
                if hasattr(self.builder, "get_provenance"):
                    prov = self.builder.get_provenance(exit_code=0 if builder_result.status == "COMPLETED" else 1)
                    provenance_records.append(prov.model_dump())
                    if getattr(prov, "duration_sec", None) is not None:
                        self.pm.update_latency(getattr(self.builder, "provider", "GOOGLE"), prov.duration_sec)
            if builder_result.status == "COMPLETED":
                self._record_provider_success(self.builder)
                break
            err = str(builder_result.error or "")
            if err.startswith("PERMISSION_") or err.startswith("BUILDER_BUSY"):
                break
            if "REFERENCE_INPUT_UNAVAILABLE" in err:
                # a defect of the ORDER itself: no retry or provider can fix it; fail fast so HEAD re-plans
                history.append({"attempt": attempt, "provider": domain, "category": "ORDER_INPUT", "error": err[:800]})
                self._save_json(run_id, "builder_attempts.json", history)
                break
            cat = self.pm.classify_error(err)
            history.append({"attempt": attempt, "provider": domain, "category": cat.value, "error": err[:800]})
            self._save_json(run_id, "builder_attempts.json", history)
            self.sm.log_audit(run_id, "ORCHESTRATOR", "BUILDER_RETRY", json.dumps(history[-1], ensure_ascii=False))
            logger.warning(f"[HEAD proxy] Builder attempt {attempt}/{attempts} on {domain} failed ({cat.value}): {err[:200]}")
            if attempt == attempts:
                break
            if cat in self.PROVIDER_DOWN:
                try:
                    self.pm.parse_cli_error(domain, err)
                except Exception:
                    pass
            if cat in self.PROVIDER_DOWN or attempt == attempts - 1 or "PROVIDER_UNAVAILABLE" in err:
                if not self._swap_agent("builder", run_id, logger, tried, "BUILDING", err) and cat in self.PROVIDER_DOWN:
                    break

        self._save_json(run_id, "builder.json", builder_result)
        self.sm.log_audit(run_id, getattr(self.builder, 'provider', 'google').upper(), "BUILDER_EXECUTION", json.dumps(builder_result.model_dump()))
        if builder_result.status == "COMPLETED":
            return builder_result, None
        err = str(builder_result.error or "")
        if err.startswith("PERMISSION_"):
            action = "PERMISSION_RESTORE_FAILED" if err.startswith("PERMISSION_RESTORE_FAILED") else "PERMISSION_SETUP_FAILED"
            return builder_result, self._stop(run_id, State.HUMAN_REQUIRED, err, logger, provenance_records, action)
        if err.startswith("BUILDER_BUSY"):
            return builder_result, self._stop(run_id, State.HUMAN_REQUIRED, f"BUILDER_BUSY: {err}", logger, provenance_records, "BUILDER_BUSY")
        state = State.TIMEOUT if "TIMEOUT" in err else State.FAIL
        return builder_result, self._stop(run_id, state, f"Builder failed after {len(history) - round_start} attempt(s) across "
                                          f"{', '.join(sorted(tried))}: {err}", logger, provenance_records)

    @staticmethod
    def _dirty_state(worktree_path: str) -> Dict[str, Any]:
        """Every path that differs from HEAD (untracked, intent-to-add, modified, deleted) -> (mtime, size)."""
        wt = Path(worktree_path).resolve()
        try:
            out = GitManager._run_git(["-c", "core.quotepath=false", "status", "--porcelain", "-uall"], wt)
        except Exception:
            return {}
        state = {}
        for line in out.splitlines():
            rel = line[3:].strip().strip('"')
            if " -> " in rel:
                rel = rel.split(" -> ", 1)[1]
            rel = rel.replace("\\", "/")
            try:
                st = (wt / rel).stat()
                state[rel] = [st.st_mtime_ns, st.st_size]
            except OSError:
                state[rel] = None
        return state

    def _verify(self, run_id: str, worktree_path: str) -> Dict[str, Any]:
        """Director verification. In HEAD proxy mode, files the verification run itself created or changed (a CLI
        under test writing its data file, report output, ...) are recorded as verification artifacts: they are the
        product's runtime output, not Builder changes, and must not later read as out-of-scope Builder edits."""
        build_cmd, test_cmd, lint_cmd = self._verification_cmds()
        before = self._dirty_state(worktree_path) if self._proxy() else None
        result = Verifier.verify(worktree_path=worktree_path, build_cmd=build_cmd, test_cmd=test_cmd, lint_cmd=lint_cmd)
        if before is not None:
            after = self._dirty_state(worktree_path)
            changed = {p for p, sig in after.items() if before.get(p, "absent") != sig and not SafetyPolicy.is_runtime_artifact(p)}
            # Files the test run CREATED are moved out of the worktree into the run record: otherwise the next
            # review/diff/snapshot presents them as the Builder's deliverable (a Builder cannot remove them, so the
            # review never converges). Files that already existed and were only modified stay, recorded as artifacts.
            created = sorted(p for p in changed if p not in before and after.get(p) is not None
                             and not SafetyPolicy.is_control_plane_path(p))
            wt = Path(worktree_path).resolve()
            out_dir = self._get_run_dir(run_id) / "verification_output"
            moved = []
            for rel in created:
                src = wt / rel
                try:
                    if not src.is_file() or src.is_symlink() or not SafetyPolicy.is_within_root(wt, src):
                        continue
                    dest = out_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dest))
                    moved.append(rel)
                except OSError:
                    continue
            if moved:
                self.sm.log_audit(run_id, "ORCHESTRATOR", "VERIFICATION_OUTPUT_MOVED",
                                  f"{', '.join(moved)[:900]} -> {out_dir}")
            remaining = changed - set(moved)
            if remaining:
                known = set(self._load_json(run_id, "verification_artifacts.json") or [])
                self._save_json(run_id, "verification_artifacts.json", sorted(known | remaining))
                self.sm.log_audit(run_id, "ORCHESTRATOR", "VERIFICATION_ARTIFACTS", ", ".join(sorted(remaining))[:1000])
        return result

    @staticmethod
    def _control_plane_fs_hits(worktree_path: str) -> list:
        """Files under <worktree>/.director_worktrees/** (excluded from git via info/exclude, so the git-based
        Scope Guard cannot see them): their mere existence inside a worktree is a control-plane violation."""
        hits = []
        try:
            base = Path(worktree_path) / ".director_worktrees"
            if base.exists():
                for p in sorted(x for x in base.rglob("*") if x.is_file()):
                    hits.append(p.relative_to(Path(worktree_path)).as_posix())
                if not hits:
                    hits.append(".director_worktrees/")
        except Exception:
            hits.append(".director_worktrees/ (unreadable)")
        return hits

    def validate_post_build(self, run_id: str, order: OrderSchema, worktree_path: str, baseline_pass_count: int,
                            logger: logging.Logger, provenance_records: list):
        """Post-build Scope Guard (canonical containment in the worktree) -> verification -> baseline regression gate.
        Returns (verification, stop)."""
        diff = GitManager.get_diff(worktree_path)
        numstat = GitManager.get_numstat(worktree_path)
        untracked = GitManager.get_untracked_files(worktree_path)
        deleted_files = GitManager.get_deleted_files(worktree_path)
        artifacts = set(self._load_json(run_id, "verification_artifacts.json") or []) if self._proxy() else set()
        if artifacts:
            def builder_change(p):
                p = str(p).replace("\\", "/")
                return (p not in artifacts or SafetyPolicy.is_control_plane_path(p)
                        or SafetyPolicy.is_path_protected(p, order.protected_paths))
            numstat = [n for n in numstat if builder_change(n[2])]
            untracked = [u for u in untracked if builder_change(u)]
            deleted_files = [d for d in (deleted_files or []) if builder_change(d)]
        approvals = self._load_json(run_id, "approvals.json") or {}
        waivers = list(approvals.get("post_build") or [])          # explicit human waivers: gate + file only
        fs_hits = self._control_plane_fs_hits(worktree_path)      # git-invisible control-plane files (info/exclude)
        scope_ok, scope_violations = SafetyPolicy.validate_scope_post_build(
            order=order, numstat=numstat, untracked_files=untracked, raw_git_diff=diff,
            deleted_files=deleted_files, worktree_path=worktree_path, approved_waivers=waivers)
        if fs_hits:
            scope_violations = list(scope_violations) + [f"Control plane modified: {h}" for h in fs_hits]
            scope_ok = False
        if waivers:
            self.sm.log_audit(run_id, "ORCHESTRATOR", "SCOPE_WAIVER_APPLIED", json.dumps({"waivers": waivers, "remaining_violations": scope_violations}, ensure_ascii=False))
        repairable = []
        if not scope_ok and self._proxy():
            triage = autopilot.split_post_build(scope_violations)
            if not triage["serious"]:
                if triage["waivable"]:
                    self.sm.log_audit(run_id, "HEAD", "HEAD_PROXY_WAIVER", json.dumps(triage["waivable"], ensure_ascii=False))
                    logger.info("[HEAD proxy] waived on the user's behalf: " + "; ".join(triage["waivable"]))
                repairable = triage["repairable"]
                scope_ok = True
                scope_violations = []
        if not scope_ok:
            msg = "Scope violation: " + "; ".join(scope_violations)
            logger.warning(f"Scope Guard detected violations: {msg}")
            return None, self._stop(run_id, State.HUMAN_REQUIRED, msg, logger, provenance_records, "SCOPE_VIOLATION", scope_violations)

        # Code that could move real money is stopped BEFORE any test executes it (all modes). Human-approvable per file.
        new_files = {}
        for rel in untracked:
            try:
                p = Path(worktree_path) / rel
                if p.is_file() and p.stat().st_size < 500_000:
                    new_files[rel] = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        money, _ = SafetyPolicy.apply_waivers(autopilot.money_code_findings(diff, new_files), waivers)
        if money:
            msg = "Scope violation: " + "; ".join(money)
            logger.warning(f"Money-moving code detected before test execution: {msg}")
            return None, self._stop(run_id, State.HUMAN_REQUIRED, msg, logger, provenance_records, "LIVE_MONEY_CODE", money)

        verification = self._verify(run_id, worktree_path)
        if repairable:
            verification['passed'] = False
            verification['scope_repairs'] = repairable
            verification['summary'] += '; SCOPE_REPAIR_REQUIRED (restore or justify): ' + '; '.join(repairable)
        missing = []
        for rel in dict.fromkeys(p for p in (autopilot.required_output_path(r) for r in order.required_outputs) if p):
            target = (Path(worktree_path) / rel).resolve()
            if not target.is_relative_to(Path(worktree_path).resolve()) or not target.is_file() or target.stat().st_size == 0:
                missing.append(rel)
        verification['required_outputs_missing'] = missing
        if missing:
            verification['passed'] = False
            verification['summary'] += '; REQUIRED_OUTPUTS_MISSING: ' + ', '.join(missing)
        # Tolerating pre-existing failures is for the user's own legacy tests. When this run builds on work the
        # Director produced (an earlier milestone or a failed attempt), those failures are ours and must be fixed.
        builds_on_director_work = bool((self._load_json(run_id, "source_snapshot.json") or {}).get("base_worktree")
                                       or (self._load_json(run_id, "mission_base.json") or {}).get("base_worktree"))
        if self._proxy() and not verification['passed'] and not repairable and not builds_on_director_work:
            unchanged = autopilot.only_preexisting_failures(self.baseline_evidence(run_id), verification)
            if unchanged:
                verification['passed'] = True
                verification['preexisting_failures'] = sorted(unchanged)
                verification['summary'] += '; PRE-EXISTING FAILURES UNCHANGED (no new failures): ' + ', '.join(sorted(unchanged)[:10])
        self._save_json(run_id, "verification.json", verification)
        self.sm.log_audit(run_id, "ORCHESTRATOR", "VERIFICATION", json.dumps(verification))
        if not verification["passed"]:
            if self.config.get('autonomy', {}).get('enabled'):
                return verification, None
            self._save_json(run_id, "provenance.json", {"run_id": run_id, "records": provenance_records})
            logger.warning(f"Verification failed: {verification['summary']}")
            self.sm.transition_to(run_id, State.FAIL, details=f"Verification failed: {verification['summary']}")
            return verification, self._build_run_summary(run_id, verification=verification)

        post_pass_count = int((verification.get("test_counts") or {}).get("passed", 0) or 0)
        if self._load_json(run_id, 'bootstrap.json') and post_pass_count <= (baseline_pass_count or 0):
            return verification, self._stop(run_id, State.FAIL, 'PRODUCT_TESTS_MISSING: bootstrap smoke test alone is not acceptance', logger, provenance_records)
        if baseline_pass_count is None:
            msg = "BASELINE_MISSING: no baseline verification result exists for this run; regression cannot be assessed."
            return verification, self._stop(run_id, State.HUMAN_REQUIRED, msg, logger, provenance_records, "BASELINE_MISSING")
        if baseline_pass_count > 0 and post_pass_count < baseline_pass_count:
            msg = f"Baseline test count decreased from {baseline_pass_count} to {post_pass_count}!"
            self.sm.log_audit(run_id, "ORCHESTRATOR", "BASELINE_REGRESSION", msg)
            self._save_json(run_id, "provenance.json", {"run_id": run_id, "records": provenance_records})
            logger.warning(msg)
            self.sm.transition_to(run_id, State.HUMAN_REQUIRED, details=msg)
            return verification, self._build_run_summary(run_id, verification=verification, scope_violations=[msg])
        return verification, None

    def _reviewer_provenance(self, exit_code: int, provenance_records: list) -> None:
        if hasattr(self.reviewer, "get_provenance"):
            prov = self.reviewer.get_provenance(exit_code=exit_code)
            provenance_records.append(prov.model_dump())
            if getattr(prov, "duration_sec", None) is not None:
                self.pm.update_latency(getattr(self.reviewer, "provider", "ANTHROPIC"), prov.duration_sec)

    def run_reviewer_with_failover(self, run_id: str, order: OrderSchema, verification: Dict[str, Any],
                                   project_context: Dict[str, Any], worktree_path: str, logger: logging.Logger,
                                   provenance_records: list):
        """Trial claim -> review (exceptions recorded, never left uncaught) -> unparseable/CLI-failure handling ->
        provider failover -> provenance. Returns (review_result, stop)."""
        if self._proxy():
            return self._run_reviewer_autonomous(run_id, order, verification, project_context, worktree_path, logger, provenance_records)
        git_diff = verification.get("git_diff", "") if verification else ""
        stop_trial = self._claim_trial_for(self.reviewer, run_id, "REVIEWING", logger)
        if stop_trial:
            return None, stop_trial

        rev_exception = None
        try:
            review_result = self.reviewer.review(order, git_diff, verification, project_context, worktree_path=worktree_path)
        except Exception as e:
            rev_exception = str(e)
            logger.error(f"Reviewer raised an exception: {rev_exception}")
            self.sm.log_audit(run_id, getattr(self, "current_reviewer_name", "CODEX"), "REVIEWER_EXCEPTION", rev_exception[:500])
            review_result = ReviewResultSchema(verdict="changes_required", p0=[f"REVIEWER_EXCEPTION: {rev_exception[:300]}"], p1=[], p2=[],
                                               summary=f"REVIEWER_EXCEPTION: {rev_exception[:300]}")
            self._reviewer_provenance(1, provenance_records)

        rev_domain = self._get_agent_domain(self.reviewer)
        rev_info = getattr(self.reviewer, "last_execution_info", {}) or {}
        rev_exit = rev_info.get("exit_code", 0)
        if rev_exception is not None:
            rev_exit = rev_info.get("exit_code") if rev_info.get("exit_code") not in (None, 0) else 1
        if rev_exception is None and rev_info.get("exit_code") == 0:
            self._record_provider_success(self.reviewer)
        rev_stderr = rev_info.get("stderr", "") or ""
        if rev_exception is not None and not rev_stderr:
            rev_stderr = rev_exception
        rev_timed_out = any("TIMEOUT:" in p for p in review_result.p0)

        is_unparseable = any("unparseable_review" in s.lower() for s in (review_result.p0 + [review_result.summary]))
        if is_unparseable:
            fail_msg = f"REVIEWER_EXECUTION_FAILURE: Unparseable review output ({review_result.summary})"
            self.sm.log_audit(run_id, getattr(self, "current_reviewer_name", "CODEX"), "REVIEW_PARSE_FAILED", fail_msg)
            self._save_json(run_id, "review.json", review_result)
            self._reviewer_provenance(1, provenance_records)
            return review_result, self._stop(run_id, State.FAIL, fail_msg, logger, provenance_records)

        if rev_exit != 0 or rev_timed_out:
            cli_err = rev_stderr or (" ".join(review_result.p0))
            cat = self.pm.classify_error(cli_err)
            if cat == ErrorCategory.CONFIG_ERROR:
                stop_summary = self._handle_provider_failure(rev_domain, cli_err, run_id, "REVIEWING", logger, provenance_records)
                if stop_summary:
                    return review_result, stop_summary
            if cat in (ErrorCategory.QUOTA, ErrorCategory.AUTH, ErrorCategory.TIMEOUT, ErrorCategory.PROVIDER_UNAVAILABLE):
                stop_summary = self._handle_provider_failure(rev_domain, cli_err, run_id, "REVIEWING", logger, provenance_records)
                if stop_summary:
                    return review_result, stop_summary
                if rev_domain == "OPENAI":
                    logger.warning(f"[FAILOVER TRIGGERED] Primary Reviewer (Codex) failed ({cat.value}). Switching to backup Reviewer (Claude)...")
                    assigned = self.pm.resolve_roles(requested_head=getattr(self.head, "provider", "openai"), requested_builder="antigravity", requested_reviewer="claude")
                    if assigned["can_proceed"]:
                        self.current_failover_mode = assigned["mode"]
                        self.current_reviewer_name = "CLAUDE"
                        self.reviewer = self._init_reviewer("claude")
                        self.sm.log_audit(run_id, "ORCHESTRATOR", "FAILOVER_REVIEWER", f"Switched to Claude Reviewer due to {cli_err}")
                        stop_trial = self._claim_trial_for(self.reviewer, run_id, "REVIEWING_BACKUP", logger)
                        if stop_trial:
                            return review_result, stop_trial
                        try:
                            review_result = self.reviewer.review(order, git_diff, verification, project_context, worktree_path=worktree_path)
                            if (getattr(self.reviewer, "last_execution_info", {}) or {}).get("exit_code") == 0:
                                self._record_provider_success(self.reviewer)
                        except Exception as e2:
                            fail_msg = f"REVIEWER_EXCEPTION (backup reviewer): {str(e2)[:300]}"
                            self.sm.log_audit(run_id, "ORCHESTRATOR", "REVIEWER_EXCEPTION", fail_msg)
                            self._reviewer_provenance(1, provenance_records)
                            return review_result, self._stop(run_id, State.FAIL, fail_msg, logger, provenance_records)
                    else:
                        return review_result, self._stop(run_id, State.HUMAN_REQUIRED, f"PROVIDER_FAILURE: {assigned['reason']}", logger, provenance_records)
            elif rev_exception is not None:
                # non-provider reviewer exception: fail closed, terminal state guaranteed
                fail_msg = f"REVIEWER_EXCEPTION: {rev_exception[:300]}"
                self._save_json(run_id, "review.json", review_result)
                return review_result, self._stop(run_id, State.FAIL, fail_msg, logger, provenance_records)

        self._save_json(run_id, "review.json", review_result)
        self.sm.log_audit(run_id, getattr(self, "current_reviewer_name", "CODEX"), "CODE_REVIEW", json.dumps(review_result.model_dump()))
        if rev_exception is None:
            self._reviewer_provenance(0 if review_result.verdict == "pass" else 1, provenance_records)
        return review_result, None

    def run_decision_with_failover(self, run_id: str, order: OrderSchema, builder_result: BuilderResultSchema,
                                   verification: Dict[str, Any], review_result: ReviewResultSchema, worktree_path: str,
                                   logger: logging.Logger, provenance_records: list):
        """HEAD decision with provider failure handling / Claude failover. Returns (decision, stop)."""
        if self._proxy():
            return self._run_decision_autonomous(run_id, order, builder_result, verification, review_result,
                                                 worktree_path, logger, provenance_records)
        stop_trial = self._claim_trial_for(self.head, run_id, "DECIDING", logger)
        if stop_trial:
            return None, stop_trial
        head_domain = self._get_agent_domain(self.head)
        try:
            decision = self.head.decide(order, builder_result, verification, review_result, cwd=worktree_path)
            self._record_provider_success(self.head)
            return decision, None
        except Exception as e:
            err_str = str(e)
            logger.error(f"Decision failed with {head_domain}: {err_str}")
            stop_summary = self._handle_provider_failure(head_domain, err_str, run_id, "DECIDING", logger, provenance_records)
            if stop_summary:
                return None, stop_summary
            head_prov = getattr(self.head, "provider", "openai").lower()
            if head_prov in ("openai", "codex"):
                logger.warning("[FAILOVER TRIGGERED] Primary HEAD decision failed. Switching to Claude HEAD...")
                assigned = self.pm.resolve_roles(requested_head="claude", requested_builder="antigravity", requested_reviewer=getattr(self.reviewer, "provider", "claude"))
                if assigned["can_proceed"]:
                    self.head = self._init_head("claude")
                    self.current_head_name = "CLAUDE"
                    stop_trial = self._claim_trial_for(self.head, run_id, "DECIDING_BACKUP", logger)
                    if stop_trial:
                        return None, stop_trial
                    try:
                        decision = self.head.decide(order, builder_result, verification, review_result, cwd=worktree_path)
                        self._record_provider_success(self.head)
                        return decision, None
                    except Exception as e2:
                        err_str2 = str(e2)
                        stop2 = self._handle_provider_failure("ANTHROPIC", err_str2, run_id, "DECIDING_BACKUP", logger, provenance_records)
                        if stop2:
                            return None, stop2
                        return None, self._stop(run_id, State.FAIL, f"Decision error: {err_str2}", logger, provenance_records)
                return None, self._stop(run_id, State.HUMAN_REQUIRED, f"PROVIDER_FAILURE: {assigned['reason']}", logger, provenance_records)
            state = State.TIMEOUT if ("timeout" in err_str.lower() or isinstance(e, TimeoutError)) else State.FAIL
            return None, self._stop(run_id, state, f"Decision {'timeout' if state == State.TIMEOUT else 'error'}: {err_str}", logger, provenance_records)

    def _run_reviewer_autonomous(self, run_id: str, order: OrderSchema, verification: Dict[str, Any],
                                 project_context: Dict[str, Any], worktree_path: str, logger: logging.Logger,
                                 provenance_records: list):
        """HEAD proxy review: a crashed/unparseable/timed-out review is retried once, then another provider reviews.
        A review is never fabricated: if nobody can review, the run FAILs (and the mission re-plans)."""
        git_diff = verification.get("git_diff", "") if verification else ""
        tried, last_err = set(), None
        for attempt in range(4):
            domain = self._get_agent_domain(self.reviewer)
            tried.add(domain)
            err = None
            if not self._provider_ready(self.reviewer, run_id, "REVIEWING", logger):
                err = f"PROVIDER_UNAVAILABLE: {domain} recovering without an available trial"
            else:
                try:
                    review = self.reviewer.review(order, git_diff, verification, project_context, worktree_path=worktree_path)
                    info = getattr(self.reviewer, "last_execution_info", {}) or {}
                    exit_code = info.get("exit_code")
                    unparseable = any("unparseable_review" in s.lower() for s in (review.p0 + [review.summary]))
                    timed_out = any("TIMEOUT:" in p for p in review.p0)
                    if unparseable or timed_out or exit_code not in (0, None):
                        err = (info.get("stderr") or review.summary or "reviewer failure")[:800]
                    else:
                        self._record_provider_success(self.reviewer)
                        self._save_json(run_id, "review.json", review)
                        self.sm.log_audit(run_id, getattr(self, "current_reviewer_name", domain), "CODE_REVIEW", json.dumps(review.model_dump()))
                        self._reviewer_provenance(0 if review.verdict == "pass" else 1, provenance_records)
                        return review, None
                except Exception as e:
                    err = str(e)[:800]
                self._reviewer_provenance(1, provenance_records)
            last_err = err
            cat = self.pm.classify_error(err)
            self.sm.log_audit(run_id, "ORCHESTRATOR", "REVIEWER_RETRY",
                              json.dumps({"attempt": attempt + 1, "provider": domain, "category": cat.value, "error": err[:500]}, ensure_ascii=False))
            logger.warning(f"[HEAD proxy] review attempt {attempt + 1} on {domain} failed ({cat.value}): {err[:200]}")
            if cat in self.PROVIDER_DOWN:
                try:
                    self.pm.parse_cli_error(domain, err)
                except Exception:
                    pass
            elif attempt == 0:
                continue
            if not self._swap_agent("reviewer", run_id, logger, tried, "REVIEWING", err):
                break
        return None, self._stop(run_id, State.FAIL, f"REVIEWER_UNAVAILABLE: no provider produced a valid review ({last_err})",
                                logger, provenance_records, "REVIEWER_UNAVAILABLE")

    def _run_decision_autonomous(self, run_id: str, order: OrderSchema, builder_result: BuilderResultSchema,
                                 verification: Dict[str, Any], review_result: ReviewResultSchema, worktree_path: str,
                                 logger: logging.Logger, provenance_records: list):
        """HEAD proxy decision: retry, then another provider plays HEAD. If no HEAD can be reached, the user's delegated
        rule decides from hard evidence only (tests + independent review), and says so in the rationale."""
        tried, last_err = set(), None
        for attempt in range(4):
            domain = self._get_agent_domain(self.head)
            tried.add(domain)
            if self._provider_ready(self.head, run_id, "DECIDING", logger):
                try:
                    decision = self.head.decide(order, builder_result, verification, review_result, cwd=worktree_path)
                    self._record_provider_success(self.head)
                    return decision, None
                except Exception as e:
                    last_err = str(e)[:800]
            else:
                last_err = f"PROVIDER_UNAVAILABLE: {domain} recovering without an available trial"
            cat = self.pm.classify_error(last_err)
            self.sm.log_audit(run_id, "ORCHESTRATOR", "DECISION_RETRY",
                              json.dumps({"attempt": attempt + 1, "provider": domain, "category": cat.value, "error": last_err[:500]}, ensure_ascii=False))
            if cat in self.PROVIDER_DOWN:
                try:
                    self.pm.parse_cli_error(domain, last_err)
                except Exception:
                    pass
            elif attempt == 0:
                continue
            if not self._swap_agent("head", run_id, logger, tried, "DECIDING", last_err):
                break
        green = bool(verification.get("passed")) and review_result.verdict == "pass" and not review_result.p0 and not review_result.p1
        decision = DecisionSchema(
            decision="PASS" if green else "FAIL",
            rationale=("HEAD unavailable on every provider (" + str(last_err)[:200] + "); delegated rule applied to hard "
                       "evidence: " + ("tests passed and the independent review passed" if green else
                                       "tests and/or independent review did not pass")),
            next_steps=list(review_result.p0) + list(review_result.p1) + list(review_result.p2)
                       or [str(verification.get("summary", ""))])
        self.sm.log_audit(run_id, "ORCHESTRATOR", "DECISION_FALLBACK", decision.rationale)
        return decision, None

    def apply_final_pass_gate(self, decision: DecisionSchema, review_result: ReviewResultSchema, logger: logging.Logger) -> str:
        """Orchestrator-level strict PASS rules (identical for run and resume). Mutates decision; returns final state.
        PASS survives only when: no P0, reviewer verdict == 'pass', no P1/deletion warning, HEAD and REVIEWER are
        independent (not both Claude / same agent family)."""
        reviewer_has_p0 = len(review_result.p0) > 0
        reviewer_has_p1 = len(review_result.p1) > 0
        has_deletion_warning = any("remove" in s.lower() or "delet" in s.lower() for s in (review_result.p1 + review_result.p2))
        final_state = decision.decision
        if final_state != State.PASS:
            return final_state
        if reviewer_has_p0:
            logger.warning("Overriding Head PASS to FAIL due to Reviewer P0 blocker")
            decision.decision = "FAIL"
            decision.rationale = f"Overridden by Orchestrator: Reviewer reported P0 blocker: {'; '.join(review_result.p0)}"
            return State.FAIL
        # HEAD proxy: unresolved review findings after the repair budget are a FAIL the mission re-plans from,
        # not a question for the user.
        blocked_state = State.FAIL if self._proxy() else State.HUMAN_REQUIRED
        if (self._proxy() and getattr(review_result, "verdict", None) != "pass" and not reviewer_has_p1
                and not has_deletion_warning and review_result.p2):
            # The reviewer itself rated everything left as minor: PASS, with the P2 items recorded as follow-ups.
            decision.rationale = (decision.rationale or "") + " [P2 FOLLOW-UPS: " + "; ".join(review_result.p2)[:1500] + "]"
            logger.info("[HEAD proxy] only P2 (minor) review findings remain: PASS with recorded follow-ups")
        elif getattr(review_result, "verdict", None) != "pass":
            logger.warning(f"Overriding Head PASS to {blocked_state}: reviewer verdict is {getattr(review_result, 'verdict', None)!r}, not 'pass'")
            decision.decision = blocked_state
            decision.rationale = f"Overridden by Orchestrator: reviewer verdict {getattr(review_result, 'verdict', None)!r} is not 'pass' ({review_result.summary})"
            return blocked_state
        if reviewer_has_p1 or has_deletion_warning:
            logger.warning(f"Overriding Head PASS to {blocked_state} due to existing behavior loss or P1 issue")
            decision.decision = blocked_state
            decision.rationale = f"Overridden by Orchestrator: Reviewer reported existing behavior loss or P1 concerns: {review_result.summary}"
            return blocked_state
        head_prov = getattr(self.head, "provider", "").lower()
        rev_prov = getattr(self.reviewer, "provider", "").lower()
        head_family = getattr(self.head, "agent_family", head_prov).lower()
        rev_family = getattr(self.reviewer, "agent_family", rev_prov).lower()
        is_both_claude = (head_prov in ("claude", "anthropic") and rev_prov in ("claude", "anthropic"))
        is_same_family = (head_family == rev_family and head_family not in ("mock", "unknown", "openai"))
        if (is_both_claude or is_same_family) and self._proxy():
            logger.warning(f"Reduced independence: HEAD ({head_prov}) and REVIEWER ({rev_prov}) share a provider after failover; "
                           "PASS kept on the user's delegated policy (tests + review green), recorded in the rationale.")
            decision.rationale = (decision.rationale or "") + f" [REDUCED_INDEPENDENCE: HEAD and REVIEWER both {head_family} after provider failover]"
            return State.PASS
        if is_both_claude or is_same_family:
            logger.warning(f"Same-Provider Independence Guard: HEAD ({head_prov}) and REVIEWER ({rev_prov}) share the same provider/family. Demoting PASS to HUMAN_REQUIRED.")
            decision.decision = "HUMAN_REQUIRED"
            decision.rationale = f"Same-Provider Independence Guard: HEAD and REVIEWER share same runtime/agent family ({head_family}). Automated PASS forbidden without human review."
            return State.HUMAN_REQUIRED
        return State.PASS

    def worktree_evidence(self, run_id: str, worktree_path: Optional[str]) -> Dict[str, Any]:
        """Explicit worktree evidence: path recorded (DB/argument), present on disk, resolvable."""
        recorded = worktree_path
        if not recorded:
            run = self.sm.get_run(run_id) or {}
            recorded = run.get("worktree_path")
        if not recorded:
            return {"status": "MISSING", "path": None, "reason": "worktree_path is NULL (not recorded)"}
        try:
            p = Path(recorded)
            if not p.exists() or not p.is_dir():
                return {"status": "MISSING", "path": str(recorded), "reason": "worktree directory does not exist on disk"}
            resolved = str(p.resolve())
        except Exception as e:
            return {"status": "MISSING", "path": str(recorded), "reason": f"worktree path cannot be resolved: {e}"}
        return {"status": "PRESENT", "path": resolved, "reason": None}

    def final_evidence(self, run_id: str, order: Optional[OrderSchema], worktree_path: Optional[str]) -> Dict[str, Any]:
        """Deterministic evidence a PASS requires, each item stated explicitly (PASS / FAIL / MISSING):
        - worktree evidence (recorded + present + resolvable)
        - lexical ORDER path validation
        - CANONICAL_CONTAINMENT of ORDER paths inside the real worktree (MISSING when no worktree evidence;
          lexical validation alone never counts as canonical PASS)
        - persisted baseline evidence, post-verification, regression against the baseline."""
        ev: Dict[str, Any] = {"reasons": []}
        wt = self.worktree_evidence(run_id, worktree_path)
        ev["worktree"] = wt["status"]
        ev["worktree_path"] = wt["path"]
        if wt["status"] != "PRESENT":
            ev["reasons"].append(f"WORKTREE_EVIDENCE_MISSING: {wt['reason']}")

        if order is None:
            ev["order_paths"] = "MISSING"
            ev["canonical_containment"] = "MISSING"
            ev["reasons"].append("ORDER_MISSING")
        else:
            lex = SafetyPolicy.validate_order_paths(order)
            ev["order_paths"] = "FAIL" if lex else "PASS"
            if lex:
                ev["reasons"].append("ORDER_PATH_VIOLATION: " + "; ".join(lex))
            if wt["status"] == "PRESENT":
                canon = SafetyPolicy.validate_order_paths(order, worktree_path=wt["path"])
                ev["canonical_containment"] = "FAIL" if canon else "PASS"
                if canon:
                    ev["reasons"].append("CANONICAL_CONTAINMENT=FAIL: " + "; ".join(canon))
            else:
                ev["canonical_containment"] = "MISSING"
                ev["reasons"].append("CANONICAL_CONTAINMENT=MISSING (no worktree evidence; lexical validation is not containment)")

        baseline = self.baseline_evidence(run_id)
        verification = self._load_json(run_id, "verification.json")
        if baseline is None:
            ev["baseline"] = "MISSING"
            ev["reasons"].append("BASELINE_MISSING")
        elif not baseline.get("passed", False):
            if self._proxy() and isinstance(verification, dict) and verification.get("passed"):
                ev["baseline"] = "PREEXISTING_FAILURES"   # recorded; post-build proved no NEW failures
            else:
                ev["baseline"] = "FAIL"
                ev["reasons"].append("BASELINE_FAILED")
        else:
            ev["baseline"] = "PASS"
        if not isinstance(verification, dict) or "passed" not in verification:
            ev["verification"] = "MISSING"
            ev["reasons"].append("VERIFICATION_MISSING")
        elif not verification.get("passed", False):
            ev["verification"] = "FAIL"
            ev["reasons"].append("VERIFICATION_FAILED")
        else:
            ev["verification"] = "PASS"
        ev["regression"] = "MISSING"
        if ev["baseline"] in ("PASS", "PREEXISTING_FAILURES") and ev["verification"] == "PASS":
            base_count = int((baseline.get("test_counts") or {}).get("passed", 0) or 0)
            post_count = int((verification.get("test_counts") or {}).get("passed", 0) or 0)
            if base_count > 0 and post_count < base_count:
                ev["regression"] = "FAIL"
                ev["reasons"].append(f"REGRESSION: baseline {base_count} -> post {post_count}")
            else:
                ev["regression"] = "PASS"
        return ev

    def final_evidence_reasons(self, run_id: str, order: Optional[OrderSchema], worktree_path: Optional[str]) -> list:
        """Reasons that refuse PASS (see final_evidence). Kept for callers that only need the list."""
        return list(self.final_evidence(run_id, order, worktree_path)["reasons"])

    def _legacy_final_evidence_reasons(self, run_id: str, order: Optional[OrderSchema], worktree_path: Optional[str]) -> list:
        reasons = []
        if order is None:
            reasons.append("ORDER_MISSING")
        else:
            viol = SafetyPolicy.validate_order_paths(order, worktree_path=worktree_path if worktree_path and Path(worktree_path).exists() else None)
            if viol:
                reasons.append("ORDER_PATH_VIOLATION: " + "; ".join(viol))
        baseline = self.baseline_evidence(run_id)
        if baseline is None:
            reasons.append("BASELINE_MISSING")
        elif not baseline.get("passed", False):
            reasons.append("BASELINE_FAILED")
        verification = self._load_json(run_id, "verification.json")
        if not isinstance(verification, dict) or "passed" not in verification:
            reasons.append("VERIFICATION_MISSING")
        elif not verification.get("passed", False):
            reasons.append("VERIFICATION_FAILED")
        elif baseline is not None:
            base_count = int((baseline.get("test_counts") or {}).get("passed", 0) or 0)
            post_count = int((verification.get("test_counts") or {}).get("passed", 0) or 0)
            if base_count > 0 and post_count < base_count:
                reasons.append(f"REGRESSION: baseline {base_count} -> post {post_count}")
        return reasons

    def _finalize(self, run_id: str, decision: DecisionSchema, review_result: ReviewResultSchema, logger: logging.Logger,
                  provenance_records: list, order=None, builder_result=None, verification=None, worktree_path=None) -> Dict[str, Any]:
        if (self._proxy() and decision.decision == "HUMAN_REQUIRED"
                and not autopilot.is_serious_text(f"{decision.rationale} {' '.join(decision.next_steps or [])}")):
            # The user delegated everything but money/credentials/irreversible external actions: HEAD resolves it.
            self.sm.log_audit(run_id, "HEAD", "HEAD_ESCALATION_RESOLVED_BY_PROXY", decision.rationale[:1000])
            logger.info("[HEAD proxy] HEAD escalation is not a delegated exception; converted to a repair instruction")
            decision.decision = "FAIL"
            decision.next_steps = list(decision.next_steps or []) or [decision.rationale]
        # HEAD proxy: with HEAD's PASS, green verification and only P2 (minor) findings left, no correction round is
        # spent - the final gate passes it and records the P2 items as follow-ups.
        minor_only = (self._proxy() and decision.decision == 'PASS' and verification is not None
                      and verification.get('passed') and not review_result.p0 and not review_result.p1)
        if (self.config.get('autonomy', {}).get('enabled') and order is not None and not minor_only
                and decision.decision != 'HUMAN_REQUIRED'
                and (decision.decision == 'FAIL' or not verification.get('passed')
                     or review_result.verdict != 'pass' or review_result.p0 or review_result.p1)):
            rounds = self._load_json(run_id, 'repair_history.json') or []
            if len(rounds) < self.config.get('autonomy', {}).get('max_repairs', 3):
                feedback = {'round': len(rounds) + 1, 'head': decision.model_dump(),
                            'review': review_result.model_dump(), 'verification': verification}
                rounds.append(feedback)
                self._save_json(run_id, 'repair_history.json', rounds)
                repair_order = {'round': len(rounds), 'head': decision.model_dump(),
                                'review': review_result.model_dump(),
                                'test_summary': verification.get('summary'),
                                'test_output': (verification.get('test', {}).get('output') or '')[-12000:]}
                marker = '\nHEAD REPAIR ORDER: '
                order.context = (order.context or '').split(marker, 1)[0] + marker + json.dumps(repair_order, ensure_ascii=False)
                self._save_json(run_id, 'order.json', order)
                self.sm.transition_to(run_id, State.BUILDING, details='HEAD requested correction round ' + str(len(rounds)))
                logger.info('State -> BUILDING (automatic correction round %s)', len(rounds))
                built, stop = self.run_builder_stage(run_id, order, worktree_path, None, logger, provenance_records)
                if stop: return stop
                self.sm.transition_to(run_id, State.VERIFYING, details='Testing HEAD correction')
                logger.info('State -> VERIFYING (automatic correction)')
                baseline = self.baseline_evidence(run_id) or {}
                verified, stop = self.validate_post_build(run_id, order, worktree_path,
                    (baseline.get('test_counts') or {}).get('passed', 0), logger, provenance_records)
                if stop: return stop
                self.sm.transition_to(run_id, State.REVIEWING, details='Independent review of correction')
                logger.info('State -> REVIEWING (automatic correction)')
                reviewed, stop = self.run_reviewer_with_failover(run_id, order, verified, {}, worktree_path, logger, provenance_records)
                if stop: return stop
                self.sm.transition_to(run_id, State.DECIDING, details='HEAD evaluates correction')
                logger.info('State -> DECIDING (automatic correction)')
                decided, stop = self.run_decision_with_failover(run_id, order, built, verified, reviewed, worktree_path, logger, provenance_records)
                if stop: return stop
                return self._finalize(run_id, decided, reviewed, logger, provenance_records,
                    order=order, builder_result=built, verification=verified, worktree_path=worktree_path)
        final_state = self.apply_final_pass_gate(decision, review_result, logger)
        evidence = self.final_evidence(run_id, order, worktree_path)
        evidence["head_decision"] = decision.decision
        evidence["reviewer_verdict"] = getattr(review_result, "verdict", None)
        self._save_json(run_id, "final_evidence.json", evidence)
        if final_state == State.PASS:
            reasons = list(evidence["reasons"])
            if evidence.get("canonical_containment") != "PASS":
                reasons.append(f"CANONICAL_CONTAINMENT={evidence.get('canonical_containment')} (PASS required)")
            if reasons:
                msg = "FINAL_GATE: PASS refused, evidence incomplete or invalid: " + "; ".join(dict.fromkeys(reasons))
                logger.warning(msg)
                self.sm.log_audit(run_id, "ORCHESTRATOR", "FINAL_GATE_BLOCK", msg)
                decision.decision = "HUMAN_REQUIRED"
                decision.rationale = f"Overridden by Orchestrator: {msg}"
                final_state = State.HUMAN_REQUIRED
        self._save_json(run_id, "decision.json", decision)
        self.sm.log_audit(run_id, "HEAD", "FINAL_DECISION", json.dumps(decision.model_dump()))
        self._save_json(run_id, "provenance.json", {"run_id": run_id, "records": provenance_records})
        self.sm.transition_to(run_id, final_state, details=f"Final decision: {decision.rationale}")
        logger.info(f"Final State -> {final_state}")
        if final_state == State.PASS:
            # persist the dashboard result summary ONCE here (git status now), never per /status poll
            try:
                from core.result_manager import ResultManager
                ResultManager(self.sm, self.config).record_summary(run_id)
            except Exception as e:
                logger.warning(f"result summary not persisted: {e}")
        return self._build_run_summary(run_id=run_id, order=order, builder_result=builder_result, verification=verification,
                                       review_result=review_result, decision=decision)

    def _is_mock_run(self) -> bool:
        return (
            (getattr(self.head, "provider", "") == "mock" or isinstance(self.head, MockHead)) and
            (getattr(self.reviewer, "provider", "") == "mock" or getattr(self.reviewer, "provider", "") == "")
        )

    def run(
        self,
        project_path: str,
        goal: str,
        run_id: Optional[str] = None,
        builder_fn: Optional[Callable[[AntigravityBuilder, OrderSchema, str], list]] = None,
        order: Optional[OrderSchema] = None,
        head_provenance: Optional[Dict[str, Any]] = None,
        base_worktree: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Executes a development run with bounded corrections to PASS/FAIL/HUMAN_REQUIRED.
        base_worktree: start this run's worktree from a private snapshot of an earlier run's worktree (mission
        milestones build on the previous PASSED step). The user's project is never modified either way.
        """
        proj_path = str(Path(project_path).resolve())
        if self.config.get('bootstrap', {}).get('enabled'):
            from core.bootstrap import prepare_project, configure_runtime
            proj_path = prepare_project(proj_path)
            self.config = configure_runtime(self.config, proj_path)

        # 0. Initialize run in SQLite (unique id guaranteed: 128-bit suffix + collision retry) and Provenance Tracker
        run_id = self._create_run_with_retry(run_id, proj_path, goal)
        logger = self._setup_logger(run_id)
        logger.info(f"Starting Run {run_id} for project {proj_path} with goal: {goal}")
        provenance_records = []

        is_mock_run = (
            (getattr(self.head, "provider", "") == "mock" or isinstance(self.head, MockHead)) and
            (getattr(self.reviewer, "provider", "") == "mock" or getattr(self.reviewer, "provider", "") == "")
        )

        req_head = self.config.get("head", {}).get("provider", "openai")
        req_builder = self.config.get("builder", {}).get("provider", "antigravity")
        req_reviewer = self.config.get("reviewer", {}).get("provider", "codex")

        if not is_mock_run:
            self.pm.check_health()
            # RECOVERING providers are planning candidates only; a trial is claimed at the moment of actual use
            cands = self.pm.mark_trial_candidates()
            if cands:
                logger.warning(f"[Recovery Trial] candidates (claimed only when actually called): {', '.join(cands)}")
            if self._proxy():
                assigned = self._autonomous_role_assignment(req_head, req_builder, req_reviewer, run_id, logger)
            elif self.config.get('manual_roles'):
                assigned = self.pm.resolve_roles(req_head, req_builder, req_reviewer, strict=True)
                if not assigned['can_proceed']:
                    # Refresh stale probe failures once while preserving independent role assignments.
                    self.pm.check_health(force=True)
                    self.pm.mark_trial_candidates()
                    assigned = self.pm.resolve_roles(req_head, req_builder, req_reviewer, strict=True)
            else:
                assigned = self.pm.resolve_roles(req_head, req_builder, req_reviewer)
            
            new_head_prov = assigned["head_provider"]
            if not self._proxy() and req_head.lower() == "openai" and new_head_prov == "ANTHROPIC":
                self.head = self._init_head("claude")
                self.reviewer = self._init_reviewer("claude")
                self.sm.log_audit(run_id, "ORCHESTRATOR", "FAILOVER", "Both HEAD and REVIEWER switched to Claude Code due to OPENAI unavailable")

            self.current_failover_mode = assigned["mode"]
            self.current_head_name = assigned["head_provider"].upper()
            self.current_reviewer_name = assigned["reviewer_provider"].upper()

            if not assigned["can_proceed"]:
                stop_msg = f"PROVIDER_FAILURE: {assigned['reason']}"
                logger.error(stop_msg)
                self.sm.log_audit(run_id, "ORCHESTRATOR", "PROVIDER_STOP", stop_msg)
                self.sm.transition_to(run_id, State.HUMAN_REQUIRED, details=stop_msg)
                return self._build_run_summary(run_id, scope_violations=[stop_msg])

            if assigned["mode"] == FailoverMode.DEGRADED:
                deg_msg = f"[DEGRADED MODE ACTIVE] {assigned['reason']}"
                logger.warning(deg_msg)
                self.sm.log_audit(run_id, "ORCHESTRATOR", "FAILOVER_DEGRADED", deg_msg)
                # Re-assign healthy providers
                if assigned["head_provider"] != req_head:
                    self.head = self._init_head(assigned["head_provider"])
                if assigned["reviewer_provider"] != req_reviewer:
                    self.reviewer = self._init_reviewer(assigned["reviewer_provider"])
                if assigned['builder_provider'] != req_builder:
                    self.builder = self._init_builder(assigned['builder_provider'])
        else:
            self.current_failover_mode = FailoverMode.NORMAL
            self.current_head_name = getattr(self.head, "provider", "HEAD").upper()
            self.current_reviewer_name = getattr(self.reviewer, "provider", "CODEX").upper()

        # 0.2 Sanity check for assigned agents
        if not is_mock_run and builder_fn is None:
            for role_name in ("HEAD", "BUILDER", "REVIEWER"):
                agent = getattr(self, role_name.lower())
                exe = getattr(agent, "executable", None)
                ver = agent.get_version() if hasattr(agent, "get_version") else "unknown"
                if (not exe or not Path(exe).exists() or ver == "unknown") and self._proxy():
                    tried = {self._get_agent_domain(agent)}
                    while self._swap_agent(role_name.lower(), run_id, logger, tried, "SANITY_CHECK",
                                           f"CLI missing/unversioned (executable: {exe}, version: {ver})"):
                        agent = getattr(self, role_name.lower())
                        exe = getattr(agent, "executable", None)
                        ver = agent.get_version() if hasattr(agent, "get_version") else "unknown"
                        if exe and Path(exe).exists() and ver != "unknown":
                            break
                if not exe or not Path(exe).exists() or ver == "unknown":
                    err_msg = f"CONFIG_ERROR: {role_name} CLI sanity check failed (executable: {exe}, version: {ver}). Run aborted."
                    logger.error(err_msg)
                    self.sm.transition_to(run_id, State.HUMAN_REQUIRED, details=err_msg)
                    return self._build_run_summary(run_id, scope_violations=[err_msg])

        # 1. State: IDLE -> PLANNING
        self.sm.transition_to(run_id, State.PLANNING, details="Head generating ORDER.json")
        logger.info("State -> PLANNING")
    
        project_context = self._read_project_context(proj_path)
        if order is not None:
            cap_notes = self.apply_safety_caps(order)
            if cap_notes:
                logger.warning(f"[Safety Cap] {'; '.join(cap_notes)}")
                self.sm.log_audit(run_id, "ORCHESTRATOR", "SAFETY_CAP_APPLIED", "; ".join(cap_notes))
            self._save_json(run_id, "order.json", order)
            self.sm.log_audit(run_id, "HEAD", "PROVIDED_ORDER", json.dumps(order.model_dump()))
            if head_provenance:
                provenance_records.append(dict(head_provenance))
        else:
            stop_trial = self._claim_trial_for(self.head, run_id, "PLANNING", logger)
            if stop_trial:
                return stop_trial
            try:
                head_prov = getattr(self.head, "provider", "OPENAI")
                order = self.head.create_order(goal, project_context)
                self._record_provider_success(self.head)
                self._save_json(run_id, "order.json", order)
                self.sm.log_audit(run_id, "HEAD", "CREATED_ORDER", json.dumps(order.model_dump()))
                if hasattr(self.head, "get_provenance"):
                    prov = self.head.get_provenance(exit_code=0)
                    provenance_records.append(prov.model_dump())
                    if hasattr(prov, "duration_sec") and prov.duration_sec is not None:
                        self.pm.update_latency(head_prov, prov.duration_sec)
            except Exception as e:
                err_str = str(e)
                is_timeout = ("timeout" in err_str.lower() or isinstance(e, TimeoutError))
                head_prov = getattr(self.head, "provider", "OPENAI")
                head_domain = self._get_agent_domain(self.head)
                logger.error(f"Planning failed with {head_domain}: {err_str}")
                if hasattr(self.head, "get_provenance"):
                    prov = self.head.get_provenance(exit_code=1)
                    provenance_records.append(prov.model_dump())
                    if hasattr(prov, "duration_sec") and prov.duration_sec is not None:
                        self.pm.update_latency(head_prov, prov.duration_sec)
    
                stop_summary = self._handle_provider_failure(head_domain, err_str, run_id, "PLANNING", logger, provenance_records)
                if stop_summary:
                    return stop_summary
    
                if head_prov.lower() in ("openai", "codex"):
                    logger.warning("[FAILOVER TRIGGERED] Primary HEAD failed. Switching to backup provider (Claude)...")
                    assigned = self.pm.resolve_roles(requested_head="claude", requested_builder="antigravity", requested_reviewer="claude")
                    if assigned["can_proceed"]:
                        self.current_failover_mode = assigned["mode"]
                        self.current_head_name = assigned["head_provider"].upper()
                        self.current_reviewer_name = assigned["reviewer_provider"].upper()
                        self.head = self._init_head("claude")
                        self.reviewer = self._init_reviewer("claude")
                        self.sm.log_audit(run_id, "ORCHESTRATOR", "FAILOVER_HEAD", f"Switched to Claude HEAD due to {err_str}")
                        stop_trial = self._claim_trial_for(self.head, run_id, "PLANNING_BACKUP", logger)
                        if stop_trial:
                            return stop_trial
                        try:
                            order = self.head.create_order(goal, project_context)
                            self._record_provider_success(self.head)
                            self._save_json(run_id, "order.json", order)
                            self.sm.log_audit(run_id, "HEAD", "CREATED_ORDER", json.dumps(order.model_dump()))
                            if hasattr(self.head, "get_provenance"):
                                head_prov = getattr(self.head, "provider", "ANTHROPIC")
                                prov = self.head.get_provenance(exit_code=0)
                                provenance_records.append(prov.model_dump())
                                if hasattr(prov, "duration_sec") and prov.duration_sec is not None:
                                    self.pm.update_latency(head_prov, prov.duration_sec)
                        except Exception as e2:
                            err_str2 = str(e2)
                            logger.error(f"Backup Planning failed on Claude: {err_str2}")
                            if hasattr(self.head, "get_provenance"):
                                head_prov = getattr(self.head, "provider", "ANTHROPIC")
                                prov = self.head.get_provenance(exit_code=1)
                                provenance_records.append(prov.model_dump())
                                if hasattr(prov, "duration_sec") and prov.duration_sec is not None:
                                    self.pm.update_latency(head_prov, prov.duration_sec)
                            stop2 = self._handle_provider_failure("ANTHROPIC", err_str2, run_id, "PLANNING_BACKUP", logger, provenance_records)
                            if stop2:
                                return stop2
                            self._save_json(run_id, "provenance.json", {"run_id": run_id, "records": provenance_records})
                            self.sm.transition_to(run_id, State.FAIL, details=f"Planning error: {err_str2}")
                            return self._build_run_summary(run_id)
                    else:
                        stop_msg = f"PROVIDER_FAILURE: {assigned['reason']}"
                        logger.error(stop_msg)
                        self._save_json(run_id, "provenance.json", {"run_id": run_id, "records": provenance_records})
                        self.sm.transition_to(run_id, State.HUMAN_REQUIRED, details=stop_msg)
                        return self._build_run_summary(run_id, scope_violations=[stop_msg])
                else:
                    self._save_json(run_id, "provenance.json", {"run_id": run_id, "records": provenance_records})
                    if is_timeout:
                        self.sm.transition_to(run_id, State.TIMEOUT, details=f"Planning timeout: {err_str}")
                    else:
                        self.sm.transition_to(run_id, State.FAIL, details=f"Planning error: {err_str}")
                    return self._build_run_summary(run_id)

        # 1.1 / 1.2  Global safety caps + fail-closed path validation (shared with resume)
        stop = self.prepare_order_for_execution(run_id, order, logger, provenance_records)
        if stop:
            return stop

        # 2. State: PLANNING -> ORDER_READY
        self.sm.transition_to(run_id, State.ORDER_READY, details="ORDER created and validated")
        logger.info("State -> ORDER_READY")

        # 3. Create isolated worktree
        try:
            if base_worktree or self.config.get('bootstrap', {}).get('enabled'):
                from core.bootstrap import snapshot
                base_ref = snapshot(base_worktree or proj_path)
                self._save_json(run_id, 'source_snapshot.json', {'base_ref': base_ref, 'source_head_unchanged': True,
                                                                 'base_worktree': base_worktree})
                if base_worktree:
                    self.sm.log_audit(run_id, "ORCHESTRATOR", "BUILDS_ON", f"{base_worktree} @ {base_ref}")
                worktree_path, branch_name = GitManager.create_worktree(proj_path, run_id, base_ref=base_ref)
            else:
                worktree_path, branch_name = GitManager.create_worktree(proj_path, run_id)
            self.sm.update_run_worktree(run_id, worktree_path, branch_name)
            logger.info(f"Isolated worktree created at {worktree_path}, branch: {branch_name}")
        except Exception as e:
            return self._stop(run_id, State.FAIL, f"Git worktree creation failed: {str(e)}", logger, provenance_records)

        # 3.0 ORDER paths must resolve inside the worktree (pre-existing symlink/junction escapes)
        stop = self.prepare_order_for_execution(run_id, order, logger, provenance_records, worktree_path=worktree_path)
        if stop:
            return stop

        # 3.1 BASELINE REGRESSION GATE: run existing tests BEFORE the Builder touches anything
        baseline_pass_count, stop = self.run_baseline_if_required(run_id, worktree_path, logger, provenance_records, force=True)
        if stop:
            return stop

        # 4. State: ORDER_READY -> BUILDING
        self.sm.transition_to(run_id, State.BUILDING, details="Antigravity executing in worktree")
        logger.info("State -> BUILDING")
        builder_result, stop = self.run_builder_stage(run_id, order, worktree_path, builder_fn, logger, provenance_records)
        if stop:
            return stop

        # 5. State: BUILDING -> VERIFYING
        self.sm.transition_to(run_id, State.VERIFYING, details="Orchestrator running build/test/diff verification")
        logger.info("State -> VERIFYING")
        verification, stop = self.validate_post_build(run_id, order, worktree_path, baseline_pass_count, logger, provenance_records)
        if stop:
            return stop

        # 6. State: VERIFYING -> REVIEWING
        self.sm.transition_to(run_id, State.REVIEWING, details="Reviewer inspecting diff and evidence")
        logger.info("State -> REVIEWING")
        review_result, stop = self.run_reviewer_with_failover(run_id, order, verification, project_context, worktree_path, logger, provenance_records)
        if stop:
            return stop

        # 7. State: REVIEWING -> DECIDING
        self.sm.transition_to(run_id, State.DECIDING, details=f"{getattr(self, 'current_head_name', 'HEAD')} deciding final verdict")
        logger.info("State -> DECIDING")
        decision, stop = self.run_decision_with_failover(run_id, order, builder_result, verification, review_result, worktree_path, logger, provenance_records)
        if stop:
            return stop

        # 8. Final verdict (strict PASS gate identical for run/resume)
        return self._finalize(run_id, decision, review_result, logger, provenance_records,
                              order=order, builder_result=builder_result, verification=verification, worktree_path=worktree_path)

    APPROVAL_PREFIX = "APPROVAL_REQUIRED"
    SCOPE_PREFIX = "Scope violation: "

    def approval_state(self, run_id: str) -> Dict[str, Any]:
        """Read-only classification of a HUMAN_REQUIRED run:
        - stage ENTRY      : 'APPROVAL_REQUIRED: ...' before any build (entry policy) -> approvable
        - stage POST_BUILD : 'Scope violation: ...' after the build, approvable ONLY when every violation is in
                             SafetyPolicy.APPROVABLE_POST_BUILD (e.g. Dependency configuration modified: <file>)
        - anything else (traversal, canonical/symlink escape, outside allowed_paths, regression, missing evidence,
          reviewer/final gate, provider) : NOT approvable.
        A decision (HUMAN_APPROVAL / HUMAN_REJECTED) counts only if it was recorded after the latest HUMAN_REQUIRED
        transition, so entry and post-build approvals are separate records and never inherited."""
        run = self.sm.get_run(run_id)
        if not run:
            return {"approvable": False, "reason": "run not found", "stage": None, "items": []}
        trans = self.sm.get_transitions(run_id)
        last_t = trans[-1] if trans else {}
        last = (last_t.get("details") or "")
        last_ts = last_t.get("timestamp") or ""
        audits = self.sm.get_audit_logs(run_id) if hasattr(self.sm, "get_audit_logs") else []
        after = [a for a in audits if (a.get("timestamp") or "") >= last_ts]
        decided = {a.get("action") for a in after}
        builder_ran = self._load_json(run_id, "builder.json") is not None or bool(run.get("worktree_path"))
        info = {
            "run_id": run_id, "state": run["current_state"], "reason": last, "stage": None, "items": [], "blocking": [],
            "rejected": "HUMAN_REJECTED" in decided, "approved": "HUMAN_APPROVAL" in decided,
            "builder_ran": builder_ran, "order_present": self._load_json(run_id, "order.json") is not None,
            "approvals_so_far": [a for a in audits if a.get("action") in ("HUMAN_APPROVAL", "HUMAN_REJECTED")],
        }
        if run["current_state"] != State.HUMAN_REQUIRED:
            info["approvable"] = False
            return info
        if last.startswith(self.APPROVAL_PREFIX):
            info["stage"] = "ENTRY"
            body = last.split(":", 1)[1] if ":" in last else last
            for part in [x.strip() for x in body.split(";") if x.strip()]:
                code = part.split(" ", 1)[0].strip()
                file = part[len(code):].strip().strip("()").strip() if "(" in part else ""
                # DEFAULT DENY: only codes in SafetyPolicy.APPROVABLE_ENTRY are approvable; unknown codes block
                (info["items"] if code in SafetyPolicy.APPROVABLE_ENTRY else info["blocking"]).append({"gate": code, "file": file, "text": part})
            info["approvable"] = (bool(info["items"]) and not info["blocking"] and info["order_present"] and not builder_ran
                                  and not info["rejected"] and not info["approved"])
        elif last.startswith(self.SCOPE_PREFIX):
            info["stage"] = "POST_BUILD"
            cls = SafetyPolicy.classify_post_build_violations([v.strip() for v in last[len(self.SCOPE_PREFIX):].split(";") if v.strip()])
            info["items"] = cls["approvable"]; info["blocking"] = cls["blocking"]
            info["approvable"] = (bool(info["items"]) and not info["blocking"] and builder_ran and info["order_present"]
                                  and bool(run.get("worktree_path")) and Path(run["worktree_path"]).exists()
                                  and not info["rejected"] and not info["approved"])
        else:
            info["approvable"] = False
        return info

    def approve_and_resume(self, run_id: str, approver: str = "HUMAN", note: str = "", request_id: str = "",
                           builder_fn: Optional[Callable[[AntigravityBuilder, OrderSchema, str], list]] = None) -> Dict[str, Any]:
        """Explicit human approval of an approvable HUMAN_REQUIRED halt. Records a stage-specific HUMAN_APPROVAL,
        reopens the SAME run (ENTRY -> ORDER_READY, POST_BUILD -> VERIFYING with a narrow per-file waiver) and
        continues through resume(): the builder is never re-run for POST_BUILD, every deterministic gate still applies."""
        st = self.approval_state(run_id)
        if not st.get("approvable"):
            return {"run_id": run_id, "status": "APPROVAL_NOT_APPLICABLE", "final_state": st.get("state"), "error": st}
        order = self._load_json(run_id, "order.json") or {}
        stage = st["stage"]
        record = {"approver": approver, "timestamp": datetime.utcnow().isoformat(), "run_id": run_id, "request_id": request_id,
                  "approval_stage": stage, "approval_reason": st["reason"],
                  "approved_files": [i["file"] for i in st["items"] if i.get("file")],
                  "approved_gates": [i["gate"] for i in st["items"]],
                  "approved_scope": order.get("allowed_paths", []), "note": note[:300],
                  "covers": ("entry APPROVAL_REQUIRED only" if stage == "ENTRY" else "listed post-build gate+file pairs only") +
                            "; canonical containment / traversal / outside-scope / regression / reviewer / final gate unchanged"}
        self.sm.log_audit(run_id, "HUMAN", "HUMAN_APPROVAL", json.dumps(record, ensure_ascii=False))
        if stage == "POST_BUILD":
            approvals = self._load_json(run_id, "approvals.json") or {}
            waivers = list(approvals.get("post_build") or [])
            for i in st["items"]:
                waivers.append({"gate": i["gate"], "file": i["file"], "approver": approver, "timestamp": record["timestamp"]})
            approvals["post_build"] = waivers
            self._save_json(run_id, "approvals.json", approvals)
            target = State.VERIFYING
        else:
            target = State.ORDER_READY
        if not self.sm.reopen_after_human_approval(run_id, f"HUMAN_APPROVED({stage}) by {approver}: {st['reason']}", target=target):
            return {"run_id": run_id, "status": "ALREADY_APPROVED", "final_state": (self.sm.get_run(run_id) or {}).get("current_state")}
        try:
            self.sm.mark_human_action_resolved(run_id)
        except Exception:
            pass
        return self.resume(run_id, builder_fn=builder_fn)

    def reject_human_required(self, run_id: str, rejecter: str = "HUMAN", note: str = "") -> Dict[str, Any]:
        """Explicit rejection: the run stays terminal HUMAN_REQUIRED, worktree preserved, nothing executes."""
        st = self.approval_state(run_id)
        if st.get("state") != State.HUMAN_REQUIRED or st.get("rejected"):
            return {"run_id": run_id, "status": "REJECT_NOT_APPLICABLE", "final_state": st.get("state")}
        self.sm.log_audit(run_id, "HUMAN", "HUMAN_REJECTED", json.dumps(
            {"rejecter": rejecter, "timestamp": datetime.utcnow().isoformat(), "run_id": run_id, "stage": st.get("stage"),
             "reason": st.get("reason"), "note": note[:300]}, ensure_ascii=False))
        try:
            self.sm.mark_human_action_resolved(run_id)
        except Exception:
            pass
        return {"run_id": run_id, "status": "REJECTED", "final_state": State.HUMAN_REQUIRED}

    def resume(
        self,
        run_id: str,
        builder_fn: Optional[Callable[[AntigravityBuilder, OrderSchema, str], list]] = None
    ) -> Dict[str, Any]:
        """
        Resumes a paused or interrupted run from its last recorded state using the SAME safety pipeline as run():
        order caps + path validation, canonical containment, baseline gate, builder stage, post-build scope guard,
        regression gate, reviewer failover, decision failover and the strict final PASS gate.
        """
        run = self.sm.get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found in database.")

        curr_state = run["current_state"]
        if curr_state in TERMINAL_STATES:
            return {
                "run_id": run_id,
                "status": "RESUME_NOT_ALLOWED",
                "final_state": curr_state,
                "error": f"Cannot resume run {run_id}: already in terminal state {curr_state}."
            }

        project_path = run["project_path"]
        if self.config.get('bootstrap', {}).get('enabled'):
            from core.bootstrap import configure_runtime
            self.config = configure_runtime(self.config, project_path)
        worktree_path = run["worktree_path"]
        logger = self._setup_logger(run_id)
        logger.info(f"Resuming Run {run_id} from state {curr_state}")
        prov_file = self._load_json(run_id, "provenance.json") or {}
        provenance_records = list(prov_file.get("records", [])) if isinstance(prov_file, dict) else []

        if curr_state == State.IDLE:
            return self.run(project_path, run["goal"], run_id=run_id, builder_fn=builder_fn)

        if not self._is_mock_run():
            try:
                self.pm.check_health()
                self.pm.mark_trial_candidates()
            except Exception as e:
                logger.warning(f"health check during resume failed: {e}")

        order_data = self._load_json(run_id, "order.json")
        order = OrderSchema.model_validate(order_data) if order_data else None
        project_context = self._read_project_context(project_path)

        if curr_state == State.PLANNING:
            stop_trial = self._claim_trial_for(self.head, run_id, "PLANNING", logger)
            if stop_trial:
                return stop_trial
            try:
                order = self.head.create_order(run["goal"], project_context)
                self._record_provider_success(self.head)
                self.sm.log_audit(run_id, "HEAD", "CREATED_ORDER", json.dumps(order.model_dump()))
                if hasattr(self.head, "get_provenance"):
                    provenance_records.append(self.head.get_provenance(exit_code=0).model_dump())
            except Exception as e:
                err_str = str(e)
                if hasattr(self.head, "get_provenance"):
                    provenance_records.append(self.head.get_provenance(exit_code=1).model_dump())
                stop_summary = self._handle_provider_failure(self._get_agent_domain(self.head), err_str, run_id, "PLANNING", logger, provenance_records)
                if stop_summary:
                    return stop_summary
                state = State.TIMEOUT if ("timeout" in err_str.lower() or isinstance(e, TimeoutError)) else State.FAIL
                return self._stop(run_id, state, f"Planning {'timeout' if state == State.TIMEOUT else 'error'}: {err_str}", logger, provenance_records)
            stop = self.prepare_order_for_execution(run_id, order, logger, provenance_records)
            if stop:
                return stop
            self.sm.transition_to(run_id, State.ORDER_READY, details="Resumed: ORDER created")
            curr_state = State.ORDER_READY

        if order is None:
            return self._stop(run_id, State.FAIL, "RESUME_FAILED: order.json is missing for this run", logger, provenance_records)

        # Every resumed ORDER is re-validated (caps + repo-relative paths), exactly like a fresh run
        stop = self.prepare_order_for_execution(run_id, order, logger, provenance_records)
        if stop:
            return stop

        baseline_pass_count = None

        # Safety preflight for EVERY non-terminal state: once a worktree exists, ORDER paths must also resolve
        # canonically inside it (symlink/junction escapes) before any later stage (reviewer / HEAD decision) runs.
        if worktree_path and Path(worktree_path).exists():
            stop = self.prepare_order_for_execution(run_id, order, logger, provenance_records, worktree_path=worktree_path)
            if stop:
                return stop

        if curr_state == State.ORDER_READY:
            if not worktree_path or not Path(worktree_path).exists():
                try:
                    mission_base = (self._load_json(run_id, 'mission_base.json') or {}).get('base_worktree')
                    if mission_base and not Path(mission_base).exists():
                        mission_base = None
                    if mission_base or self.config.get('bootstrap', {}).get('enabled'):
                        from core.bootstrap import snapshot
                        base_ref = snapshot(mission_base or project_path)
                        self._save_json(run_id, 'source_snapshot.json', {'base_ref': base_ref, 'source_head_unchanged': True,
                                                                         'base_worktree': mission_base})
                        worktree_path, branch_name = GitManager.create_worktree(project_path, run_id, base_ref=base_ref)
                    else:
                        worktree_path, branch_name = GitManager.create_worktree(project_path, run_id)
                    self.sm.update_run_worktree(run_id, worktree_path, branch_name)
                except Exception as e:
                    return self._stop(run_id, State.FAIL, f"Git worktree creation failed: {str(e)}", logger, provenance_records)
            stop = self.prepare_order_for_execution(run_id, order, logger, provenance_records, worktree_path=worktree_path)
            if stop:
                return stop
            # ORDER_READY is the only state in which a baseline may still be measured (worktree provably untouched)
            baseline_pass_count, stop = self.run_baseline_if_required(run_id, worktree_path, logger, provenance_records, allow_measure=True)
            if stop:
                return stop
            self.sm.transition_to(run_id, State.BUILDING, details="Resumed: Starting builder")
            curr_state = State.BUILDING

        if curr_state in (State.BUILDING, State.VERIFYING, State.REVIEWING, State.DECIDING):
            wt_ev = self.worktree_evidence(run_id, worktree_path)
            if wt_ev["status"] != "PRESENT":
                return self._stop(run_id, State.HUMAN_REQUIRED,
                                  f"WORKTREE_EVIDENCE_MISSING: {wt_ev['reason']} (state {curr_state}); canonical containment cannot be verified, no further stage runs.",
                                  logger, provenance_records, "WORKTREE_EVIDENCE_MISSING")
            worktree_path = wt_ev["path"]

        if curr_state == State.BUILDING:
            stop = self.prepare_order_for_execution(run_id, order, logger, provenance_records, worktree_path=worktree_path)
            if stop:
                return stop
            if baseline_pass_count is None:
                # BUILDING: the builder may already have modified the worktree -> persisted evidence only, never re-measure
                baseline_pass_count, stop = self.run_baseline_if_required(run_id, worktree_path, logger, provenance_records, allow_measure=False)
                if stop:
                    return stop
            builder_result, stop = self.run_builder_stage(run_id, order, worktree_path, builder_fn, logger, provenance_records)
            if stop:
                return stop
            self.sm.transition_to(run_id, State.VERIFYING, details="Resumed: Verifying")
            curr_state = State.VERIFYING

        if curr_state == State.VERIFYING:
            if baseline_pass_count is None:
                base = self.baseline_evidence(run_id)
                if base is not None and base.get("passed", False):
                    baseline_pass_count = int((base.get("test_counts") or {}).get("passed", 0) or 0)
                elif base is not None:
                    return self._stop(run_id, State.HUMAN_REQUIRED, f"BASELINE_FAILED: persisted baseline did not pass ({base.get('summary')})", logger, provenance_records, "BASELINE_FAILED")
                else:
                    return self._stop(run_id, State.HUMAN_REQUIRED, "BASELINE_MISSING: no persisted baseline evidence; a baseline cannot be measured after the builder modified the worktree.", logger, provenance_records, "BASELINE_MISSING")
            verification, stop = self.validate_post_build(run_id, order, worktree_path, baseline_pass_count, logger, provenance_records)
            if stop:
                return stop
            self.sm.transition_to(run_id, State.REVIEWING, details="Resumed: Reviewing")
            curr_state = State.REVIEWING

        if curr_state in (State.REVIEWING, State.DECIDING):
            # Late-state evidence check BEFORE any further AI call: baseline + verification must exist and be sound
            missing = [r for r in self.final_evidence_reasons(run_id, order, worktree_path)
                       if not r.startswith("ORDER_PATH_VIOLATION") and not r.startswith("CANONICAL_CONTAINMENT=FAIL")]
            if self.config.get('autonomy', {}).get('enabled'):
                # Parity with run(): a verification that EXISTS but failed goes to review/HEAD/repair, it is not
                # missing evidence. (The final gate still refuses PASS on it.)
                missing = [r for r in missing if r != "VERIFICATION_FAILED" and not r.startswith("REGRESSION")]
            if missing:
                return self._stop(run_id, State.HUMAN_REQUIRED, "EVIDENCE_MISSING: " + "; ".join(missing), logger, provenance_records,
                                  "BASELINE_MISSING" if any(m.startswith("BASELINE") for m in missing) else "EVIDENCE_MISSING")

        if curr_state == State.REVIEWING:
            verification = self._load_json(run_id, "verification.json") or {}
            review_result, stop = self.run_reviewer_with_failover(run_id, order, verification, project_context, worktree_path, logger, provenance_records)
            if stop:
                return stop
            self.sm.transition_to(run_id, State.DECIDING, details="Resumed: Deciding")
            curr_state = State.DECIDING

        if curr_state == State.DECIDING:
            builder_result = BuilderResultSchema.model_validate(self._load_json(run_id, "builder.json") or {"status": "UNKNOWN", "summary": "n/a"})
            verification = self._load_json(run_id, "verification.json") or {}
            review_data = self._load_json(run_id, "review.json")
            if not review_data:
                return self._stop(run_id, State.FAIL, "RESUME_FAILED: review.json is missing for DECIDING run", logger, provenance_records)
            review_result = ReviewResultSchema.model_validate(review_data)
            decision, stop = self.run_decision_with_failover(run_id, order, builder_result, verification, review_result, worktree_path, logger, provenance_records)
            if stop:
                return stop
            return self._finalize(run_id, decision, review_result, logger, provenance_records,
                                  order=order, builder_result=builder_result, verification=verification, worktree_path=worktree_path)

        return self._build_run_summary(run_id)

    def _build_run_summary(
        self,
        run_id: str,
        order: Optional[OrderSchema] = None,
        builder_result: Optional[BuilderResultSchema] = None,
        verification: Optional[Dict[str, Any]] = None,
        review_result: Optional[ReviewResultSchema] = None,
        decision: Optional[DecisionSchema] = None,
        scope_violations: Optional[list] = None
    ) -> Dict[str, Any]:
        run = self.sm.get_run(run_id)
        current_state = run["current_state"] if run else "UNKNOWN"
        
        # Count changed files from worktree if possible
        files_changed = 0
        if run and run.get("worktree_path"):
            try:
                numstat = GitManager.get_numstat(run["worktree_path"])
                untracked = GitManager.get_untracked_files(run["worktree_path"])
                files_changed = len(numstat) + len(untracked)
            except Exception:
                pass

        return {
            "run_id": run_id,
            "final_state": current_state,
            "files_changed": files_changed,
            "scope_violations": len(scope_violations or []),
            "failover_mode": getattr(self, "current_failover_mode", "NORMAL"),
            "head_label": getattr(self, "current_head_name", "HEAD"),
            "reviewer_label": getattr(self, "current_reviewer_name", "CODEX"),
            "order": order.model_dump() if order else self._load_json(run_id, "order.json"),
            "provenance": self._load_json(run_id, "provenance.json"),
            "baseline_verification": self._load_json(run_id, "baseline_verification.json"),
            "builder": builder_result.model_dump() if builder_result else self._load_json(run_id, "builder.json"),
            "verification": verification or self._load_json(run_id, "verification.json"),
            "review": review_result.model_dump() if review_result else self._load_json(run_id, "review.json"),
            "decision": decision.model_dump() if decision else self._load_json(run_id, "decision.json")
        }

import os
import shutil
import subprocess
from enum import Enum
from typing import Dict, Any, Optional, Tuple, List
from core.env_utils import resolve_cli, get_sanitized_env

class ProviderStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    RECOVERING = "RECOVERING"      # CLI runtime alive (cheap probe ok) but quota/inference NOT yet verified
    QUOTA_LOW = "QUOTA_LOW"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    AUTH_FAILED = "AUTH_FAILED"
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"
    CONFIG_ERROR = "CONFIG_ERROR"
    UNKNOWN = "UNKNOWN"

class ErrorCategory(str, Enum):
    QUOTA = "QUOTA"
    AUTH = "AUTH"
    TIMEOUT = "TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    CONFIG_ERROR = "CONFIG_ERROR"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def classify(cls, output: str) -> "ErrorCategory":
        lower = output.lower()

        # 1. Config error (checked early so config errors don't trigger provider down)
        if any(phrase in lower for phrase in [
            "not inside a trusted directory",
            "trusted directory",
            "config_error",
            "missing executable",
            "invalid configuration"
        ]):
            return cls.CONFIG_ERROR

        # 2. Quota / Subscription limit (explicit phrases only)
        if any(phrase in lower for phrase in [
            "usage limit",
            "usage limit reached",
            "usage limit exceeded",
            "quota exhausted",
            "subscription limit",
            "subscription limit reached",
            "purchase more credits",
            "insufficient_quota",
            "resource_exhausted",
            "rate limit exceeded",
            "rate limit reached",
            "rate limit:",
            "too many requests"
        ]):
            return cls.QUOTA

        # 3. Auth failure (explicit phrases, avoid matching single generic 'auth')
        if any(phrase in lower for phrase in [
            "authentication expired",
            "authentication failed",
            "login required",
            "token expired",
            "not logged in",
            "keyring error",
            "unauthorized"
        ]):
            return cls.AUTH

        # 4. Process/CLI Timeout (explicit phrases, avoid matching generic 'timeout' in review critique)
        if any(phrase in lower for phrase in [
            "timeout:",
            "timed out",
            "cli timeout",
            "process timed out",
            "hard timeout exceeded",
            "hard limit exceeded",
            "no-progress timeout",
            "no output received for"
        ]):
            return cls.TIMEOUT

        # 5. Service unavailable (explicit codes/phrases)
        if any(phrase in lower for phrase in [
            "service unavailable",
            "connection refused",
            "502 bad gateway",
            "503 service unavailable",
            "provider unavailable",
            "502",
            "503"
        ]):
            return cls.PROVIDER_UNAVAILABLE

        # 6. Execution error
        if any(phrase in lower for phrase in [
            "syntaxerror",
            "compilation error",
            "test failed",
            "assertionerror",
            "execution error",
            "non-zero exit code",
            "unparseable_review",
            "jsondecodeerror"
        ]):
            return cls.EXECUTION_ERROR

        return cls.UNKNOWN

class FailoverMode(str, Enum):
    NORMAL = "NORMAL"          # 3 providers healthy
    DEGRADED = "DEGRADED"      # 2 providers healthy, 1 down
    STOP = "STOP"              # 1 or 0 providers healthy (stop autonomous run)

import sqlite3
import time
from datetime import datetime
from pathlib import Path

class ProviderManager:
    """
    Manages failure domains across OPENAI, ANTHROPIC, and GOOGLE.
    Handles health checks, quota exhaustion detection, and safe failover policies.
    """

    def __init__(self, timeouts: Optional[Dict[str, int]] = None, db_path: Optional[str] = None):
        self.timeouts = timeouts or {"head": 300, "builder": 600, "reviewer": 300}
        
        from core.db_path import get_db_path
        if db_path is None:
            self.db_path = get_db_path()
        else:
            self.db_path = Path(db_path)
            
        self.status_map: Dict[str, ProviderStatus] = {
            "OPENAI": ProviderStatus.HEALTHY,
            "ANTHROPIC": ProviderStatus.HEALTHY,
            "GOOGLE": ProviderStatus.HEALTHY
        }
        self.last_errors: Dict[str, str] = {}
        self.active_trials: set = set()      # domains for which THIS process holds a recovery trial claim
        self.trial_candidates: set = set()   # RECOVERING domains that COULD be claimed by this run (planning only)
        self._sync_from_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _sync_from_db(self):
        try:
            with self._get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS provider_health (
                        provider TEXT PRIMARY KEY,
                        cli TEXT,
                        version TEXT,
                        status TEXT NOT NULL,
                        last_success DATETIME,
                        last_failure DATETIME,
                        failure_reason TEXT,
                        current_role TEXT,
                        last_latency_sec REAL,
                        consecutive_failures INTEGER DEFAULT 0,
                        usage_percent REAL,
                        remaining REAL,
                        reset_at DATETIME,
                        last_checked DATETIME
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS provider_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp DATETIME NOT NULL,
                        provider TEXT NOT NULL,
                        status TEXT NOT NULL,
                        usage_percent REAL,
                        latency REAL,
                        success INTEGER NOT NULL
                    )
                ''')
                
                # Migration: trial bookkeeping column (v0.2.3.2)
                cols = [r[1] for r in cursor.execute("PRAGMA table_info(provider_health)").fetchall()]
                if "trial_started_at" not in cols:
                    cursor.execute("ALTER TABLE provider_health ADD COLUMN trial_started_at TEXT")
                if "next_trial_at" not in cols:
                    cursor.execute("ALTER TABLE provider_health ADD COLUMN next_trial_at TEXT")

                # Create initial rows if not exist
                for provider in ["OPENAI", "ANTHROPIC", "GOOGLE"]:
                    cursor.execute('''
                        INSERT OR IGNORE INTO provider_health (provider, status, consecutive_failures) 
                        VALUES (?, ?, 0)
                    ''', (provider, ProviderStatus.HEALTHY.value))
                conn.commit()
                
                cursor.execute('SELECT provider, status FROM provider_health')
                for row in cursor.fetchall():
                    prov = row['provider'].upper()
                    if prov in self.status_map:
                        try:
                            self.status_map[prov] = ProviderStatus(row['status'])
                        except ValueError:
                            pass
        except Exception:
            pass # fallback to in-memory

    def get_status(self, domain: str) -> ProviderStatus:
        self._sync_from_db()
        return self.status_map.get(domain.upper(), ProviderStatus.UNAVAILABLE)

    def set_status(self, domain: str, status: ProviderStatus, error: Optional[str] = None):
        d = domain.upper()
        self.status_map[d] = status
        if error:
            self.last_errors[d] = error
        
        try:
            with self._get_conn() as conn:
                cursor = conn.cursor()
                now = datetime.utcnow().isoformat()
                
                # Fetch existing consecutive failures
                cursor.execute('SELECT consecutive_failures FROM provider_health WHERE provider = ?', (d,))
                row = cursor.fetchone()
                fails = row['consecutive_failures'] if row else 0
                
                if status == ProviderStatus.HEALTHY:
                    fails = 0
                    cursor.execute('''
                        UPDATE provider_health
                        SET status = ?, last_success = ?, consecutive_failures = ?, trial_started_at = NULL, reset_at = NULL, next_trial_at = NULL
                        WHERE provider = ?
                    ''', (status.value, now, fails, d))
                elif status == ProviderStatus.RECOVERING:
                    # runtime probe ok, quota unverified: not a failure, not a success
                    cursor.execute('''
                        UPDATE provider_health
                        SET status = ?, failure_reason = ?, last_checked = ?
                        WHERE provider = ?
                    ''', (status.value, error, now, d))
                    conn.commit()
                    return
                else:
                    self.active_trials.discard(d)
                    self.trial_candidates.discard(d)
                    fails += 1
                    reset_at = self.parse_reset_at(error or "") if status == ProviderStatus.QUOTA_EXHAUSTED else None
                    # cooldown is PRESERVED after a failed real call: the next trial is only allowed after
                    # TRIAL_COOLDOWN_SEC (and, for quota, not before reset_at); force refresh cannot bypass it
                    from datetime import timedelta as _td
                    next_trial = (datetime.utcnow() + _td(seconds=self.TRIAL_COOLDOWN_SEC)).isoformat()
                    if reset_at and reset_at > next_trial:
                        next_trial = reset_at
                    cursor.execute('''
                        UPDATE provider_health
                        SET status = ?, last_failure = ?, failure_reason = ?, consecutive_failures = ?, reset_at = ?,
                            trial_started_at = NULL, next_trial_at = ?
                        WHERE provider = ?
                    ''', (status.value, now, error, fails, reset_at, next_trial, d))
                
                # Record metrics
                cursor.execute('''
                    INSERT INTO provider_metrics (timestamp, provider, status, usage_percent, latency, success)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (now, d, status.value, None, 0.0, 1 if status == ProviderStatus.HEALTHY else 0))
                
                conn.commit()
        except Exception as e:
            pass # fallback to memory

    def classify_error(self, output: str) -> ErrorCategory:
        return ErrorCategory.classify(output)

    def parse_cli_error(self, domain: str, output: str) -> ProviderStatus:
        d = domain.upper()
        cat = ErrorCategory.classify(output)
        if cat == ErrorCategory.CONFIG_ERROR:
            # CONFIG_ERROR (e.g. trusted directory) must NOT mark provider down
            self.last_errors[d] = f"CONFIG_ERROR: {output[:200]}"
            return self.status_map.get(d, ProviderStatus.HEALTHY)
        if cat in (ErrorCategory.EXECUTION_ERROR, ErrorCategory.UNKNOWN):
            # EXECUTION_ERROR or UNKNOWN must NOT mark provider down
            self.last_errors[d] = f"{cat.value}: {output[:200]}"
            return self.status_map.get(d, ProviderStatus.HEALTHY)
        if cat == ErrorCategory.QUOTA:
            self.set_status(d, ProviderStatus.QUOTA_EXHAUSTED, output[:200])
            return ProviderStatus.QUOTA_EXHAUSTED
        if cat == ErrorCategory.AUTH:
            self.set_status(d, ProviderStatus.AUTH_FAILED, output[:200])
            return ProviderStatus.AUTH_FAILED
        if cat == ErrorCategory.TIMEOUT:
            self.set_status(d, ProviderStatus.TIMEOUT, output[:200])
            return ProviderStatus.TIMEOUT
        if cat == ErrorCategory.PROVIDER_UNAVAILABLE:
            self.set_status(d, ProviderStatus.UNAVAILABLE, output[:200])
            return ProviderStatus.UNAVAILABLE

        return self.status_map.get(d, ProviderStatus.HEALTHY)

    def parse_cli_output(self, domain: str, output: str) -> ProviderStatus:
        return self.parse_cli_error(domain, output)

    PROBE_TIMEOUT_SEC = 5.0   # runtime-availability probe only; killed as a process tree on timeout

    def _get_cli_version(self, cli_bin: str, arg: str = "--version") -> str:
        try:
            from core.env_utils import probe_version
            return probe_version(cli_bin, timeout=self.PROBE_TIMEOUT_SEC, arg=arg)
        except Exception:
            return "UNKNOWN"

    # ---- bounded recovery of stale failure states -------------------------------------
    RECHECK_AFTER_SEC = 1800  # a failure older than this may be rechecked with a cheap probe

    @staticmethod
    def parse_reset_at(text: str) -> Optional[str]:
        """Extracts a quota reset time from CLI output when present (ISO string) else None.
        Supports ISO timestamps and the Codex phrasing 'try again at 11:13 PM'."""
        import re as _re
        if not text:
            return None
        m = _re.search(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)", text)
        if m:
            try:
                return datetime.fromisoformat(m.group(1).replace(" ", "T")).isoformat()
            except ValueError:
                pass
        m = _re.search(r"try again at\s+(\d{1,2}):(\d{2})\s*(AM|PM)", text, _re.I)
        if m:
            hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3).upper()
            hour = hour % 12 + (12 if ampm == "PM" else 0)
            now_local = datetime.now()
            candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now_local:
                from datetime import timedelta
                candidate = candidate + timedelta(days=1)
            # stored in UTC to match last_failure/last_checked columns
            offset = datetime.utcnow() - now_local
            return (candidate + offset).isoformat()
        return None

    def _health_row(self, domain: str) -> Dict[str, Any]:
        try:
            with self._get_conn() as conn:
                row = conn.execute("SELECT * FROM provider_health WHERE provider = ?", (domain.upper(),)).fetchone()
                return dict(row) if row else {}
        except Exception:
            return {}

    def _recheck_allowed(self, domain: str) -> bool:
        """True when a stale failure may be re-probed: reset_at has passed, or the last failure
        is older than RECHECK_AFTER_SEC. Unknown timestamps never unlock a recheck."""
        row = self._health_row(domain)
        now = datetime.utcnow()
        reset_at = row.get("reset_at")
        if reset_at:
            try:
                if now >= datetime.fromisoformat(reset_at):
                    return True
            except ValueError:
                pass
        last_failure = row.get("last_failure")
        if last_failure:
            try:
                return (now - datetime.fromisoformat(last_failure)).total_seconds() >= self.RECHECK_AFTER_SEC
            except ValueError:
                return False
        return False

    def _probe_and_update(self, domain: str, cli_name: str, cli_bin: Optional[str], force: bool,
                          failure_statuses) -> None:
        """Cheap probe (binary present + --version). Never fabricates HEALTHY: a provider in a
        failure state is only restored when a recheck is allowed (or forced) AND the probe succeeds."""
        if not cli_bin:
            self.set_status(domain, ProviderStatus.UNAVAILABLE, f"{cli_name} CLI missing")
            self._update_provider_info(domain, "UNKNOWN", "UNKNOWN")
            return
        ver = self._get_cli_version(cli_bin, "--version")
        if not ver or ver == "UNKNOWN":
            self.set_status(domain, ProviderStatus.TIMEOUT, f"{cli_name} --version timed out or failed")
            self._update_provider_info(domain, cli_name, "UNKNOWN")
            return
        current = self.status_map.get(domain)
        if current in (None, ProviderStatus.HEALTHY):
            self.set_status(domain, ProviderStatus.HEALTHY)          # HEALTHY stays HEALTHY (probe is consistent)
        elif current in failure_statuses and (force or self._recheck_allowed(domain)):
            # Truthfulness: a version probe proves CLI availability only. Quota/inference is unverified,
            # so the provider becomes RECOVERING, never HEALTHY. HEALTHY requires record_success().
            self.set_status(domain, ProviderStatus.RECOVERING,
                            f"RECOVERING: cheap probe ok ({'forced' if force else 'scheduled'} recheck); quota/inference unverified until a real CLI call succeeds")
        else:
            # RECOVERING / QUOTA_LOW / DEGRADED / CONFIG_ERROR / UNKNOWN / fresh failures: a version probe only
            # proves CLI_PRESENT. Status meaning is preserved; HEALTHY requires a real successful call.
            pass
        self._update_provider_info(domain, cli_name, ver)

    # ---- real-call outcome hooks & bounded trial calls ---------------------------------
    TRIAL_COOLDOWN_SEC = 600  # at most one recovery trial call per provider per cooldown window

    def record_success(self, domain: str) -> None:
        """A real AI CLI invocation finished with exit_code 0: the only path to HEALTHY from a failure state."""
        d = domain.upper()
        if d not in self.status_map:
            return
        self.set_status(d, ProviderStatus.HEALTHY)
        self.active_trials.discard(d)
        self.trial_candidates.discard(d)

    def trial_available(self, domain: str) -> bool:
        """Read-only: could a trial be claimed for this RECOVERING provider right now (no in-flight trial,
        cooldown / reset_at passed)? Used for planning; does not claim."""
        d = domain.upper()
        self._sync_from_db()
        if self.status_map.get(d) != ProviderStatus.RECOVERING:
            return False
        if d in self.active_trials:
            return True
        now = datetime.utcnow()
        row = self._health_row(d)
        started = row.get("trial_started_at")
        if started:
            try:
                if (now - datetime.fromisoformat(started)).total_seconds() < self.TRIAL_COOLDOWN_SEC:
                    return False
            except ValueError:
                return False
        nxt = row.get("next_trial_at")
        if nxt:
            try:
                if now < datetime.fromisoformat(nxt):
                    return False
            except ValueError:
                return False
        return True

    @staticmethod
    def domain_for_provider(provider: str) -> str:
        """Single provider-name -> domain mapping shared by the orchestrator and the dashboard."""
        prov = (provider or "").lower()
        if prov in ("anthropic", "claude"):
            return "ANTHROPIC"
        if prov in ("google", "antigravity"):
            return "GOOGLE"
        if prov in ("openai", "codex"):
            return "OPENAI"
        if prov == "mock":
            return "MOCK"
        return "UNKNOWN"

    def available_trial_candidates(self, only: Optional[List[str]] = None) -> List[str]:
        """READ-ONLY: RECOVERING providers whose single recovery trial could be claimed right now
        (restricted to `only` when given). No DB write, no claim, no probe, no in-memory planning state."""
        cands = []
        for dom, st in list(self.status_map.items()):
            if only is not None and dom not in [o.upper() for o in only]:
                continue
            if st == ProviderStatus.RECOVERING and self.trial_available(dom):
                cands.append(dom)
        return cands

    def mark_trial_candidates(self, only: Optional[List[str]] = None) -> List[str]:
        """Planning helper: remember which RECOVERING providers this run could try (restricted to `only`
        when given). No DB write, no claim consumed. Same policy as available_trial_candidates()."""
        cands = self.available_trial_candidates(only)
        self.trial_candidates = set(cands)
        return cands

    def get_effective_mode_for_next_run(self, only: Optional[List[str]] = None) -> Dict[str, Any]:
        """READ-ONLY preview of the mode the next chat/run gate will compute, using exactly the execution
        policy (mark_trial_candidates + get_failover_mode) without marking, claiming or probing anything.
        `only` mirrors the caller: the chat path passes [HEAD domain]; a full run passes None (all).
        Provider statuses are untouched: a RECOVERING candidate is reported as a trial, never as HEALTHY."""
        self._sync_from_db()
        cands = self.available_trial_candidates(only)
        usable = self.usable_domains(candidates=set(cands))
        mode, down = self._mode_for_usable(usable)
        return {
            "mode": mode,
            "down": down,
            "trial_candidates": cands,
            "usable": usable,
            "statuses": {d: s.value for d, s in self.status_map.items()},
        }

    def claim_trial(self, domain: str) -> bool:
        """ATOMIC single-owner claim of one real trial call for a RECOVERING provider (cross-process safe).
        One conditional UPDATE decides ownership: only the process whose UPDATE affected a row is the owner."""
        d = domain.upper()
        self._sync_from_db()
        if self.status_map.get(d) != ProviderStatus.RECOVERING:
            return False
        if d in self.active_trials:
            return True
        now = datetime.utcnow()
        from datetime import timedelta as _td
        stale_before = (now - _td(seconds=self.TRIAL_COOLDOWN_SEC)).isoformat()
        try:
            with self._get_conn() as conn:
                conn.isolation_level = None
                cur = conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute('''
                    UPDATE provider_health
                    SET trial_started_at = ?
                    WHERE provider = ?
                      AND status = ?
                      AND (trial_started_at IS NULL OR trial_started_at < ?)
                      AND (next_trial_at IS NULL OR next_trial_at <= ?)
                ''', (now.isoformat(), d, ProviderStatus.RECOVERING.value, stale_before, now.isoformat()))
                claimed = (cur.rowcount == 1)
                conn.execute("COMMIT")
        except Exception:
            return False
        if not claimed:
            self.trial_candidates.discard(d)
            return False
        self.active_trials.add(d)
        self.trial_candidates.discard(d)
        return True

    def usable_domains(self, candidates: Optional[set] = None) -> List[str]:
        """HEALTHY providers, plus RECOVERING providers that this run has claimed (active) or could still claim
        (candidates, planning only). `candidates` defaults to the planning state set by mark_trial_candidates()."""
        cands = self.trial_candidates if candidates is None else candidates
        return [dom for dom, st in self.status_map.items()
                if st == ProviderStatus.HEALTHY
                or (st == ProviderStatus.RECOVERING and (dom in self.active_trials or dom in cands))]

    def _update_provider_info(self, domain: str, cli_name: str, version: str):
        try:
            with self._get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE provider_health 
                    SET cli = ?, version = ?, last_checked = ?
                    WHERE provider = ?
                ''', (cli_name, version, datetime.utcnow().isoformat(), domain))
                conn.commit()
        except Exception:
            pass

    def check_health(self, force: bool = False) -> Dict[str, ProviderStatus]:
        """Performs fast health checks for all three CLI providers."""
        failure_statuses = (ProviderStatus.QUOTA_EXHAUSTED, ProviderStatus.AUTH_FAILED, ProviderStatus.UNAVAILABLE, ProviderStatus.TIMEOUT)
        self._sync_from_db()

        self._probe_and_update("ANTHROPIC", "claude-code-cli", resolve_cli("claude"), force, failure_statuses)
        self._probe_and_update("GOOGLE", "antigravity-cli", resolve_cli("agy"), force, failure_statuses)
        self._probe_and_update("OPENAI", "codex-cli", resolve_cli("codex"), force, failure_statuses)

        return self.status_map.copy()

    def _mode_for_usable(self, usable_list: List[str]) -> Tuple[FailoverMode, List[str]]:
        """The one mode rule: 3 usable -> NORMAL, 2 -> DEGRADED, otherwise STOP."""
        usable = set(usable_list)
        healthy_providers = [domain for domain in self.status_map if domain in usable]
        down_providers = [
            f"{domain}({status.value})" for domain, status in self.status_map.items()
            if domain not in usable
        ]
        if len(healthy_providers) == 3:
            return FailoverMode.NORMAL, down_providers
        elif len(healthy_providers) == 2:
            return FailoverMode.DEGRADED, down_providers
        else:
            return FailoverMode.STOP, down_providers

    def get_failover_mode(self) -> Tuple[FailoverMode, List[str]]:
        return self._mode_for_usable(self.usable_domains())

    def resolve_roles(
        self,
        requested_head: str = "openai",
        requested_builder: str = "antigravity",
        requested_reviewer: str = "codex",
        strict: bool = False
    ) -> Dict[str, Any]:
        mode, down = self.get_failover_mode()
        
        assigned = {
            "mode": mode,
            "down_providers": down,
            "head_provider": requested_head,
            "builder_provider": requested_builder,
            "reviewer_provider": requested_reviewer,
            "can_proceed": True,
            "reason": "All primary providers are healthy."
        }

        if mode == FailoverMode.STOP:
            assigned["can_proceed"] = False
            down_str = ", ".join(down)
            assigned["reason"] = f"Multiple providers down ({down_str}). Minimum 2 healthy providers required. Halting autonomously."
            return assigned

        usable = set(self.usable_domains())
        openai_healthy = "OPENAI" in usable
        anthropic_healthy = "ANTHROPIC" in usable
        google_healthy = "GOOGLE" in usable

        if strict:
            from core.role_settings import ALIASES, DOMAINS
            missing = [DOMAINS[ALIASES[p.lower()]] for p in (requested_head, requested_builder, requested_reviewer)
                       if ALIASES[p.lower()] in DOMAINS and DOMAINS[ALIASES[p.lower()]] not in usable]
            if missing:
                assigned['can_proceed'] = False
                assigned['reason'] = 'Manually selected CLI unavailable: ' + ', '.join(missing)
                return assigned
        elif not openai_healthy:
            # OpenAI down -> Claude steps in as HEAD & REVIEWER
            assigned["head_provider"] = "claude"
            assigned["reviewer_provider"] = "claude"
            assigned["reason"] = "OpenAI unavailable. Degraded mode active: Claude acting as Head and Reviewer."
        elif not google_healthy:
            # Google down -> Codex steps in as Builder, Claude as Reviewer
            assigned["builder_provider"] = "codex"
            assigned["reviewer_provider"] = "claude"
            assigned["reason"] = "Google unavailable. Degraded mode active: Codex acting as Builder."
        elif not anthropic_healthy:
            # Anthropic down -> OpenAI handles Head & Reviewer
            assigned["head_provider"] = "openai"
            assigned["reviewer_provider"] = "codex"
            assigned["reason"] = "Anthropic unavailable. Degraded mode active: OpenAI handling Head & Reviewer."

        # A local LLM role never depends on cloud health: keep it even when failover rewrote other roles.
        if (requested_head or "").lower() == "local":
            assigned["head_provider"] = "local"
        if (requested_reviewer or "").lower() == "local":
            assigned["reviewer_provider"] = "local"

        # Update roles in DB
        try:
            with self._get_conn() as conn:
                cursor = conn.cursor()
                # Reset roles
                cursor.execute("UPDATE provider_health SET current_role = 'NONE'")
                
                # Assign roles based on `assigned`
                # Map providers
                role_map = {
                    "OPENAI": [],
                    "ANTHROPIC": [],
                    "GOOGLE": []
                }
                
                from core.role_settings import ALIASES, DOMAINS
                for role in ('head', 'builder', 'reviewer'):
                    alias = ALIASES[assigned[role + '_provider'].lower()]
                    if alias in DOMAINS:   # 'local' LLM has no cloud domain row
                        role_map[DOMAINS[alias]].append(role.upper())
                    
                for prov, roles in role_map.items():
                    role_str = ", ".join(roles) if roles else "BACKUP" if prov == "ANTHROPIC" else "NONE"
                    cursor.execute("UPDATE provider_health SET current_role = ? WHERE provider = ?", (role_str, prov))
                conn.commit()
        except Exception:
            pass

        return assigned

    def update_latency(self, provider: str, latency_sec: float):
        try:
            with self._get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE provider_health 
                    SET last_latency_sec = ?
                    WHERE provider = ?
                ''', (latency_sec, provider.upper()))
                conn.commit()
        except Exception:
            pass

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from orch_kernel import WorktreeOwnershipRegistry
from provider_health_state import (
    clear_provider as clear_global_provider,
    mark_quota_exhausted as mark_global_quota_exhausted,
    provider_status as global_provider_status,
    provider_unavailable as global_provider_unavailable,
)

HOME = Path.home()
BASE = HOME / ".local/share/orchbridge"
PROJECT_BASE = Path(os.getenv("AI_ORCH_PROJECT_BASE", str(BASE))).expanduser().resolve()
PHASE1_DIR = BASE / "phase1"
DELEGATIONS_DIR = PROJECT_BASE / "delegations"
WORKTREES_DIR = PROJECT_BASE / "worktrees"
JOBS_DIR = PROJECT_BASE / "jobs"
CACHE_DIR = HOME / ".cache/orchbridge"

DB_PATH = DELEGATIONS_DIR / "registry.sqlite3"
EVENTS_PATH = DELEGATIONS_DIR / "events.jsonl"
QUOTA_LEDGER_PATH = DELEGATIONS_DIR / "quota-ledger.jsonl"
GLOBAL_FACT_LEDGER = DELEGATIONS_DIR / "fact-ledger.jsonl"
PROVIDER_HEALTH_PATH = BASE / "delegations/provider-health.json"
WORKTREE_OWNERSHIP = WorktreeOwnershipRegistry(PROJECT_BASE / "delegations")

DEFAULT_TIMEOUT = int(os.environ.get("AI_ORCH_DELEGATE_TIMEOUT", "1200"))
DEDUP_TTL = int(os.environ.get("AI_ORCH_DEDUP_TTL", "1800"))
QUOTA_RESERVE = float(os.environ.get("AI_ORCH_DELEGATE_QUOTA_RESERVE", "3"))
LEASE_SECONDS = int(os.environ.get("AI_ORCH_DELEGATE_LEASE_SECONDS", "90"))
HEARTBEAT_SECONDS = int(os.environ.get("AI_ORCH_DELEGATE_HEARTBEAT_SECONDS", "15"))

TARGETS = {
    "codex": {"backend": "codex", "model": None, "label": "GPT-6 Luna"},
    "cmd": {
        "backend": "cmd",
        "model": "xiaomi/mimo-v2.5-pro",
        "label": "MiMo V2.5 Pro",
    },
    "sonnet": {
        "backend": "agy",
        "model": "claude-sonnet-5-5",
        "label": "Claude Sonnet 5.5",
    },
    "opus": {
        "backend": "agy",
        "model": "claude-opus-5-5",
        "label": "Claude Opus 5.5",
    },
    "gemini-low": {
        "backend": "agy",
        "model": "gemini-3.8-flash-low",
        "label": "Gemini 3.8 Flash low",
    },
    "gemini-medium": {
        "backend": "agy",
        "model": "gemini-3.8-flash-medium",
        "label": "Gemini 3.8 Flash medium",
    },
    "gemini-high": {
        "backend": "agy",
        "model": "gemini-3.8-flash-high",
        "label": "Gemini 3.8 Flash high",
    },
}


@dataclass(frozen=True)
class ProviderAdapter:
    target: str
    backend: str
    model: str | None
    label: str

    @classmethod
    def from_target(cls, target: str) -> "ProviderAdapter":
        spec = TARGETS[target]
        return cls(
            target=target,
            backend=str(spec["backend"]),
            model=spec.get("model"),
            label=str(spec["label"]),
        )

    def invoke(
        self,
        envelope: "TaskEnvelope",
        worktree: Path,
        prompt: str,
    ) -> tuple[int, str, str, str]:
        env = _provider_env(envelope)
        if self.backend == "codex":
            return _run_codex(worktree, prompt, envelope.timeout_seconds, env)
        if self.backend == "agy":
            assert isinstance(self.model, str)
            return _run_agy(worktree, prompt, self.model, envelope.timeout_seconds, env)
        if self.backend == "cmd":
            assert isinstance(self.model, str)
            return _run_cmd(worktree, prompt, self.model, envelope.timeout_seconds, env)
        raise RuntimeError(f"unsupported provider backend: {self.backend}")


CALLER_BACKENDS = {
    "codex": "codex",
    "cmd": "cmd",
    "sonnet": "agy",
    "opus": "agy",
    "gemini-low": "agy",
    "gemini-medium": "agy",
    "gemini-high": "agy",
}


def _caller_backend(caller: str) -> str | None:
    c = (caller or "").strip().lower()
    if c in CALLER_BACKENDS:
        return CALLER_BACKENDS[c]
    if "codex" in c or "gpt-6" in c:
        return "codex"
    if "mimo" in c or "command code" in c or c == "cmd":
        return "cmd"
    if any(x in c for x in ("sonnet", "opus", "gemini", "agy", "claude")):
        return "agy"
    return None


def _job_delegate_budget() -> int:
    try:
        return max(1, min(8, int(os.environ.get("AI_ORCH_MAX_DELEGATES_PER_JOB", "3"))))
    except Exception:
        return 3


def _delegates_used_for_job(con: sqlite3.Connection, parent_job_id: str) -> int:
    row = con.execute(
        """
        SELECT COUNT(*)
        FROM delegations
        WHERE parent_job_id = ?
          AND worker_id NOT LIKE 'quota-%'
          AND status NOT IN ('BLOCKED', 'CANCELLED')
        """,
        (parent_job_id,),
    ).fetchone()
    return int(row[0] if row else 0)



def _load_provider_health() -> dict[str, Any]:
    try:
        data = json.loads(PROVIDER_HEALTH_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_provider_health(data: dict[str, Any]) -> None:
    _atomic_json(PROVIDER_HEALTH_PATH, data)


def _health_blocked(target: str) -> tuple[bool, dict[str, Any] | None]:
    if target == "cmd" and global_provider_unavailable("commandcode"):
        g = global_provider_status("commandcode")
        return True, {**g, "global": True, "status": "QUOTA_EXHAUSTED"}
    data = _load_provider_health()
    item = data.get(target)
    if not isinstance(item, dict):
        return False, None
    until = float(item.get("blocked_until") or 0)
    if until <= _now():
        return False, item
    return True, item


def _health_mark_success(target: str) -> None:
    if target == "cmd":
        clear_global_provider("commandcode", source="delegate_success")
    data = _load_provider_health()
    if target in data:
        data[target] = {
            "status": "HEALTHY",
            "blocked_until": 0,
            "failure_class": None,
            "failure_count": 0,
            "updated_at": _now(),
            "updated_at_iso": _iso(),
        }
        _save_provider_health(data)


def _health_mark_failure(target: str, failure_class: str | None) -> None:
    if not failure_class:
        return
    if target == "cmd" and failure_class == "CREDIT_EXHAUSTED":
        mark_global_quota_exhausted(
            "commandcode",
            reason="credit/quota exhausted",
            source="delegate",
            retry_after_seconds=3600,
        )
        data = _load_provider_health()
        old = data.get(target, {}) if isinstance(data.get(target), dict) else {}
        data[target] = {
            "status": "QUOTA_EXHAUSTED",
            "blocked_until": 0,
            "failure_class": failure_class,
            "failure_count": int(old.get("failure_count") or 0) + 1,
            "updated_at": _now(),
            "updated_at_iso": _iso(),
        }
        _save_provider_health(data)
        return

    cooldowns = {
        "RATE_LIMIT": 15 * 60,
        "AUTH": 10 * 60,
        "PERMISSION": 10 * 60,
        "NETWORK": 2 * 60,
        "PROVIDER_ERROR": 5 * 60,
    }
    seconds = cooldowns.get(failure_class, 5 * 60)

    data = _load_provider_health()
    old = data.get(target, {}) if isinstance(data.get(target), dict) else {}
    count = int(old.get("failure_count") or 0) + 1

    # Small bounded backoff for repeated non-quota failures.
    if failure_class not in {"RATE_LIMIT", "AUTH", "PERMISSION"}:
        seconds = min(15 * 60, seconds * min(count, 3))

    data[target] = {
        "status": "COOLDOWN",
        "blocked_until": _now() + seconds,
        "failure_class": failure_class,
        "failure_count": count,
        "updated_at": _now(),
        "updated_at_iso": _iso(),
    }
    _save_provider_health(data)



def _quota_viable(target: str) -> tuple[bool, float | None]:
    snap = _quota_snapshot(target)
    rem = snap.get("remaining_pct")
    if rem is None:
        return True, None
    try:
        value = float(rem)
    except Exception:
        return True, None
    return value > QUOTA_RESERVE, value


def _recent_target_counts(limit: int = 18) -> dict[str, int]:
    if not DB_PATH.exists():
        return {}
    try:
        con = sqlite3.connect(DB_PATH)
        try:
            rows = con.execute(
                """
                SELECT target FROM delegations
                WHERE worker_id NOT LIKE 'quota-%'
                ORDER BY started_at DESC LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return {}
    counts: dict[str, int] = {}
    for row in rows:
        target = str(row[0] or "")
        if target:
            counts[target] = counts.get(target, 0) + 1
    return counts


def _delegate_task_role(task: str) -> str:
    text = str(task or "").casefold()
    review_terms = (
        "review", "audit", "verify", "verification", "second opinion", "independent",
        "evidence", "security", "risk", "regression", "proof",
        "검토", "감사", "검증", "독립", "근거", "보안", "리스크",
    )
    research_terms = (
        "research", "compare", "survey", "investigate", "sources", "benchmark",
        "조사", "비교", "자료", "리서치", "벤치마크",
    )
    implementation_terms = (
        "implement", "fix", "debug", "patch", "refactor", "code", "test", "build",
        "구현", "수정", "디버그", "리팩터", "코드", "테스트", "빌드",
    )
    if any(term in text for term in review_terms):
        return "review"
    if any(term in text for term in research_terms):
        return "research"
    if any(term in text for term in implementation_terms):
        return "implementation"
    return "general"


def _task_affinity(task: str, target: str) -> float:
    raw = hashlib.sha256((str(task or "") + "\0" + target).encode("utf-8")).digest()
    return int.from_bytes(raw[:2], "big") / 65535.0


def _auto_target(caller: str, task: str = "") -> str:
    caller_backend = _caller_backend(caller)
    role = _delegate_task_role(task)
    role_capability = {
        "review": {
            "sonnet": .98, "opus": 1.00, "codex": .94, "gemini-high": .88,
            "gemini-medium": .79, "cmd": .72, "gemini-low": .66,
        },
        "research": {
            "gemini-high": .95, "sonnet": .93, "codex": .90, "gemini-medium": .86,
            "cmd": .76, "gemini-low": .75, "opus": .98,
        },
        "implementation": {
            "codex": .97, "sonnet": .92, "cmd": .89, "gemini-high": .85,
            "gemini-medium": .80, "gemini-low": .72, "opus": .98,
        },
        "general": {
            "codex": .95, "sonnet": .92, "cmd": .86, "gemini-high": .86,
            "gemini-medium": .80, "gemini-low": .73, "opus": .98,
        },
    }[role]
    costs = {"cmd": 1, "gemini-low": 1, "gemini-medium": 2, "sonnet": 3,
             "gemini-high": 3, "codex": 4, "opus": 9}
    order = ["sonnet", "gemini-high", "cmd", "gemini-medium", "gemini-low", "codex"]
    if str(os.environ.get("AI_ORCH_AUTO_INCLUDE_OPUS", "0")) == "1":
        order.append("opus")
    recent = _recent_target_counts()
    ranked: list[tuple[float, str]] = []
    for base_rank, target in enumerate(order):
        if target == caller:
            continue
        if caller_backend and TARGETS[target]["backend"] == caller_backend:
            continue
        blocked, _ = _health_blocked(target)
        if blocked:
            continue
        ok, rem = _quota_viable(target)
        if not ok:
            continue
        cap = float(role_capability.get(target, .70))
        score = (
            2.8 * float(recent.get(target, 0))
            + 3.0 * (1.0 - cap)
            + 0.045 * float(costs.get(target, 3))
            + 0.025 * float(base_rank)
            - (0.0 if rem is None else min(.85, max(0.0, float(rem)) / 100.0))
            - .38 * _task_affinity(task, target)
            - (.38 if role == "review" and target == "sonnet" else 0.0)
        )
        ranked.append((score, target))
    if ranked:
        ranked.sort(key=lambda row: (row[0], row[1]))
        chosen = ranked[0][1]
        print(
            f"[delegate] auto-select role={role} -> {chosen} (recent={recent.get(chosen, 0)})",
            file=sys.stderr, flush=True,
        )
        return chosen
    raise RuntimeError("NO_CROSS_PROVIDER_TARGET_WITH_QUOTA")


@dataclass
class TaskEnvelope:
    task_id: str
    parent_job_id: str
    caller: str
    target: str
    depth: int
    mode: str
    task: str
    repo: str
    repo_head: str
    repo_tree: str
    branch: str
    timeout_seconds: int
    fingerprint: str
    created_at: float
    constraints: list[str] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)


@dataclass
class WorkerResult:
    status: str
    summary: str
    findings: list[Any] = field(default_factory=list)
    evidence: list[Any] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    tests: list[Any] = field(default_factory=list)
    blockers: list[Any] = field(default_factory=list)
    recommended_next: list[Any] = field(default_factory=list)
    mechanical_verification: dict[str, Any] = field(default_factory=dict)
    worker_id: str | None = None
    target: str | None = None
    requested_target: str | None = None
    model: str | None = None
    mode: str | None = None
    branch: str | None = None
    worktree: str | None = None
    duration_seconds: float | None = None
    raw_response_path: str | None = None
    quota_before: dict[str, Any] = field(default_factory=dict)
    quota_after: dict[str, Any] = field(default_factory=dict)
    quota_before_summary: str | None = None
    quota_summary: str | None = None
    failure_class: str | None = None
    cache_hit: bool = False


def _now() -> float:
    return time.time()


def _iso(ts: float | None = None) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts or _now()).astimezone().isoformat(timespec="seconds")


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)


def _append_jsonl(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def _terminate_process_group(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            return
    try:
        proc.wait(timeout=2)
        return
    except Exception:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: int = 30,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        _terminate_process_group(proc)
        stdout, stderr = proc.communicate()
        raise subprocess.TimeoutExpired(
            argv,
            timeout,
            output=stdout or getattr(e, "output", None),
            stderr=stderr or getattr(e, "stderr", None),
        )
    except KeyboardInterrupt:
        _terminate_process_group(proc)
        proc.communicate()
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def _git(repo: Path, *args: str, timeout: int = 20) -> str:
    p = _run(["git", *args], cwd=repo, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout.rstrip()


def _repo_root(path: Path) -> Path:
    p = _run(["git", "rev-parse", "--show-toplevel"], cwd=path, timeout=10)
    if p.returncode != 0:
        raise RuntimeError(f"not a git repository: {path}")
    return Path(p.stdout.strip()).resolve()


def _repo_snapshot(repo: Path) -> dict[str, Any]:
    return {
        "head": _git(repo, "rev-parse", "HEAD"),
        "tree": _git(repo, "rev-parse", "HEAD^{tree}"),
        "branch": _git(repo, "branch", "--show-current") or "detached",
        "status": _git(repo, "status", "--porcelain=v1", "--untracked-files=all"),
    }


def _normalize_task(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _fingerprint(
    *,
    repo_head: str,
    target: str,
    mode: str,
    task: str,
) -> str:
    blob = json.dumps(
        {
            "head": repo_head,
            "target": target,
            "mode": mode,
            "task": _normalize_task(task),
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(blob).hexdigest()



def _logical_fingerprint(
    *,
    repo_head: str,
    mode: str,
    task: str,
) -> str:
    """Provider-independent exact logical task identity."""
    blob = json.dumps(
        {
            "head": repo_head,
            "mode": mode,
            "task": _normalize_task(task),
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(blob).hexdigest()



def _init_db() -> sqlite3.Connection:
    DELEGATIONS_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS delegations (
            fingerprint TEXT PRIMARY KEY,
            worker_id TEXT NOT NULL,
            parent_job_id TEXT NOT NULL,
            caller TEXT NOT NULL,
            target TEXT NOT NULL,
            mode TEXT NOT NULL,
            repo_head TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at REAL NOT NULL,
            ended_at REAL,
            result_path TEXT,
            task_preview TEXT,
            logical_fingerprint TEXT
        )
        """
    )

    cols = {
        row[1]
        for row in con.execute("PRAGMA table_info(delegations)").fetchall()
    }
    if "logical_fingerprint" not in cols:
        con.execute(
            "ALTER TABLE delegations ADD COLUMN logical_fingerprint TEXT"
        )

    con.execute(
        """
        CREATE INDEX IF NOT EXISTS delegations_parent_idx
        ON delegations(parent_job_id, started_at DESC)
        """
    )
    con.execute(
        """
        CREATE INDEX IF NOT EXISTS delegations_logical_idx
        ON delegations(logical_fingerprint, status, started_at DESC)
        """
    )
    con.commit()
    return con


def _emit_event(
    event: str,
    *,
    parent_job_id: str,
    worker_id: str,
    target: str,
    mode: str,
    status: str | None = None,
    summary: str | None = None,
    duration: float | None = None,
    result_path: str | None = None,
    quota_summary: str | None = None,
    quota_before_summary: str | None = None,
    quota_after: dict[str, Any] | None = None,
) -> None:
    payload = {
        "ts": _iso(),
        "epoch": _now(),
        "event": event,
        "parent_job_id": parent_job_id,
        "worker_id": worker_id,
        "target": target,
        "target_label": TARGETS.get(target, {}).get("label", target),
        "mode": mode,
        "status": status,
        "summary": summary,
        "duration_seconds": duration,
        "result_path": result_path,
        "quota_summary": quota_summary,
        "quota_before_summary": quota_before_summary,
        "quota_after": quota_after,
    }
    _append_jsonl(EVENTS_PATH, payload)


def _parent_job_id() -> str:
    env = os.environ.get("AI_ORCH_JOB_ID", "").strip()
    if env:
        return env
    return "adhoc-" + time.strftime("%Y%m%d-%H%M%S")


def _fact_ledger_path(parent_job_id: str) -> Path:
    candidate = JOBS_DIR / parent_job_id
    if candidate.is_dir():
        return candidate / "fact-ledger.jsonl"
    return GLOBAL_FACT_LEDGER


def _append_fact(
    parent_job_id: str,
    *,
    claim: str,
    status: str,
    source_type: str,
    source: str,
    worker_id: str,
    repo_head: str,
    repo_tree: str | None = None,
    evidence: Any = None,
) -> None:
    _append_jsonl(
        _fact_ledger_path(parent_job_id),
        {
            "ts": _iso(),
            "claim": claim,
            "status": status,
            "source_type": source_type,
            "source": source,
            "worker_id": worker_id,
            "repo_head": repo_head,
            "repo_tree": repo_tree,
            "verified_at": _now(),
            "verified_at_iso": _iso(),
            "evidence": evidence,
        },
    )


def _quota_snapshot(target: str) -> dict[str, Any]:
    def load(path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}

    if target == "codex":
        d = load(CACHE_DIR / "codex-usage.json")
        vals = [
            d.get("five_hour_remaining_pct"),
            d.get("weekly_remaining_pct"),
        ]
        numeric = [float(x) for x in vals if x is not None]
        return {
            "source": "codex-usage.json",
            "remaining_pct": min(numeric) if numeric else None,
            "raw": {
                "five_hour_remaining_pct": d.get("five_hour_remaining_pct"),
                "weekly_remaining_pct": d.get("weekly_remaining_pct"),
            },
        }

    if target == "cmd":
        d = load(CACHE_DIR / "cmd-usage.json")
        vals = [
            d.get("monthly_remaining_pct"),
            d.get("five_hour_remaining_pct"),
            d.get("weekly_remaining_pct"),
        ]
        numeric = [float(x) for x in vals if x is not None]
        return {
            "source": "cmd-usage.json",
            "remaining_pct": min(numeric) if numeric else None,
            "raw": {
                "monthly_remaining_pct": d.get("monthly_remaining_pct"),
                "five_hour_remaining_pct": d.get("five_hour_remaining_pct"),
                "weekly_remaining_pct": d.get("weekly_remaining_pct"),
            },
        }

    d = load(CACHE_DIR / "agy-status.json")
    quota = d.get("quota", {}) if isinstance(d, dict) else {}
    if target.startswith("gemini-"):
        keys = ("gemini-5h", "gemini-weekly")
    else:
        keys = ("3p-5h", "3p-weekly")

    values = []
    raw = {}
    for key in keys:
        item = quota.get(key, {}) if isinstance(quota, dict) else {}
        frac = item.get("remaining_fraction") if isinstance(item, dict) else None
        raw[key] = frac
        if frac is not None:
            try:
                values.append(float(frac) * 100.0)
            except Exception:
                pass

    return {
        "source": "agy-status.json",
        "remaining_pct": min(values) if values else None,
        "raw": raw,
    }


def _quota_allowed(target: str, *, ignore: bool) -> tuple[bool, dict[str, Any]]:
    snap = _quota_snapshot(target)
    rem = snap.get("remaining_pct")
    if ignore or rem is None:
        return True, snap
    return float(rem) > QUOTA_RESERVE, snap


def _refresh_target_quota(target: str) -> dict[str, Any]:
    helper = HOME / ".local/bin/ai-model-quota"
    if not helper.exists():
        snap = _quota_snapshot(target)
        return {
            "target": target,
            "summary": (
                f"remaining {snap.get('remaining_pct'):.0f}%"
                if snap.get("remaining_pct") is not None
                else "quota unavailable"
            ),
            "remaining_pct": snap.get("remaining_pct"),
            "fresh": False,
            "raw": snap.get("raw", {}),
            "reason": "ai-model-quota helper missing",
        }

    try:
        p = _run(
            [str(helper), target, "--json"],
            timeout=80,
        )
        rows = [x for x in p.stdout.splitlines() if x.strip()]
        if rows:
            data = json.loads(rows[-1])
            if isinstance(data, dict):
                return data
    except Exception as e:
        return {
            "target": target,
            "summary": "quota refresh failed",
            "remaining_pct": None,
            "fresh": False,
            "reason": str(e),
        }

    return {
        "target": target,
        "summary": "quota refresh unavailable",
        "remaining_pct": None,
        "fresh": False,
    }


def _quota_summary(snapshot: dict[str, Any]) -> str:
    raw = snapshot.get("raw", {}) if isinstance(snapshot, dict) else {}
    pieces: list[str] = []

    month = raw.get("monthly_remaining_pct")
    five = raw.get("five_hour_remaining_pct")
    week = raw.get("weekly_remaining_pct")
    if month is not None:
        pieces.append(f"month {float(month):.0f}%")
    if five is not None:
        pieces.append(f"5h {float(five):.0f}%")
    if week is not None:
        pieces.append(f"wk {float(week):.0f}%")

    if not pieces:
        seen = set()
        for key, short in (
            ("3p-5h", "5h"),
            ("3p-weekly", "wk"),
            ("gemini-5h", "5h"),
            ("gemini-weekly", "wk"),
        ):
            frac = raw.get(key)
            if frac is not None and short not in seen:
                pieces.append(f"{short} {float(frac) * 100:.0f}%")
                seen.add(short)

    rem = snapshot.get("remaining_pct") if isinstance(snapshot, dict) else None
    if not pieces and rem is not None:
        pieces.append(f"effective {float(rem):.0f}%")
    return " · ".join(pieces) if pieces else "quota unavailable"


def _quota_ledger(
    *,
    envelope: TaskEnvelope,
    worker_id: str,
    before: dict[str, Any],
    after: dict[str, Any],
    duration: float,
) -> None:
    _append_jsonl(
        QUOTA_LEDGER_PATH,
        {
            "ts": _iso(),
            "parent_job_id": envelope.parent_job_id,
            "worker_id": worker_id,
            "caller": envelope.caller,
            "target": envelope.target,
            "mode": envelope.mode,
            "quota_before": before,
            "quota_after": after,
            "duration_seconds": round(duration, 3),
        },
    )


def _safe_id(text: str, limit: int = 40) -> str:
    clean = re.sub(r"[^a-zA-Z0-9._-]+", "-", text).strip("-_.")
    return (clean or "job")[:limit]



def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _lease_path(run_dir: Path) -> Path:
    return run_dir / "lease.json"


def _write_lease(
    run_dir: Path,
    *,
    worker_id: str,
    parent_job_id: str,
    target: str,
    state: str,
) -> None:
    now = _now()
    _atomic_json(
        _lease_path(run_dir),
        {
            "worker_id": worker_id,
            "parent_job_id": parent_job_id,
            "target": target,
            "pid": os.getpid(),
            "state": state,
            "heartbeat_at": now,
            "heartbeat_at_iso": _iso(now),
            "lease_until": now + LEASE_SECONDS,
            "lease_until_iso": _iso(now + LEASE_SECONDS),
        },
    )


def _start_lease_heartbeat(
    run_dir: Path,
    *,
    worker_id: str,
    parent_job_id: str,
    target: str,
) -> tuple[threading.Event, threading.Thread]:
    stop = threading.Event()

    def beat() -> None:
        while not stop.is_set():
            try:
                _write_lease(
                    run_dir,
                    worker_id=worker_id,
                    parent_job_id=parent_job_id,
                    target=target,
                    state="RUNNING",
                )
            except Exception:
                pass
            stop.wait(max(3, HEARTBEAT_SECONDS))

    thread = threading.Thread(
        target=beat,
        name=f"orch-lease-{worker_id}",
        daemon=True,
    )
    thread.start()
    return stop, thread


def _find_run_dir(worker_id: str) -> Path | None:
    for p in DELEGATIONS_DIR.glob(f"*/{worker_id}"):
        if p.is_dir():
            return p
    return None


def _recover_stale_running(
    con: sqlite3.Connection,
    repo: Path,
) -> list[dict[str, Any]]:
    recovered: list[dict[str, Any]] = []
    rows = con.execute(
        """
        SELECT fingerprint, worker_id, parent_job_id, target, mode, started_at
        FROM delegations
        WHERE status = 'RUNNING'
        """
    ).fetchall()

    for fingerprint, worker_id, parent_job_id, target, mode, started_at in rows:
        run_dir = _find_run_dir(str(worker_id))
        lease = {}
        if run_dir and _lease_path(run_dir).exists():
            try:
                lease = json.loads(_lease_path(run_dir).read_text())
            except Exception:
                lease = {}

        pid = int(lease.get("pid") or 0)
        lease_until = float(lease.get("lease_until") or 0)
        alive = _pid_alive(pid)

        # Give old v2.x rows without a lease a grace period before recovery.
        no_lease_stale = (
            not lease
            and (_now() - float(started_at or 0)) > max(LEASE_SECONDS * 2, 180)
        )
        expired = bool(lease and lease_until < _now() and not alive)

        if not (no_lease_stale or expired):
            continue

        con.execute(
            """
            UPDATE delegations
            SET status = 'ABANDONED', ended_at = ?
            WHERE fingerprint = ? AND status = 'RUNNING'
            """,
            (_now(), fingerprint),
        )
        con.commit()

        _emit_event(
            "LEASE_EXPIRED",
            parent_job_id=str(parent_job_id),
            worker_id=str(worker_id),
            target=str(target),
            mode=str(mode),
            status="ABANDONED",
            summary="Stale RUNNING delegate recovered after lease expiry.",
        )

        envelope = _find_envelope_for_worker(str(worker_id))
        worktree = None
        if envelope:
            candidate = (
                WORKTREES_DIR
                / _safe_id(str(parent_job_id))
                / _safe_id(str(worker_id))
            )
            if candidate.exists():
                worktree = candidate

        # Only disposable read-only worktrees are auto-cleaned.
        if str(mode) == "read_only" and worktree is not None:
            try:
                _remove_worktree(repo, worktree)
            except Exception:
                pass

        recovered.append(
            {
                "worker_id": worker_id,
                "parent_job_id": parent_job_id,
                "target": target,
                "mode": mode,
                "reason": "LEASE_EXPIRED" if expired else "LEGACY_STALE_RUNNING",
                "pid": pid or None,
            }
        )

    return recovered



def _set_tree_read_only(path: Path, read_only: bool) -> None:
    """
    Best-effort filesystem hardening for disposable read-only workers.
    Stronger than prompt-only isolation, but not a kernel sandbox.
    """
    if not path.exists():
        return
    entries: list[Path] = []
    for root, dirs, files in os.walk(path, topdown=True, followlinks=False):
        base = Path(root)
        entries.extend(base / name for name in files)
        entries.extend(base / name for name in dirs)
    entries.append(path)
    if not read_only:
        entries = list(reversed(entries))
    for item in entries:
        try:
            if item.is_symlink():
                continue
            mode = stat.S_IMODE(item.stat().st_mode)
            if read_only:
                new_mode = mode & ~0o222
            else:
                new_mode = mode | 0o200
                if item.is_dir():
                    new_mode |= 0o100
            os.chmod(item, new_mode)
        except (FileNotFoundError, PermissionError):
            continue


def _find_envelope_for_worker(worker_id: str) -> dict[str, Any] | None:
    for p in DELEGATIONS_DIR.glob(f"*/{worker_id}/task-envelope.json"):
        try:
            return json.loads(p.read_text())
        except Exception:
            return None
    return None


def _cleanup_stale_worktrees(
    repo: Path,
    *,
    max_age_seconds: int = 7200,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    con = _init_db()
    cleaned: list[dict[str, Any]] = []
    cutoff = _now() - max_age_seconds

    for path in WORKTREES_DIR.glob("*/*"):
        if not path.is_dir():
            continue

        worker_id = path.name
        envelope = _find_envelope_for_worker(worker_id)
        if not envelope or envelope.get("mode") != "read_only":
            continue

        row = con.execute(
            "SELECT status, started_at, ended_at FROM delegations WHERE worker_id = ?",
            (worker_id,),
        ).fetchone()
        if not row:
            continue

        status, started_at, ended_at = row
        if str(status) == "RUNNING":
            continue

        terminal_at = float(ended_at or started_at or 0)
        if terminal_at > cutoff:
            continue

        item = {
            "worker_id": worker_id,
            "path": str(path),
            "status": status,
            "age_seconds": int(_now() - terminal_at),
            "dry_run": dry_run,
        }
        cleaned.append(item)

        if not dry_run:
            _set_tree_read_only(path, False)
            _remove_worktree(repo, path)

    if not dry_run:
        _run(["git", "worktree", "prune"], cwd=repo, timeout=30)
    return cleaned


def _prepare_worktree(
    repo: Path,
    *,
    parent_job_id: str,
    worker_id: str,
    mode: str,
    head: str,
) -> tuple[Path, str | None, bool]:
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)

    job_seg = _safe_id(parent_job_id)
    worker_seg = _safe_id(worker_id)
    path = (WORKTREES_DIR / job_seg / worker_seg).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        raise RuntimeError(
            f"WORKTREE_PATH_OCCUPIED: refusing to delete unproven existing path: {path}"
        )

    if mode == "read_only":
        p = _run(
            ["git", "worktree", "add", "--detach", str(path), head],
            cwd=repo,
            timeout=60,
        )
        if p.returncode != 0:
            raise RuntimeError(f"read-only worktree add failed: {p.stderr.strip()}")
        _set_tree_read_only(path, True)
        WORKTREE_OWNERSHIP.register({
            "worktree_id": f"{job_seg}/{worker_seg}",
            "owner_job_id": parent_job_id,
            "owner_agent_id": worker_id,
            "repo_id": str(_repo_root(repo)),
            "base_commit": head,
            "branch": None,
            "path": str(path),
            "mode": "read_only",
            "lifecycle": "LEASE_ACTIVE",
            "cleanup_state": "ACTIVE",
        })
        return path, None, True

    branch = f"orch/{job_seg}/{worker_seg}"
    p = _run(
        ["git", "worktree", "add", "-b", branch, str(path), head],
        cwd=repo,
        timeout=60,
    )
    if p.returncode != 0:
        raise RuntimeError(f"write worktree add failed: {p.stderr.strip()}")
    WORKTREE_OWNERSHIP.register({
        "worktree_id": f"{job_seg}/{worker_seg}",
        "owner_job_id": parent_job_id,
        "owner_agent_id": worker_id,
        "repo_id": str(_repo_root(repo)),
        "base_commit": head,
        "branch": branch,
        "path": str(path),
        "mode": "write",
        "lifecycle": "LEASE_ACTIVE",
        "cleanup_state": "PRESERVE_UNTIL_PROVEN",
    })
    return path, branch, False


def _remove_worktree(repo: Path, path: Path) -> None:
    _set_tree_read_only(path, False)
    _run(
        ["git", "worktree", "remove", "--force", str(path)],
        cwd=repo,
        timeout=60,
    )
    _run(["git", "worktree", "prune"], cwd=repo, timeout=30)


def _governance_text(repo: Path) -> str:
    p = repo / "AGENTS.md"
    if not p.exists():
        return "(No AGENTS.md found in the main checkout.)"
    try:
        text = p.read_text()
    except Exception:
        return "(AGENTS.md could not be read.)"
    return text[:20000]


def _job_multimodal(parent_job_id: str) -> tuple[list[str], list[str]]:
    p = JOBS_DIR / parent_job_id / "job.json"
    try:
        d = json.loads(p.read_text())
        return ([str(x) for x in d.get("attachments", []) if x], [str(x) for x in d.get("skills", []) if x][:3])
    except Exception:
        return ([], [])

def _job_context(parent_job_id: str) -> str:
    tool = HOME / ".local/bin/orch-context"
    if not tool.exists(): return ""
    try:
        p = subprocess.run([str(tool), "build", "--job", parent_job_id], capture_output=True, text=True, timeout=15, env={**os.environ.copy(), "AI_ORCH_PROJECT_BASE": str(PROJECT_BASE)})
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""

def _worker_prompt(
    envelope: TaskEnvelope,
    *,
    isolated_worktree: Path,
    main_repo: Path,
) -> str:
    governance = _governance_text(main_repo)
    orch_context = _job_context(envelope.parent_job_id)

    mode_text = (
        "READ-ONLY investigation. The orchestrator created a disposable isolated "
        "worktree. Do not intentionally modify the authoritative main checkout. "
        "If you accidentally modify the disposable worktree, those changes will "
        "be discarded and reported."
        if envelope.mode == "read_only"
        else
        "WRITE worker. You have a dedicated worktree and branch. Modify only this "
        "worktree. Do not merge/cherry-pick/push. The supervisor will inspect and "
        "integrate your result separately."
    )

    return f"""You are a delegated worker in a local multi-provider coding orchestrator.

WORKER ID: {envelope.task_id}
PARENT JOB: {envelope.parent_job_id}
CALLER: {envelope.caller}
TARGET: {envelope.target}
DEPTH: {envelope.depth}
MODE: {envelope.mode}

Repository snapshot:
- authoritative main checkout: {main_repo}
- worker checkout: {isolated_worktree}
- starting HEAD: {envelope.repo_head}
- starting tree: {envelope.repo_tree}
- starting branch: {envelope.branch}

Permission / execution contract:
- {mode_text}
- You are a worker, not the supervisor.
- You MUST NOT call orch-delegate, orch-consult, orch-parallel, or orch-handoff, and MUST NOT delegate/orchestrate another worker. You may use orch-collect only to read an already-created collection packet when directly relevant.
- Do not reinterpret worker output as authoritative project truth.
- Do not weaken project safety/governance rules.
- Do not perform user-GO-gated actions.
- Prefer concrete repo/tool evidence over unsupported inference.
- If blocked, return BLOCKED with the exact blocker rather than improvising.

Authoritative project governance supplied from the main checkout:
--- AGENTS.md ---
{governance}
--- END AGENTS.md ---

ORCHESTRATOR JOB CONTEXT:
--- CONTEXT ---
{orch_context or "(No attachment/skill context.)"}
--- END CONTEXT ---

Delegated task:
--- TASK ---
{envelope.task}
--- END TASK ---

Your FINAL response must end with exactly this marker and one valid JSON object:

ORCH_WORKER_RESULT
{{
  "status": "COMPLETE|PARTIAL|BLOCKED|FAILED",
  "summary": "short summary",
  "findings": [
    {{
      "claim": "claim",
      "confidence": "HIGH|MEDIUM|LOW",
      "evidence": ["file/line/tool evidence"]
    }}
  ],
  "evidence": [],
  "files_changed": [],
  "tests": [
    {{
      "command": "exact test command if actually run",
      "exit_code": 0
    }}
  ],
  "blockers": [],
  "recommended_next": []
}}

Do not put prose after the JSON object.
"""


def _provider_env(envelope: TaskEnvelope) -> dict[str, str]:
    env = os.environ.copy()
    env["AI_ORCH_DELEGATION_DEPTH"] = "1"
    env["AI_ORCH_WORKER_ID"] = envelope.task_id
    env["AI_ORCH_PARENT_JOB"] = envelope.parent_job_id
    env["AI_ORCH_MAIN_MODEL"] = envelope.target
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run_codex(
    cwd: Path,
    prompt: str,
    timeout: int,
    env: dict[str, str],
) -> tuple[int, str, str, str]:
    argv = [
        "codex",
        "exec",
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "-",
    ]
    p = _run(argv, cwd=cwd, input_text=prompt, timeout=timeout, env=env)
    final = ""
    for line in p.stdout.splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
        if obj.get("type") == "item.completed" and item.get("type") == "agent_message":
            text = str(item.get("text", ""))
            if text:
                final = text
    return p.returncode, final, p.stdout, p.stderr


def _run_agy(
    cwd: Path,
    prompt: str,
    model: str,
    timeout: int,
    env: dict[str, str],
) -> tuple[int, str, str, str]:
    argv = ["agy", "-p", prompt, "--model", model]
    if model.startswith("claude-"):
        effort = str(env.get("AI_ORCH_AGY_CLAUDE_EFFORT", "medium")).strip().lower()
        if effort not in {"low", "medium", "high"}:
            effort = "medium"
        argv.extend(["--effort", effort])
    argv.extend([
        "--output-format",
        "stream-json",
        "--dangerously-skip-permissions",
        "--print-timeout",
        f"{max(1, timeout // 60)}m",
    ])
    p = _run(argv, cwd=cwd, timeout=timeout, env=env)
    final = ""
    for line in p.stdout.splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("event") == "result":
            result = obj.get("result", {})
            if isinstance(result, dict):
                text = str(result.get("response", ""))
                if text:
                    final = text
    return p.returncode, final, p.stdout, p.stderr


def _run_cmd(
    cwd: Path,
    prompt: str,
    model: str,
    timeout: int,
    env: dict[str, str],
) -> tuple[int, str, str, str]:
    argv = [
        "cmd",
        "-p",
        prompt,
        "--skip-onboarding",
        "--yolo",
        "--output-format",
        "json",
        "-m",
        model,
    ]
    p = _run(argv, cwd=cwd, timeout=timeout, env=env)
    final = ""
    for line in p.stdout.splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") == "result":
            text = str(obj.get("finalText", ""))
            if text:
                final = text
    return p.returncode, final, p.stdout, p.stderr


def _invoke_provider(
    envelope: TaskEnvelope,
    worktree: Path,
    prompt: str,
) -> tuple[int, str, str, str]:
    return ProviderAdapter.from_target(envelope.target).invoke(
        envelope,
        worktree,
        prompt,
    )


def _classify_failure(
    *,
    rc: int,
    stdout: str,
    stderr: str,
) -> str | None:
    if rc == 0:
        return None
    text = f"{stdout}\n{stderr}".lower()
    if rc == 10 or re.search(r"insufficient credits|credit limit|credits? exhausted|out of credits|no credits", text):
        return "CREDIT_EXHAUSTED"
    if re.search(r"usage limit|rate.?limit|quota|try again at|too many requests", text):
        return "RATE_LIMIT"
    if re.search(r"unauthori[sz]ed|authentication|not logged in|\\b401\\b|\\b403\\b", text):
        return "AUTH"
    if re.search(r"permission denied|not permitted|blocked by policy|approval", text):
        return "PERMISSION"
    if re.search(r"network issue|connection reset|temporar.*unavailable|dns|socket", text):
        return "NETWORK"
    return "PROVIDER_ERROR"


def _extract_worker_json(text: str) -> dict[str, Any] | None:
    marker = "ORCH_WORKER_RESULT"
    candidates: list[str] = []

    if marker in text:
        candidates.append(text.rsplit(marker, 1)[1].strip())

    candidates.append(text.strip())

    decoder = json.JSONDecoder()
    for candidate in candidates:
        # Strip markdown fences if the model ignored the "raw JSON" instruction.
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.I)
        candidate = re.sub(r"\s*```$", "", candidate)

        starts = [m.start() for m in re.finditer(r"\{", candidate)]
        for start in starts:
            try:
                obj, _ = decoder.raw_decode(candidate[start:])
            except Exception:
                continue
            if isinstance(obj, dict) and "status" in obj and "summary" in obj:
                return obj
    return None


def _changed_files(worktree: Path) -> list[str]:
    names: set[str] = set()
    for args in (
        ("diff", "--name-only"),
        ("diff", "--cached", "--name-only"),
    ):
        try:
            out = _git(worktree, *args)
        except Exception:
            continue
        names.update(x.strip() for x in out.splitlines() if x.strip())

    try:
        status = _git(worktree, "status", "--porcelain=v1", "--untracked-files=all")
        for line in status.splitlines():
            if len(line) >= 4:
                value = line[3:]
                if " -> " in value:
                    value = value.split(" -> ", 1)[1]
                names.add(value.strip())
    except Exception:
        pass

    return sorted(x for x in names if x)


def _safe_test_argv(command: str) -> list[str] | None:
    # Do not run model-generated shell syntax through a shell.
    if re.search(r"[;&|><`$()]", command):
        return None
    try:
        argv = shlex.split(command)
    except Exception:
        return None
    if not argv:
        return None

    exe = Path(argv[0]).name
    if exe == "pytest":
        return argv
    if exe in {"python", "python3"} and len(argv) >= 3 and argv[1:3] == ["-m", "pytest"]:
        return argv
    if exe == "uv" and len(argv) >= 3 and argv[1:3] == ["run", "pytest"]:
        return argv
    if exe == "poetry" and len(argv) >= 3 and argv[1:3] == ["run", "pytest"]:
        return argv
    if exe in {"npm", "pnpm", "yarn"} and len(argv) >= 2 and argv[1] in {"test", "run"}:
        return argv
    if exe in {"dart", "flutter"} and len(argv) >= 2 and argv[1] == "test":
        return argv
    if exe == "go" and len(argv) >= 2 and argv[1] == "test":
        return argv
    if exe == "cargo" and len(argv) >= 2 and argv[1] == "test":
        return argv
    return None


def _rerun_declared_tests(
    worktree: Path,
    tests: Any,
    *,
    max_tests: int = 3,
) -> list[dict[str, Any]]:
    verified: list[dict[str, Any]] = []
    if not isinstance(tests, list):
        return verified

    for item in tests[:max_tests]:
        if isinstance(item, str):
            command = item
        elif isinstance(item, dict):
            command = str(item.get("command", ""))
        else:
            continue

        argv = _safe_test_argv(command)
        if not argv:
            verified.append(
                {
                    "command": command,
                    "rerun": False,
                    "reason": "not in safe mechanical test allowlist",
                }
            )
            continue

        try:
            p = _run(argv, cwd=worktree, timeout=300)
            verified.append(
                {
                    "command": command,
                    "rerun": True,
                    "exit_code": p.returncode,
                    "stdout_tail": p.stdout[-2000:],
                    "stderr_tail": p.stderr[-2000:],
                }
            )
        except subprocess.TimeoutExpired:
            verified.append(
                {
                    "command": command,
                    "rerun": True,
                    "timeout": True,
                }
            )
    return verified


def _worker_result_from_payload(
    payload: dict[str, Any] | None,
    *,
    raw_text: str,
) -> WorkerResult:
    if not payload:
        return WorkerResult(
            status="UNSTRUCTURED",
            summary=raw_text[-4000:] if raw_text else "Worker produced no structured result.",
            blockers=["worker output did not satisfy ORCH_WORKER_RESULT schema"],
        )

    def as_list(key: str) -> list[Any]:
        value = payload.get(key, [])
        return value if isinstance(value, list) else [value]

    return WorkerResult(
        status=str(payload.get("status", "PARTIAL")).upper(),
        summary=str(payload.get("summary", "")),
        findings=as_list("findings"),
        evidence=as_list("evidence"),
        files_changed=as_list("files_changed"),
        tests=as_list("tests"),
        blockers=as_list("blockers"),
        recommended_next=as_list("recommended_next"),
    )


def delegate(args: argparse.Namespace) -> int:
    depth = int(os.environ.get("AI_ORCH_DELEGATION_DEPTH", "0") or "0")
    if depth >= 1:
        result = WorkerResult(
            status="BLOCKED",
            summary="Delegated workers cannot delegate another worker.",
            blockers=["MAX_DELEGATION_DEPTH=1"],
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 20

    caller = (args.caller or os.environ.get("AI_ORCH_MAIN_MODEL") or "").strip()
    requested_target = args.model.strip()

    if not caller:
        raise SystemExit("--caller is required when AI_ORCH_MAIN_MODEL is not set")

    if args.task_file:
        task = Path(args.task_file).read_text()
    elif args.task is not None:
        task = args.task
    else:
        task = sys.stdin.read()

    if not task.strip():
        raise SystemExit("delegated task is empty")

    if requested_target == "auto":
        try:
            target = _auto_target(caller, task)
        except Exception as e:
            result = WorkerResult(
                status="BLOCKED",
                summary=f"Auto delegate target selection failed: {e}",
                blockers=["NO_CROSS_PROVIDER_TARGET_WITH_QUOTA"],
                requested_target="auto",
            )
            print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
            return 24
    else:
        target = requested_target

    if target not in TARGETS:
        raise SystemExit(f"unsupported model target: {target}")

    if caller == target:
        result = WorkerResult(
            status="BLOCKED",
            summary=f"Self-call prohibited: {caller} -> {target}",
            blockers=["SELF_CALL_PROHIBITED"],
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 21

    health_blocked, health_state = _health_blocked(target)
    if health_blocked and not args.ignore_health:
        until = _iso(float(health_state.get("blocked_until") or _now()))
        result = WorkerResult(
            status="BLOCKED",
            summary=f"{target} is in provider cooldown until {until}.",
            blockers=["PROVIDER_COOLDOWN"],
            target=target,
            requested_target=requested_target,
            mode=args.mode,
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 26

    repo = _repo_root(Path(args.repo or os.getcwd()))
    base_snapshot = _repo_snapshot(repo)
    parent_job_id = _parent_job_id()

    try:
        _cleanup_stale_worktrees(repo, max_age_seconds=7200, dry_run=False)
    except Exception:
        pass

    fingerprint = _fingerprint(
        repo_head=base_snapshot["head"],
        target=target,
        mode=args.mode,
        task=task,
    )
    logical_fingerprint = _logical_fingerprint(
        repo_head=base_snapshot["head"],
        mode=args.mode,
        task=task,
    )

    con = _init_db()
    _recover_stale_running(con, repo)

    max_delegates = _job_delegate_budget()
    used_delegates = _delegates_used_for_job(con, parent_job_id)
    if used_delegates >= max_delegates:
        result = WorkerResult(
            status="BLOCKED",
            summary=(
                f"Delegation budget exhausted for {parent_job_id}: "
                f"{used_delegates}/{max_delegates}"
            ),
            blockers=["DELEGATION_BUDGET_EXHAUSTED"],
            target=target,
            requested_target=requested_target,
            mode=args.mode,
        )
        worker_id = "budget-" + fingerprint[:10]
        _emit_event(
            "BUDGET_BLOCK",
            parent_job_id=parent_job_id,
            worker_id=worker_id,
            target=target,
            mode=args.mode,
            status="BLOCKED",
            summary=result.summary,
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 25

    if not args.fresh:
        active = con.execute(
            """
            SELECT worker_id, target, parent_job_id, started_at
            FROM delegations
            WHERE logical_fingerprint = ? AND status = 'RUNNING'
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (logical_fingerprint,),
        ).fetchone()
        if active:
            active_worker, active_target, active_parent, active_started = active
            result = WorkerResult(
                status="BLOCKED",
                summary=(
                    "Equivalent logical delegate task is already running "
                    f"as {active_worker} on {active_target}."
                ),
                blockers=["LOGICAL_DEDUP_ACTIVE"],
                worker_id=str(active_worker),
                target=str(active_target),
                requested_target=requested_target,
                mode=args.mode,
            )
            _emit_event(
                "DEDUP_BLOCK",
                parent_job_id=parent_job_id,
                worker_id=str(active_worker),
                target=str(active_target),
                mode=args.mode,
                status="BLOCKED",
                summary=result.summary,
            )
            print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
            return 22

    row = con.execute(
        """
        SELECT worker_id, status, ended_at, result_path
        FROM delegations WHERE fingerprint = ?
        """,
        (fingerprint,),
    ).fetchone()

    if row and not args.fresh:
        worker_id, status, ended_at, result_path = row
        if status == "RUNNING":
            result = WorkerResult(
                status="BLOCKED",
                summary="An identical delegate task is already running.",
                blockers=["DEDUP_ACTIVE"],
                worker_id=worker_id,
                target=target,
                mode=args.mode,
            )
            _emit_event(
                "DEDUP_BLOCK",
                parent_job_id=parent_job_id,
                worker_id=worker_id,
                target=target,
                mode=args.mode,
                status="BLOCKED",
                summary=result.summary,
            )
            print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
            return 22

        if (
            status == "COMPLETE"
            and ended_at
            and (_now() - float(ended_at)) <= DEDUP_TTL
            and result_path
            and Path(result_path).exists()
        ):
            data = json.loads(Path(result_path).read_text())
            data["cache_hit"] = True
            _emit_event(
                "CACHE_HIT",
                parent_job_id=parent_job_id,
                worker_id=worker_id,
                target=target,
                mode=args.mode,
                status=data.get("status"),
                summary=data.get("summary"),
                result_path=result_path,
            )
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0

    allowed, quota_before = _quota_allowed(target, ignore=args.ignore_quota)
    quota_before_summary = _quota_summary(quota_before)
    if not allowed:
        result = WorkerResult(
            status="BLOCKED",
            summary=(
                f"{target} quota is at/below delegate reserve "
                f"({quota_before.get('remaining_pct')}% <= {QUOTA_RESERVE}%)."
            ),
            blockers=["QUOTA_RESERVE"],
            target=target,
            mode=args.mode,
        )
        worker_id = "quota-" + fingerprint[:10]
        _emit_event(
            "QUOTA_BLOCK",
            parent_job_id=parent_job_id,
            worker_id=worker_id,
            target=target,
            mode=args.mode,
            status="BLOCKED",
            summary=result.summary,
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 23

    worker_id = f"w-{time.strftime('%H%M%S')}-{fingerprint[:8]}"
    run_dir = DELEGATIONS_DIR / parent_job_id / worker_id
    run_dir.mkdir(parents=True, exist_ok=True)

    envelope = TaskEnvelope(
        task_id=worker_id,
        parent_job_id=parent_job_id,
        caller=caller,
        target=target,
        depth=1,
        mode=args.mode,
        task=task,
        repo=str(repo),
        repo_head=base_snapshot["head"],
        repo_tree=base_snapshot["tree"],
        branch=base_snapshot["branch"],
        timeout_seconds=args.timeout,
        fingerprint=fingerprint,
        created_at=_now(),
        attachments=_job_multimodal(parent_job_id)[0],
        skills=_job_multimodal(parent_job_id)[1],
        constraints=[
            "MAX_DELEGATION_DEPTH=1",
            f"MAX_DELEGATES_PER_JOB={max_delegates}",
            "SELF_CALL_PROHIBITED",
            "WORKER_CANNOT_DELEGATE",
            f"REQUESTED_TARGET={requested_target}",
            (
                "DISPOSABLE_ISOLATED_WORKTREE"
                if args.mode == "read_only"
                else "DEDICATED_WRITE_WORKTREE_NO_AUTO_MERGE"
            ),
        ],
    )
    _atomic_json(run_dir / "task-envelope.json", asdict(envelope))

    con.execute(
        """
        INSERT OR REPLACE INTO delegations (
            fingerprint, worker_id, parent_job_id, caller, target, mode,
            repo_head, status, started_at, ended_at, result_path, task_preview,
            logical_fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
        """,
        (
            fingerprint,
            worker_id,
            parent_job_id,
            caller,
            target,
            args.mode,
            base_snapshot["head"],
            "RUNNING",
            _now(),
            _normalize_task(task)[:300],
            logical_fingerprint,
        ),
    )
    con.commit()

    _emit_event(
        "START",
        parent_job_id=parent_job_id,
        worker_id=worker_id,
        target=target,
        mode=args.mode,
        status="RUNNING",
        summary=_normalize_task(task)[:160],
        quota_before_summary=quota_before_summary,
    )

    chosen_note = (
        f"auto→{target}" if requested_target == "auto" else target
    )
    print(
        f"[delegate] START {worker_id} · {caller} -> {chosen_note} · {args.mode}",
        file=sys.stderr,
        flush=True,
    )

    worktree: Path | None = None
    branch: str | None = None
    ephemeral = False
    lease_stop: threading.Event | None = None
    lease_thread: threading.Thread | None = None
    start = _now()

    try:
        _write_lease(
            run_dir,
            worker_id=worker_id,
            parent_job_id=parent_job_id,
            target=target,
            state="STARTING",
        )
        lease_stop, lease_thread = _start_lease_heartbeat(
            run_dir,
            worker_id=worker_id,
            parent_job_id=parent_job_id,
            target=target,
        )
        worktree, branch, ephemeral = _prepare_worktree(
            repo,
            parent_job_id=parent_job_id,
            worker_id=worker_id,
            mode=args.mode,
            head=base_snapshot["head"],
        )

        _emit_event(
            "PROGRESS",
            parent_job_id=parent_job_id,
            worker_id=worker_id,
            target=target,
            mode=args.mode,
            status="WORKTREE_READY",
            summary=(
                "isolated read-only worktree ready"
                if args.mode == "read_only"
                else "dedicated write worktree ready"
            ),
            quota_before_summary=quota_before_summary,
        )

        prompt = _worker_prompt(
            envelope,
            isolated_worktree=worktree,
            main_repo=repo,
        )
        (run_dir / "worker-prompt.md").write_text(prompt)

        _emit_event(
            "PROGRESS",
            parent_job_id=parent_job_id,
            worker_id=worker_id,
            target=target,
            mode=args.mode,
            status="PROVIDER_RUNNING",
            summary=f"{TARGETS[target]['label']} worker running",
            quota_before_summary=quota_before_summary,
        )

        rc, final, raw_out, raw_err = _invoke_provider(envelope, worktree, prompt)
        failure_class = _classify_failure(rc=rc, stdout=raw_out, stderr=raw_err)

        _emit_event(
            "PROGRESS",
            parent_job_id=parent_job_id,
            worker_id=worker_id,
            target=target,
            mode=args.mode,
            status="VERIFYING",
            summary="provider returned; mechanically verifying result",
            quota_before_summary=quota_before_summary,
        )

        (run_dir / "provider.stdout.log").write_text(raw_out)
        (run_dir / "provider.stderr.log").write_text(raw_err)
        (run_dir / "raw-response.md").write_text(final)

        payload = _extract_worker_json(final)
        result = _worker_result_from_payload(payload, raw_text=final)

        # Provider-level failure outranks "UNSTRUCTURED". If the provider never
        # had enough quota/auth/permission to produce the contract, report the
        # actual failure class instead of blaming JSON formatting.
        if payload is None and failure_class:
            result.status = "BLOCKED" if failure_class in {
                "RATE_LIMIT", "AUTH", "PERMISSION", "NETWORK"
            } else "FAILED"
            result.summary = (
                f"{TARGETS[target]['label']} provider failed before a structured "
                f"WorkerResult could be produced ({failure_class})."
            )
            result.blockers = [f"FAILURE_CLASS_{failure_class}"]

        actual_changed = _changed_files(worktree)
        main_after = _repo_snapshot(repo)
        main_unchanged = (
            main_after["status"] == base_snapshot["status"]
            and main_after["head"] == base_snapshot["head"]
            and main_after["tree"] == base_snapshot["tree"]
        )

        test_verification = _rerun_declared_tests(worktree, result.tests)

        mechanical = {
            "provider_exit_code": rc,
            "structured_result": payload is not None,
            "main_checkout_unchanged": main_unchanged,
            "starting_head": base_snapshot["head"],
            "ending_main_head": main_after["head"],
            "actual_changed_files": actual_changed,
            "declared_files_changed": result.files_changed,
            "declared_files_match_actual": sorted(map(str, result.files_changed)) == actual_changed,
            "test_reruns": test_verification,
            "read_only_worker_writes_discarded": bool(args.mode == "read_only" and actual_changed),
            "read_only_filesystem_hardening": bool(args.mode == "read_only"),
            "delegation_budget": {
                "used_before_this_call": used_delegates,
                "max_per_job": max_delegates,
            },
        }

        result.files_changed = actual_changed
        result.mechanical_verification = mechanical
        result.worker_id = worker_id
        result.target = target
        result.requested_target = requested_target
        result.model = TARGETS[target]["model"] or TARGETS[target]["label"]
        result.mode = args.mode
        result.branch = branch
        result.worktree = str(worktree)
        result.duration_seconds = round(_now() - start, 3)
        result.raw_response_path = str(run_dir / "raw-response.md")
        result.quota_before = quota_before
        result.quota_before_summary = quota_before_summary
        result.failure_class = failure_class

        # Refresh only the provider that actually ran. CMD/Codex are actively
        # probed; AGY is reported from the statusline cache without burning a
        # second model request.
        quota_after_display = _refresh_target_quota(target)
        result.quota_after = quota_after_display
        result.quota_summary = str(quota_after_display.get("summary") or "quota unavailable")

        # Mechanical guardrail: worker self-report cannot override checkout integrity.
        if not main_unchanged:
            result.status = "FAILED"
            result.blockers.append("AUTHORITATIVE_MAIN_CHECKOUT_CHANGED_DURING_DELEGATE")

        if rc != 0 and result.status == "COMPLETE":
            result.status = "PARTIAL"
            result.blockers.append(f"PROVIDER_EXIT_{rc}")
        if failure_class and f"FAILURE_CLASS_{failure_class}" not in result.blockers:
            result.blockers.append(f"FAILURE_CLASS_{failure_class}")

        result_path = run_dir / "worker-result.json"
        _atomic_json(result_path, asdict(result))

        # Worker findings are claims, not verified truth.
        for finding in result.findings:
            if isinstance(finding, dict):
                claim = str(finding.get("claim", "")).strip()
                evidence = finding.get("evidence")
            else:
                claim = str(finding).strip()
                evidence = None

            if claim:
                _append_fact(
                    parent_job_id,
                    claim=claim,
                    status="WORKER_CLAIM_UNVERIFIED",
                    source_type="worker_result",
                    source=target,
                    worker_id=worker_id,
                    repo_head=base_snapshot["head"],
                    repo_tree=base_snapshot["tree"],
                    evidence=evidence,
                )

        # Mechanically verifiable facts are written separately.
        _append_fact(
            parent_job_id,
            claim=f"delegate main checkout unchanged = {main_unchanged}",
            status="VERIFIED",
            source_type="git_status_comparison",
            source="phase1_supervisor",
            worker_id=worker_id,
            repo_head=base_snapshot["head"],
            repo_tree=base_snapshot["tree"],
            evidence={
                "before_status_sha256": hashlib.sha256(
                    base_snapshot["status"].encode()
                ).hexdigest(),
                "after_status_sha256": hashlib.sha256(
                    main_after["status"].encode()
                ).hexdigest(),
            },
        )
        _append_fact(
            parent_job_id,
            claim=f"delegate actual changed files = {actual_changed}",
            status="VERIFIED",
            source_type="git_diff",
            source="phase1_supervisor",
            worker_id=worker_id,
            repo_head=base_snapshot["head"],
            repo_tree=base_snapshot["tree"],
            evidence=actual_changed,
        )

        for test in test_verification:
            if test.get("rerun"):
                _append_fact(
                    parent_job_id,
                    claim=(
                        f"mechanical test rerun: {test.get('command')} "
                        f"exit={test.get('exit_code')}"
                    ),
                    status="VERIFIED",
                    source_type="test_rerun",
                    source="phase1_supervisor",
                    worker_id=worker_id,
                    repo_head=base_snapshot["head"],
                    repo_tree=base_snapshot["tree"],
                    evidence=test,
                )

        quota_after = {
            "source": quota_after_display.get("source"),
            "remaining_pct": quota_after_display.get("remaining_pct"),
            "raw": quota_after_display.get("raw", {}),
            "fresh": quota_after_display.get("fresh"),
            "age_seconds": quota_after_display.get("age_seconds"),
            "summary": quota_after_display.get("summary"),
        }
        _quota_ledger(
            envelope=envelope,
            worker_id=worker_id,
            before=quota_before,
            after=quota_after,
            duration=result.duration_seconds or 0.0,
        )

        if failure_class:
            _health_mark_failure(target, failure_class)
        elif result.status == "COMPLETE" and rc == 0:
            _health_mark_success(target)

        db_status = "COMPLETE" if result.status == "COMPLETE" else result.status
        con.execute(
            """
            UPDATE delegations
            SET status = ?, ended_at = ?, result_path = ?
            WHERE fingerprint = ?
            """,
            (db_status, _now(), str(result_path), fingerprint),
        )
        con.commit()

        _emit_event(
            "DONE",
            parent_job_id=parent_job_id,
            worker_id=worker_id,
            target=target,
            mode=args.mode,
            status=result.status,
            summary=result.summary[:500],
            duration=result.duration_seconds,
            result_path=str(result_path),
            quota_summary=result.quota_summary,
            quota_before_summary=result.quota_before_summary,
            quota_after=quota_after,
        )

        print(
            f"[delegate] DONE {worker_id} · {result.status} · "
            f"{result.duration_seconds:.1f}s · quota "
            f"{result.quota_before_summary} -> {result.quota_summary}",
            file=sys.stderr,
            flush=True,
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0 if result.status in {"COMPLETE", "PARTIAL", "BLOCKED"} else 30

    except KeyboardInterrupt:
        duration = _now() - start
        result = WorkerResult(
            status="CANCELLED",
            summary="Delegated worker cancelled by user.",
            blockers=["USER_CANCELLED"],
            worker_id=worker_id,
            target=target,
            mode=args.mode,
            branch=branch,
            worktree=str(worktree) if worktree else None,
            duration_seconds=round(duration, 3),
            quota_before=quota_before,
            quota_before_summary=quota_before_summary,
        )
        result_path = run_dir / "worker-result.json"
        _atomic_json(result_path, asdict(result))
        con.execute(
            """
            UPDATE delegations
            SET status = 'CANCELLED', ended_at = ?, result_path = ?
            WHERE fingerprint = ?
            """,
            (_now(), str(result_path), fingerprint),
        )
        con.commit()
        _emit_event(
            "DONE",
            parent_job_id=parent_job_id,
            worker_id=worker_id,
            target=target,
            mode=args.mode,
            status="CANCELLED",
            summary=result.summary,
            duration=duration,
            result_path=str(result_path),
            quota_before_summary=quota_before_summary,
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 130

    except subprocess.TimeoutExpired:
        _health_mark_failure(target, "NETWORK")
        duration = _now() - start
        result = WorkerResult(
            status="FAILED",
            summary=f"Delegated worker timed out after {args.timeout}s.",
            blockers=["WORKER_TIMEOUT"],
            worker_id=worker_id,
            target=target,
            mode=args.mode,
            branch=branch,
            worktree=str(worktree) if worktree else None,
            duration_seconds=round(duration, 3),
        )
        result_path = run_dir / "worker-result.json"
        _atomic_json(result_path, asdict(result))
        con.execute(
            """
            UPDATE delegations
            SET status = 'FAILED', ended_at = ?, result_path = ?
            WHERE fingerprint = ?
            """,
            (_now(), str(result_path), fingerprint),
        )
        con.commit()
        _emit_event(
            "DONE",
            parent_job_id=parent_job_id,
            worker_id=worker_id,
            target=target,
            mode=args.mode,
            status="FAILED",
            summary=result.summary,
            duration=duration,
            result_path=str(result_path),
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 31

    except Exception as e:
        duration = _now() - start
        result = WorkerResult(
            status="FAILED",
            summary=f"Delegate supervisor error: {type(e).__name__}: {e}",
            blockers=["SUPERVISOR_ERROR"],
            worker_id=worker_id,
            target=target,
            mode=args.mode,
            branch=branch,
            worktree=str(worktree) if worktree else None,
            duration_seconds=round(duration, 3),
        )
        result_path = run_dir / "worker-result.json"
        _atomic_json(result_path, asdict(result))
        con.execute(
            """
            UPDATE delegations
            SET status = 'FAILED', ended_at = ?, result_path = ?
            WHERE fingerprint = ?
            """,
            (_now(), str(result_path), fingerprint),
        )
        con.commit()
        _emit_event(
            "DONE",
            parent_job_id=parent_job_id,
            worker_id=worker_id,
            target=target,
            mode=args.mode,
            status="FAILED",
            summary=result.summary,
            duration=duration,
            result_path=str(result_path),
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 32

    finally:
        if lease_stop is not None:
            lease_stop.set()
        if lease_thread is not None and lease_thread.is_alive():
            lease_thread.join(timeout=2)
        try:
            _write_lease(
                run_dir,
                worker_id=worker_id,
                parent_job_id=parent_job_id,
                target=target,
                state="TERMINAL",
            )
        except Exception:
            pass

        if worktree is not None and ephemeral:
            try:
                _remove_worktree(repo, worktree)
            except Exception as e:
                print(f"[delegate] warning: failed to remove temp worktree: {e}", file=sys.stderr)


def list_delegations(args: argparse.Namespace) -> int:
    con = _init_db()
    query = """
        SELECT worker_id, parent_job_id, caller, target, mode, status,
               started_at, ended_at, result_path, task_preview
        FROM delegations
    """
    params: list[Any] = []
    if args.job:
        query += " WHERE parent_job_id = ?"
        params.append(args.job)
    query += " ORDER BY started_at DESC LIMIT ?"
    params.append(args.limit)

    rows = con.execute(query, params).fetchall()
    data = []
    for row in rows:
        (
            worker_id,
            parent_job_id,
            caller,
            target,
            mode,
            status,
            started_at,
            ended_at,
            result_path,
            task_preview,
        ) = row
        data.append(
            {
                "worker_id": worker_id,
                "parent_job_id": parent_job_id,
                "caller": caller,
                "target": target,
                "mode": mode,
                "status": status,
                "started_at": _iso(started_at),
                "ended_at": _iso(ended_at) if ended_at else None,
                "result_path": result_path,
                "task_preview": task_preview,
            }
        )

    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def show_delegation(args: argparse.Namespace) -> int:
    for p in DELEGATIONS_DIR.glob(f"*/{args.worker_id}/worker-result.json"):
        print(p.read_text())
        return 0
    raise SystemExit(f"worker result not found: {args.worker_id}")


def list_facts(args: argparse.Namespace) -> int:
    parent = args.job or os.environ.get("AI_ORCH_JOB_ID")
    paths: list[Path] = []
    if parent:
        job_path = JOBS_DIR / parent / "fact-ledger.jsonl"
        if job_path.exists():
            paths.append(job_path)
    if not paths and GLOBAL_FACT_LEDGER.exists():
        paths.append(GLOBAL_FACT_LEDGER)

    current = None
    try:
        repo = _repo_root(Path(args.repo or os.getcwd()))
        current = _repo_snapshot(repo)
    except Exception:
        current = None

    rows: list[dict[str, Any]] = []
    for path in paths:
        for line in path.read_text().splitlines():
            try:
                item = json.loads(line)
            except Exception:
                continue

            if args.status and str(item.get("status")) != args.status:
                continue

            fresh: bool | None = None
            stale_reason = None
            if current is not None:
                fact_tree = item.get("repo_tree")
                fact_head = item.get("repo_head")
                if fact_tree:
                    fresh = str(fact_tree) == str(current["tree"])
                    if not fresh:
                        stale_reason = "REPO_TREE_CHANGED"
                elif fact_head:
                    fresh = str(fact_head) == str(current["head"])
                    if not fresh:
                        stale_reason = "REPO_HEAD_CHANGED"
                else:
                    fresh = None
                    stale_reason = "NO_REPO_IDENTITY"

            item["_fresh"] = fresh
            item["_stale_reason"] = stale_reason
            if current is not None:
                item["_current_head"] = current["head"]
                item["_current_tree"] = current["tree"]
            item["_ledger"] = str(path)

            if args.fresh_only and fresh is not True:
                continue
            if args.stale_only and fresh is not False:
                continue
            rows.append(item)

    print(json.dumps(rows[-args.limit:], ensure_ascii=False, indent=2))
    return 0


def cleanup_worktrees(args: argparse.Namespace) -> int:
    repo = _repo_root(Path(args.repo or os.getcwd()))
    data = _cleanup_stale_worktrees(
        repo,
        max_age_seconds=args.older_than,
        dry_run=args.dry_run,
    )
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def show_health(args: argparse.Namespace) -> int:
    data = _load_provider_health()
    now = _now()
    out = {}
    for target in sorted(TARGETS):
        item = data.get(target, {}) if isinstance(data.get(target), dict) else {}
        until = float(item.get("blocked_until") or 0)
        out[target] = {
            "label": TARGETS[target]["label"],
            "state": "COOLDOWN" if until > now else "HEALTHY",
            "failure_class": item.get("failure_class"),
            "failure_count": int(item.get("failure_count") or 0),
            "blocked_until": _iso(until) if until > now else None,
            "remaining_cooldown_seconds": max(0, int(until - now)),
        }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def recover_stale(args: argparse.Namespace) -> int:
    repo = _repo_root(Path(args.repo or os.getcwd()))
    con = _init_db()
    rows = _recover_stale_running(con, repo)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="OrchBridge Phase 3-safe delegation supervisor")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("delegate", help="delegate one synchronous worker")
    d.add_argument("--model", required=True, choices=["auto", *sorted(TARGETS)])
    d.add_argument("--caller")
    d.add_argument("--mode", choices=["read_only", "write"], default="read_only")
    d.add_argument("--task")
    d.add_argument("--task-file")
    d.add_argument("--repo")
    d.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    d.add_argument("--fresh", action="store_true", help="bypass exact-task dedup cache")
    d.add_argument(
        "--ignore-quota",
        action="store_true",
        help="manual override only; bypass delegate quota reserve",
    )
    d.add_argument(
        "--ignore-health",
        action="store_true",
        help="manual override only; bypass provider circuit-breaker cooldown",
    )
    d.set_defaults(func=delegate)

    ls = sub.add_parser("list", help="list recent delegations")
    ls.add_argument("--job")
    ls.add_argument("--limit", type=int, default=20)
    ls.set_defaults(func=list_delegations)

    sh = sub.add_parser("show", help="show one worker result")
    sh.add_argument("worker_id")
    sh.set_defaults(func=show_delegation)

    facts = sub.add_parser("facts", help="show fact-ledger entries")
    facts.add_argument("--job")
    facts.add_argument("--status")
    facts.add_argument("--repo")
    facts.add_argument("--fresh-only", action="store_true")
    facts.add_argument("--stale-only", action="store_true")
    facts.add_argument("--limit", type=int, default=30)
    facts.set_defaults(func=list_facts)

    cleanup = sub.add_parser("cleanup", help="clean stale terminal read-only worktrees")
    cleanup.add_argument("--repo")
    cleanup.add_argument("--older-than", type=int, default=7200)
    cleanup.add_argument("--dry-run", action="store_true")
    cleanup.set_defaults(func=cleanup_worktrees)

    health = sub.add_parser("health", help="show delegate provider circuit-breaker state")
    health.set_defaults(func=show_health)

    recover = sub.add_parser("recover", help="recover stale RUNNING delegate leases")
    recover.add_argument("--repo")
    recover.set_defaults(func=recover_stale)

    return p


def main() -> int:
    PHASE1_DIR.mkdir(parents=True, exist_ok=True)
    DELEGATIONS_DIR.mkdir(parents=True, exist_ok=True)
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)

    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from provider_health_state import (
    clear_provider as clear_global_provider,
    mark_quota_exhausted as mark_global_quota_exhausted,
    provider_status as global_provider_status,
    provider_unavailable as global_provider_unavailable,
)

HOME = Path.home()
BASE = HOME / ".local/share/orchbridge"
APP = BASE / "app"
BIN = HOME / ".local/bin"
CACHE = HOME / ".cache/orchbridge"
PROJECT_BASE = Path(os.environ.get("AI_ORCH_PROJECT_BASE", str(BASE))).expanduser().resolve()
JOBS_DIR = PROJECT_BASE / "jobs"
DELEGATIONS_DIR = PROJECT_BASE / "delegations"
DB_PATH = DELEGATIONS_DIR / "registry.sqlite3"
PHASE3_DIR = PROJECT_BASE / "phase3"
BATCHES_DIR = PHASE3_DIR / "batches"
HANDOFFS_DIR = PHASE3_DIR / "handoffs"
EVENTS_PATH = PHASE3_DIR / "events.jsonl"
HANDOFF_LOCK = PHASE3_DIR / "handoff.lock"
PROVIDER_HEALTH_PATH = BASE / "delegations/provider-health.json"
DELEGATE_BIN = Path(os.environ.get("AI_ORCH_PHASE3_DELEGATE_BIN", str(BIN / "orch-delegate")))

DEFAULT_TIMEOUT = int(os.environ.get("AI_ORCH_PHASE3_TIMEOUT", "1200"))
DEFAULT_PARALLEL = max(1, min(3, int(os.environ.get("AI_ORCH_PHASE3_PARALLEL", "2"))))
MAX_PARALLEL = 3
QUOTA_RESERVE = float(os.environ.get("AI_ORCH_DELEGATE_QUOTA_RESERVE", "3"))

TARGETS: dict[str, dict[str, Any]] = {
    "codex": {"backend": "codex", "model": None, "label": "GPT-6 Luna", "pool": "codex", "cost": 4},
    "cmd": {"backend": "cmd", "model": "xiaomi/mimo-v2.5-pro", "label": "MiMo V2.5 Pro", "pool": "cmd", "cost": 1},
    "sonnet": {"backend": "agy", "model": "claude-sonnet-4-6-thinking", "label": "Claude Sonnet 4.6 Thinking", "pool": "agy-third", "cost": 3},
    "opus": {"backend": "agy", "model": "claude-opus-4-6-thinking", "label": "Claude Opus 4.6 Thinking", "pool": "agy-third", "cost": 9},
    "gemini-low": {"backend": "agy", "model": "gemini-3.8-flash-low", "label": "Gemini 3.8 Flash low", "pool": "agy-gemini", "cost": 1},
    "gemini-medium": {"backend": "agy", "model": "gemini-3.8-flash-medium", "label": "Gemini 3.8 Flash medium", "pool": "agy-gemini", "cost": 2},
    "gemini-high": {"backend": "agy", "model": "gemini-3.8-flash-high", "label": "Gemini 3.8 Flash high", "pool": "agy-gemini", "cost": 3},
}

_EVENT_LOCK = threading.Lock()


@dataclass
class WorkerCall:
    target: str
    returncode: int
    status: str
    summary: str
    result: dict[str, Any] = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0


@dataclass
class BatchResult:
    batch_id: str
    kind: str
    parent_job_id: str
    caller: str
    task: str
    requested_count: int
    targets: list[str]
    started_at: float
    ended_at: float | None = None
    status: str = "RUNNING"
    workers: list[dict[str, Any]] = field(default_factory=list)
    collection_path: str | None = None


def now() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ts or now()).astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)


def append_jsonl(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(data, ensure_ascii=False) + "\n"
    with _EVENT_LOCK:
        with path.open("a") as f:
            f.write(line)


def emit(event: str, **payload: Any) -> None:
    append_jsonl(EVENTS_PATH, {"ts": iso(), "epoch": now(), "event": event, **payload})


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def job_id() -> str:
    value = os.environ.get("AI_ORCH_JOB_ID", "").strip()
    return value or "adhoc-" + time.strftime("%Y%m%d-%H%M%S")


def current_job_dir(parent_job_id: str | None = None) -> Path | None:
    jid = parent_job_id or job_id()
    p = JOBS_DIR / jid
    return p if p.is_dir() else None


def caller_target(caller: str) -> str | None:
    c = (caller or "").strip().lower()
    if c in TARGETS:
        return c
    if "gpt-6" in c or "codex" in c:
        return "codex"
    if "mimo" in c or "command code" in c or c == "cmd":
        return "cmd"
    if "opus" in c:
        return "opus"
    if "sonnet" in c or "claude" in c:
        return "sonnet"
    if "gemini-3.8-flash-high" in c or "gemini high" in c:
        return "gemini-high"
    if "gemini-3.8-flash-medium" in c or "gemini medium" in c:
        return "gemini-medium"
    if "gemini" in c:
        return "gemini-low"
    return None


def caller_pool(caller: str) -> str | None:
    target = caller_target(caller)
    return TARGETS[target]["pool"] if target else None


def health_state(target: str) -> dict[str, Any]:
    data = load_json(PROVIDER_HEALTH_PATH)
    item = data.get(target)
    return item if isinstance(item, dict) else {}


def health_blocked(target: str) -> tuple[bool, dict[str, Any]]:
    if target == "cmd" and global_provider_unavailable("commandcode"):
        g = global_provider_status("commandcode")
        return True, {**g, "global": True, "status": "QUOTA_EXHAUSTED"}
    item = health_state(target)
    until = float(item.get("blocked_until") or 0)
    return until > now(), item


def save_provider_health(data: dict[str, Any]) -> None:
    atomic_json(PROVIDER_HEALTH_PATH, data)


def health_mark_success(target: str) -> None:
    if target == "cmd":
        clear_global_provider("commandcode", source="phase3_success")
    data = load_json(PROVIDER_HEALTH_PATH)
    data[target] = {
        "status": "HEALTHY",
        "blocked_until": 0,
        "failure_class": None,
        "failure_count": 0,
        "updated_at": now(),
        "updated_at_iso": iso(),
    }
    save_provider_health(data)


def health_mark_failure(target: str, failure_class: str | None) -> None:
    if not failure_class:
        return
    if target == "cmd" and failure_class == "CREDIT_EXHAUSTED":
        mark_global_quota_exhausted(
            "commandcode", reason="credit/quota exhausted", source="phase3",
            retry_after_seconds=3600,
        )
        data = load_json(PROVIDER_HEALTH_PATH)
        old = data.get(target, {}) if isinstance(data.get(target), dict) else {}
        data[target] = {
            "status": "QUOTA_EXHAUSTED", "blocked_until": 0,
            "failure_class": failure_class,
            "failure_count": int(old.get("failure_count") or 0) + 1,
            "updated_at": now(), "updated_at_iso": iso(),
        }
        save_provider_health(data)
        return
    cooldowns = {
        "RATE_LIMIT": 15 * 60,
        "AUTH": 10 * 60,
        "PERMISSION": 10 * 60,
        "NETWORK": 2 * 60,
        "PROVIDER_ERROR": 5 * 60,
    }
    seconds = cooldowns.get(failure_class, 5 * 60)
    data = load_json(PROVIDER_HEALTH_PATH)
    old = data.get(target, {}) if isinstance(data.get(target), dict) else {}
    count = int(old.get("failure_count") or 0) + 1
    if failure_class not in {"RATE_LIMIT", "AUTH", "PERMISSION"}:
        seconds = min(15 * 60, seconds * min(count, 3))
    data[target] = {
        "status": "COOLDOWN",
        "blocked_until": now() + seconds,
        "failure_class": failure_class,
        "failure_count": count,
        "updated_at": now(),
        "updated_at_iso": iso(),
    }
    save_provider_health(data)


def _remaining_from_fields(data: dict[str, Any], fields: list[str]) -> float | None:
    values: list[float] = []
    for field in fields:
        value = data.get(field)
        if value is None:
            continue
        try:
            values.append(float(value))
        except Exception:
            pass
    return min(values) if values else None


def quota_remaining(target: str) -> tuple[float | None, str]:
    pool = TARGETS[target]["pool"]
    if pool == "cmd":
        d = load_json(CACHE / "cmd-usage.json")
        return _remaining_from_fields(d, ["monthly_remaining_pct", "five_hour_remaining_pct", "weekly_remaining_pct"]), "cmd-cache"
    if pool == "codex":
        d = load_json(CACHE / "codex-usage.json")
        return _remaining_from_fields(d, ["five_hour_remaining_pct", "weekly_remaining_pct"]), "codex-cache"

    d = load_json(CACHE / "agy-status.json")
    quota = d.get("quota", {}) if isinstance(d.get("quota"), dict) else {}
    keys = ["3p-5h", "3p-weekly"] if pool == "agy-third" else ["gemini-5h", "gemini-weekly"]
    values: list[float] = []
    for key in keys:
        item = quota.get(key, {}) if isinstance(quota.get(key), dict) else {}
        rem = item.get("remaining_fraction")
        if rem is not None:
            try:
                values.append(float(rem) * 100.0)
            except Exception:
                pass
    return (min(values) if values else None), "agy-cache"


def target_viable(target: str) -> tuple[bool, dict[str, Any]]:
    blocked, hs = health_blocked(target)
    remaining, source = quota_remaining(target)
    viable = not blocked and (remaining is None or remaining > QUOTA_RESERVE)
    return viable, {
        "target": target,
        "pool": TARGETS[target]["pool"],
        "health_blocked": blocked,
        "health": hs,
        "remaining_pct": remaining,
        "quota_source": source,
    }


def _recent_target_counts(limit: int = 24) -> dict[str, int]:
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


def _collab_task_role(task: str, kind: str = "parallel") -> str:
    text = str(task or "").casefold()
    if kind == "consult":
        return "review"
    if any(x in text for x in (
        "review", "audit", "verify", "second opinion", "independent", "evidence",
        "security", "risk", "regression", "final", "검토", "감사", "검증", "독립", "근거",
    )):
        return "review"
    if any(x in text for x in (
        "research", "compare", "survey", "sources", "benchmark", "조사", "비교", "리서치",
    )):
        return "research"
    if any(x in text for x in (
        "implement", "fix", "debug", "patch", "refactor", "code", "test",
        "구현", "수정", "디버그", "리팩터", "코드", "테스트",
    )):
        return "implementation"
    return "general"


def _task_affinity(task: str, target: str) -> float:
    raw = hashlib.sha256((str(task or "") + "\0" + target).encode("utf-8")).digest()
    return int.from_bytes(raw[:2], "big") / 65535.0


def auto_targets(
    caller: str,
    count: int,
    *,
    include_opus: bool = False,
    task: str = "",
    kind: str = "parallel",
) -> list[str]:
    count = max(1, min(MAX_PARALLEL, count))
    current_target = caller_target(caller)
    current_pool = caller_pool(caller)
    role = _collab_task_role(task, kind)
    role_capability = {
        "review": {"sonnet": .98, "opus": 1.00, "codex": .94, "gemini-high": .89,
                   "gemini-medium": .80, "cmd": .72, "gemini-low": .66},
        "research": {"gemini-high": .96, "sonnet": .93, "codex": .90, "gemini-medium": .87,
                     "cmd": .76, "gemini-low": .75, "opus": .98},
        "implementation": {"codex": .97, "sonnet": .92, "cmd": .89, "gemini-high": .85,
                           "gemini-medium": .80, "gemini-low": .72, "opus": .98},
        "general": {"codex": .95, "sonnet": .92, "cmd": .86, "gemini-high": .86,
                    "gemini-medium": .80, "gemini-low": .73, "opus": .98},
    }[role]
    priority = ["sonnet", "gemini-high", "cmd", "gemini-medium", "gemini-low", "codex"]
    if include_opus:
        priority.append("opus")
    recent = _recent_target_counts()
    ranked: list[tuple[float, str, str]] = []
    for base_rank, target in enumerate(priority):
        spec = TARGETS[target]
        pool = str(spec["pool"])
        if target == current_target or pool == current_pool:
            continue
        viable, detail = target_viable(target)
        if not viable:
            continue
        rem = detail["remaining_pct"]
        cap = float(role_capability.get(target, .70))
        score = (
            2.8 * float(recent.get(target, 0))
            + 3.0 * (1.0 - cap)
            + 0.045 * float(spec["cost"])
            + 0.025 * float(base_rank)
            - (0.0 if rem is None else min(.85, max(0.0, float(rem)) / 100.0))
            - .38 * _task_affinity(task, target)
            - (.40 if role == "review" and target == "sonnet" else 0.0)
        )
        ranked.append((score, target, pool))
    ranked.sort(key=lambda row: (row[0], row[1]))
    ordered: list[str] = []
    seen_pools: set[str] = set()
    for _score, target, pool in ranked:
        if pool in seen_pools:
            continue
        ordered.append(target)
        seen_pools.add(pool)
        if len(ordered) >= count:
            break
    return ordered


def parse_models(
    text: str | None,
    caller: str,
    count: int,
    include_opus: bool,
    *,
    task: str = "",
    kind: str = "parallel",
) -> list[str]:
    if not text or text.strip().lower() == "auto":
        return auto_targets(caller, count, include_opus=include_opus, task=task, kind=kind)
    raw = [x.strip() for x in text.split(",") if x.strip()]
    out: list[str] = []
    current = caller_target(caller)
    for target in raw:
        if target not in TARGETS:
            raise SystemExit(f"unsupported Phase 3 target: {target}")
        if target == "opus" and not include_opus:
            raise SystemExit("Opus is reserve-only; pass --include-opus for an explicit Opus request")
        if target == current:
            raise SystemExit(f"self-call prohibited: {caller} -> {target}")
        if target not in out:
            out.append(target)
    if len(out) > MAX_PARALLEL:
        raise SystemExit(f"max parallel workers is {MAX_PARALLEL}")
    return out[:count]


def delegate_budget_remaining(parent_job_id: str) -> tuple[int, int, int]:
    try:
        maximum = max(1, min(8, int(os.environ.get("AI_ORCH_MAX_DELEGATES_PER_JOB", "3"))))
    except Exception:
        maximum = 3
    used = 0
    if DB_PATH.exists():
        try:
            con = sqlite3.connect(DB_PATH)
            row = con.execute(
                """
                SELECT COUNT(*) FROM delegations
                WHERE parent_job_id=? AND worker_id NOT LIKE 'quota-%'
                  AND status NOT IN ('BLOCKED', 'CANCELLED')
                """,
                (parent_job_id,),
            ).fetchone()
            con.close()
            used = int(row[0] if row else 0)
        except Exception:
            used = 0
    return maximum, used, max(0, maximum - used)


def read_task(args: argparse.Namespace, *, allow_job_default: bool = True) -> str:
    if getattr(args, "task_file", None):
        text = Path(args.task_file).read_text()
    elif getattr(args, "task", None):
        text = str(args.task)
    elif allow_job_default:
        jd = current_job_dir()
        p = jd / "original-prompt.md" if jd else None
        text = p.read_text() if p and p.exists() else ""
    else:
        text = ""
    if not text.strip():
        raise SystemExit("task is empty; use --task/--task-file or run inside a persistent job")
    return text.strip()


def new_id(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    decoder = json.JSONDecoder()
    for m in reversed(list(re.finditer(r"\{", text))):
        try:
            obj, _ = decoder.raw_decode(text[m.start():])
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return {}


def delegate_task_prompt(task: str, kind: str, target: str, ordinal: int) -> str:
    if kind == "consult":
        prefix = (
            "PHASE3 CONSULTATION. Give an independent second opinion on the bounded task below. "
            "Focus on concrete evidence, risks, contradictions, and missing verification. "
            "Do not broaden scope merely to be comprehensive."
        )
    else:
        prefix = (
            f"PHASE3 PARALLEL REVIEW #{ordinal}. Independently investigate the bounded task below. "
            "Do not assume other reviewers agree with you. Prefer repo/tool evidence and explicitly "
            "flag uncertainty or contradictory evidence."
        )
    return f"{prefix}\n\nTARGET REVIEWER: {target}\n\nTASK:\n{task}\n"


def run_delegate_call(*, target: str, caller: str, task: str, kind: str, mode: str, timeout: int, ordinal: int, fresh: bool) -> WorkerCall:
    argv = [
        str(DELEGATE_BIN),
        "--caller", caller,
        "--model", target,
        "--mode", mode,
        "--timeout", str(timeout),
        "--task", delegate_task_prompt(task, kind, target, ordinal),
    ]
    if fresh:
        argv.append("--fresh")

    started = now()
    emit("COLLAB_WORKER_START", parent_job_id=job_id(), kind=kind, target=target, caller=caller)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 30)
        payload = parse_json_object(p.stdout)
        status = str(payload.get("status") or ("FAILED" if p.returncode else "UNKNOWN"))
        summary = str(payload.get("summary") or p.stderr.strip()[-500:] or "no summary")
        call = WorkerCall(
            target=target,
            returncode=p.returncode,
            status=status,
            summary=summary,
            result=payload,
            stdout=p.stdout,
            stderr=p.stderr,
            duration_seconds=round(now() - started, 3),
        )
    except subprocess.TimeoutExpired as e:
        call = WorkerCall(
            target=target,
            returncode=124,
            status="FAILED",
            summary=f"Phase 3 wrapper timed out after {timeout + 30}s",
            stdout=(e.stdout or "") if isinstance(e.stdout, str) else "",
            stderr=(e.stderr or "") if isinstance(e.stderr, str) else "",
            duration_seconds=round(now() - started, 3),
        )
    emit(
        "COLLAB_WORKER_DONE",
        parent_job_id=job_id(),
        kind=kind,
        target=target,
        caller=caller,
        status=call.status,
        duration_seconds=call.duration_seconds,
        summary=call.summary[:500],
    )
    return call


def normalized_claim(claim: str) -> str:
    text = claim.casefold().strip()
    text = re.sub(r"[`*_#]+", "", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\w\s./:-]", "", text)
    return text[:1000]


def build_collection(batch: dict[str, Any]) -> dict[str, Any]:
    workers = batch.get("workers", []) if isinstance(batch.get("workers"), list) else []
    claims: list[dict[str, Any]] = []
    exact_groups: dict[str, list[dict[str, Any]]] = {}
    verified: list[dict[str, Any]] = []

    for worker in workers:
        result = worker.get("result", {}) if isinstance(worker.get("result"), dict) else {}
        target = str(worker.get("target") or result.get("target") or "unknown")
        for finding in result.get("findings", []) if isinstance(result.get("findings"), list) else []:
            if isinstance(finding, dict):
                claim = str(finding.get("claim") or "").strip()
                item = {
                    "target": target,
                    "claim": claim,
                    "confidence": finding.get("confidence"),
                    "evidence": finding.get("evidence"),
                }
            else:
                claim = str(finding).strip()
                item = {"target": target, "claim": claim, "confidence": None, "evidence": None}
            if not claim:
                continue
            claims.append(item)
            key = normalized_claim(claim)
            exact_groups.setdefault(key, []).append(item)

        mech = result.get("mechanical_verification")
        if isinstance(mech, dict) and mech:
            verified.append({"target": target, "mechanical_verification": mech})

    exact_agreement = [
        {"claim": items[0]["claim"], "targets": sorted({str(x["target"]) for x in items}), "count": len({str(x["target"]) for x in items})}
        for key, items in exact_groups.items()
        if key and len({str(x["target"]) for x in items}) >= 2
    ]

    statuses = [str(w.get("status") or "UNKNOWN") for w in workers]
    usable = sum(1 for s in statuses if s in {"COMPLETE", "PARTIAL"})
    if workers and usable == len(workers) and all(s == "COMPLETE" for s in statuses):
        coll_status = "COMPLETE"
    elif usable:
        coll_status = "PARTIAL"
    elif workers:
        coll_status = "BLOCKED"
    else:
        coll_status = "EMPTY"

    return {
        "schema": "phase3-collection-v1",
        "batch_id": batch.get("batch_id"),
        "kind": batch.get("kind"),
        "parent_job_id": batch.get("parent_job_id"),
        "status": coll_status,
        "worker_count": len(workers),
        "usable_worker_count": usable,
        "workers": [
            {
                "target": w.get("target"),
                "status": w.get("status"),
                "summary": w.get("summary"),
                "duration_seconds": w.get("duration_seconds"),
                "worker_id": (w.get("result") or {}).get("worker_id") if isinstance(w.get("result"), dict) else None,
                "blockers": (w.get("result") or {}).get("blockers") if isinstance(w.get("result"), dict) else None,
                "recommended_next": (w.get("result") or {}).get("recommended_next") if isinstance(w.get("result"), dict) else None,
            }
            for w in workers
        ],
        "claims": claims,
        "mechanical_exact_agreement": exact_agreement,
        "mechanical_verification": verified,
        "semantic_consensus": "NOT_COMPUTED",
        "semantic_conflicts": "NOT_COMPUTED",
        "trust_note": (
            "Worker claims remain unverified unless backed by mechanical_verification/fact-ledger evidence. "
            "Exact text agreement is not semantic consensus. MAIN must synthesize and re-check important claims."
        ),
    }


def run_batch(args: argparse.Namespace, kind: str) -> int:
    if int(os.environ.get("AI_ORCH_DELEGATION_DEPTH", "0") or "0") >= 1:
        print(json.dumps({"status": "BLOCKED", "blocker": "WORKER_CANNOT_ORCHESTRATE"}, indent=2))
        return 20

    caller = (args.caller or "").strip()
    if not caller:
        raise SystemExit("--caller is required")

    task = read_task(args)
    if kind == "consult":
        requested_count = 1
    else:
        if args.count is None:
            explicit = [x.strip() for x in str(args.models or "").split(",") if x.strip()]
            requested_count = DEFAULT_PARALLEL if not explicit or explicit == ["auto"] else len(explicit)
        else:
            requested_count = int(args.count)
        requested_count = max(1, min(MAX_PARALLEL, requested_count))
    mode = "read_only" if kind == "consult" else args.mode
    if mode == "write" and requested_count > 1:
        raise SystemExit("parallel write workers are prohibited; use one write delegate or a synchronous write handoff")

    maximum, used, remaining = delegate_budget_remaining(job_id())
    if remaining <= 0:
        print(json.dumps({
            "status": "BLOCKED",
            "summary": f"Delegate budget exhausted: {used}/{maximum}",
            "blockers": ["DELEGATION_BUDGET_EXHAUSTED"],
        }, ensure_ascii=False, indent=2))
        return 25
    launch_count = min(requested_count, remaining)
    targets = parse_models(args.models, caller, launch_count, args.include_opus, task=task, kind=kind)
    if not targets:
        payload = {
            "status": "BLOCKED",
            "summary": "No distinct healthy/quota-viable Phase 3 target available.",
            "blockers": ["NO_PHASE3_TARGET"],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 24

    bid = new_id("consult" if kind == "consult" else "parallel")
    bdir = BATCHES_DIR / bid
    bdir.mkdir(parents=True, exist_ok=False)
    batch = BatchResult(
        batch_id=bid,
        kind=kind,
        parent_job_id=job_id(),
        caller=caller,
        task=task,
        requested_count=requested_count,
        targets=targets,
        started_at=now(),
    )
    atomic_json(bdir / "batch.json", asdict(batch))
    emit("COLLAB_BATCH_START", batch_id=bid, parent_job_id=batch.parent_job_id, kind=kind, caller=caller, targets=targets)

    calls: list[WorkerCall] = []
    if len(targets) == 1:
        calls.append(run_delegate_call(
            target=targets[0], caller=caller, task=task, kind=kind, mode=mode,
            timeout=args.timeout, ordinal=1, fresh=bool(args.fresh or kind == "parallel"),
        ))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(targets))) as ex:
            futures = {
                ex.submit(
                    run_delegate_call,
                    target=t,
                    caller=caller,
                    task=task,
                    kind=kind,
                    mode=mode,
                    timeout=args.timeout,
                    ordinal=i + 1,
                    fresh=True,
                ): t
                for i, t in enumerate(targets)
            }
            for fut in concurrent.futures.as_completed(futures):
                calls.append(fut.result())

    # Preserve requested target order for deterministic collection/output.
    by_target = {c.target: c for c in calls}
    ordered = [by_target[t] for t in targets if t in by_target]
    workers_payload: list[dict[str, Any]] = []
    for call in ordered:
        item = asdict(call)
        workers_payload.append(item)
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", call.target)
        atomic_json(bdir / f"worker-{safe}.json", item)
        if call.stderr:
            (bdir / f"worker-{safe}.stderr.log").write_text(call.stderr)
        if call.stdout:
            (bdir / f"worker-{safe}.stdout.log").write_text(call.stdout)

    usable = sum(1 for c in ordered if c.status in {"COMPLETE", "PARTIAL"})
    if usable == len(ordered) and ordered and all(c.status == "COMPLETE" for c in ordered):
        final_status = "COMPLETE"
    elif usable:
        final_status = "PARTIAL"
    else:
        final_status = "BLOCKED"

    batch.workers = workers_payload
    batch.ended_at = now()
    batch.status = final_status
    collection = build_collection(asdict(batch))
    collection_path = bdir / "collection.json"
    atomic_json(collection_path, collection)
    batch.collection_path = str(collection_path)
    atomic_json(bdir / "batch.json", asdict(batch))
    emit(
        "COLLAB_BATCH_DONE",
        batch_id=bid,
        parent_job_id=batch.parent_job_id,
        kind=kind,
        status=final_status,
        targets=targets,
        usable_workers=usable,
        collection_path=str(collection_path),
    )

    output = {
        "status": final_status,
        "batch_id": bid,
        "kind": kind,
        "requested_count": requested_count,
        "launched_count": len(targets),
        "delegate_budget": {"max": maximum, "used_before": used, "remaining_before": remaining},
        "targets": targets,
        "collection_path": str(collection_path),
        "collection": collection,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if usable else 30


def find_batch(batch_id: str, parent_job: str | None = None) -> Path:
    if batch_id != "latest":
        p = BATCHES_DIR / batch_id
        if not p.is_dir():
            raise SystemExit(f"unknown batch: {batch_id}")
        return p

    candidates = []
    for p in BATCHES_DIR.glob("*") if BATCHES_DIR.exists() else []:
        meta = load_json(p / "batch.json")
        if parent_job and str(meta.get("parent_job_id")) != parent_job:
            continue
        candidates.append((float(meta.get("started_at") or p.stat().st_mtime), p))
    if not candidates:
        raise SystemExit("no Phase 3 batches found")
    candidates.sort(reverse=True, key=lambda x: x[0])
    return candidates[0][1]


def collect_cmd(args: argparse.Namespace) -> int:
    parent = args.job or (job_id() if os.environ.get("AI_ORCH_JOB_ID") else None)
    bdir = find_batch(args.batch, parent_job=parent if args.batch == "latest" else None)
    batch = load_json(bdir / "batch.json")
    collection = build_collection(batch)
    atomic_json(bdir / "collection.json", collection)
    print(json.dumps(collection, ensure_ascii=False, indent=2))
    return 0


def list_cmd(args: argparse.Namespace) -> int:
    rows: list[dict[str, Any]] = []
    if BATCHES_DIR.exists():
        for p in BATCHES_DIR.glob("*"):
            d = load_json(p / "batch.json")
            if not d:
                continue
            if args.job and str(d.get("parent_job_id")) != args.job:
                continue
            rows.append({
                "type": "batch",
                "id": d.get("batch_id"),
                "kind": d.get("kind"),
                "job": d.get("parent_job_id"),
                "status": d.get("status"),
                "targets": d.get("targets"),
                "started_at": d.get("started_at"),
            })
    if HANDOFFS_DIR.exists():
        for p in HANDOFFS_DIR.glob("*"):
            d = load_json(p / "handoff.json")
            if not d:
                continue
            if args.job and str(d.get("parent_job_id")) != args.job:
                continue
            rows.append({
                "type": "handoff",
                "id": d.get("handoff_id"),
                "job": d.get("parent_job_id"),
                "status": d.get("status"),
                "caller": d.get("caller"),
                "target": d.get("target"),
                "mode": d.get("mode"),
                "started_at": d.get("started_at"),
            })
    rows.sort(key=lambda x: float(x.get("started_at") or 0), reverse=True)
    rows = rows[: args.limit]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif not rows:
        print("No Phase 3 collaboration activity.")
    else:
        print("PHASE 3 COLLABORATION")
        for r in rows:
            if r["type"] == "batch":
                print(f"{r['id']}  {str(r.get('kind')):<8} {str(r.get('status')):<9} targets={','.join(r.get('targets') or [])}")
            else:
                print(f"{r['id']}  handoff  {str(r.get('status')):<9} {r.get('caller')} -> {r.get('target')} ({r.get('mode')})")
    return 0


def show_cmd(args: argparse.Namespace) -> int:
    p = BATCHES_DIR / args.id / "batch.json"
    if p.exists():
        print(p.read_text(), end="")
        return 0
    p = HANDOFFS_DIR / args.id / "handoff.json"
    if p.exists():
        print(p.read_text(), end="")
        return 0
    raise SystemExit(f"unknown Phase 3 id: {args.id}")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def acquire_handoff_lock(payload: dict[str, Any]) -> None:
    PHASE3_DIR.mkdir(parents=True, exist_ok=True)
    if HANDOFF_LOCK.exists():
        old = load_json(HANDOFF_LOCK)
        old_pid = int(old.get("pid") or 0)
        old_at = float(old.get("created_at") or 0)
        if not pid_alive(old_pid) and now() - old_at > 30:
            try:
                HANDOFF_LOCK.unlink()
            except FileNotFoundError:
                pass
        else:
            raise RuntimeError(f"HANDOFF_ALREADY_ACTIVE pid={old_pid} id={old.get('handoff_id')}")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(HANDOFF_LOCK, flags, 0o600)
    try:
        os.write(fd, (json.dumps({"pid": os.getpid(), "created_at": now(), **payload}) + "\n").encode())
    finally:
        os.close(fd)


def release_handoff_lock() -> None:
    try:
        data = load_json(HANDOFF_LOCK)
        if int(data.get("pid") or 0) == os.getpid():
            HANDOFF_LOCK.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def running_write_delegate() -> dict[str, Any] | None:
    db = DELEGATIONS_DIR / "registry.sqlite3"
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(db)
        row = con.execute(
            "SELECT worker_id,target,started_at FROM delegations WHERE status='RUNNING' AND mode='write' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        con.close()
        if row:
            return {"worker_id": row[0], "target": row[1], "started_at": row[2]}
    except Exception:
        return None
    return None


def git(repo: Path, *args: str, timeout: int = 20) -> tuple[int, str, str]:
    try:
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return 1, "", repr(e)


def repo_snapshot(repo: Path) -> dict[str, Any]:
    rc, head, _ = git(repo, "rev-parse", "HEAD")
    rc2, tree, _ = git(repo, "rev-parse", "HEAD^{tree}")
    rc3, branch, _ = git(repo, "branch", "--show-current")
    rc4, status, _ = git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    return {
        "git": rc == 0,
        "head": head if rc == 0 else None,
        "tree": tree if rc2 == 0 else None,
        "branch": branch if rc3 == 0 else None,
        "status": status if rc4 == 0 else None,
    }


def changed_files_from_status(before: str | None, after: str | None) -> list[str]:
    def files(text: str | None) -> set[str]:
        out: set[str] = set()
        for line in (text or "").splitlines():
            body = line[3:] if len(line) >= 4 else line
            if " -> " in body:
                body = body.split(" -> ", 1)[1]
            body = body.strip()
            if body:
                out.add(body)
        return out
    return sorted(files(before) | files(after))


def prepare_readonly_worktree(repo: Path, hid: str) -> tuple[Path, bool]:
    snap = repo_snapshot(repo)
    if not snap.get("git"):
        return repo, False
    worktree = HANDOFFS_DIR / hid / "worktree"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), str(snap["head"])], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {p.stderr.strip()}")
    return worktree, True


def remove_worktree(repo: Path, worktree: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)], capture_output=True, text=True)
    shutil.rmtree(worktree, ignore_errors=True)


def terminate_group(proc: subprocess.Popen[str]) -> None:
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
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass


def run_process(argv: list[str], *, cwd: Path, input_text: str | None = None, timeout: int, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(input_text, timeout=timeout)
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        terminate_group(proc)
        stdout, stderr = proc.communicate()
        raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr)
    except KeyboardInterrupt:
        terminate_group(proc)
        raise


def invoke_target(target: str, cwd: Path, prompt: str, timeout: int, env: dict[str, str]) -> tuple[int, str, str, str]:
    spec = TARGETS[target]
    if spec["backend"] == "codex":
        argv = ["codex", "exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "-"]
        p = run_process(argv, cwd=cwd, input_text=prompt, timeout=timeout, env=env)
        final = ""
        for line in p.stdout.splitlines():
            try:
                obj = json.loads(line)
            except Exception:
                continue
            item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
            if obj.get("type") == "item.completed" and item.get("type") == "agent_message":
                text = str(item.get("text") or "")
                if text:
                    final = text
        return p.returncode, final, p.stdout, p.stderr

    if spec["backend"] == "agy":
        argv = [
            "agy", "-p", prompt, "--model", str(spec["model"]),
            "--output-format", "stream-json", "--dangerously-skip-permissions",
            "--print-timeout", f"{max(1, timeout // 60)}m",
        ]
        p = run_process(argv, cwd=cwd, timeout=timeout, env=env)
        final = ""
        for line in p.stdout.splitlines():
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("event") == "result":
                result = obj.get("result", {})
                if isinstance(result, dict):
                    text = str(result.get("response") or "")
                    if text:
                        final = text
        return p.returncode, final, p.stdout, p.stderr

    argv = [
        "cmd", "-p", prompt, "--skip-onboarding", "--yolo",
        "--output-format", "json", "-m", str(spec["model"]),
    ]
    p = run_process(argv, cwd=cwd, timeout=timeout, env=env)
    final = ""
    for line in p.stdout.splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") == "result":
            text = str(obj.get("finalText") or "")
            if text:
                final = text
    return p.returncode, final, p.stdout, p.stderr


def classify_failure(rc: int, stdout: str, stderr: str) -> str | None:
    if rc == 0:
        return None
    text = f"{stdout}\n{stderr}".lower()
    if rc == 10 or re.search(r"insufficient credits|credit limit|credits? exhausted|quota exhausted|out of credits|no credits", text):
        return "CREDIT_EXHAUSTED"
    if re.search(r"usage limit|rate.?limit|quota|try again at|too many requests", text):
        return "RATE_LIMIT"
    if re.search(r"unauthori[sz]ed|authentication|not logged in|\b401\b|\b403\b", text):
        return "AUTH"
    if re.search(r"permission denied|not permitted|blocked by policy|approval", text):
        return "PERMISSION"
    if re.search(r"network issue|connection reset|temporar.*unavailable|dns|socket", text):
        return "NETWORK"
    return "PROVIDER_ERROR"


def parse_orch_status(text: str) -> str | None:
    matches = re.findall(r"(?im)^\s*ORCH_STATUS:\s*(COMPLETE|BLOCKED|NEEDS_USER|NEEDS_GO|FAILED)\s*$", text)
    return matches[-1].upper() if matches else None


def handoff_context(args: argparse.Namespace) -> tuple[str, str]:
    task = read_task(args)
    context = ""
    if args.context_file:
        context = Path(args.context_file).read_text()
    elif args.context:
        context = args.context
    else:
        jd = current_job_dir()
        if jd:
            cp = jd / "checkpoint.md"
            if cp.exists():
                context = cp.read_text()
    return task, context.strip()


def _v41_job_context(parent_job_id: str | None = None) -> str:
    jid = parent_job_id or job_id()
    tool = HOME / ".local/bin/orch-context"
    if not tool.exists(): return ""
    try:
        p = subprocess.run([str(tool), "build", "--job", jid], capture_output=True, text=True, timeout=15, env={**os.environ.copy(), "AI_ORCH_PROJECT_BASE": str(PROJECT_BASE), "AI_ORCH_JOB_ID": jid})
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""

def handoff_prompt(*, hid: str, caller: str, target: str, mode: str, repo: Path, task: str, context: str, reason: str | None) -> str:
    v41_context = _v41_job_context()
    mode_text = (
        "READ-ONLY successor. Work in an isolated detached worktree. Do not intentionally modify the authoritative checkout."
        if mode == "read_only"
        else "WRITE successor. You now own the synchronous MAIN continuation in the authoritative checkout. Preserve pre-existing user changes and modify only what the task requires."
    )
    return f"""PHASE 3 MAIN HANDOFF

HANDOFF ID: {hid}
FROM MAIN: {caller}
TO MAIN: {target}
MODE: {mode}
AUTHORITATIVE REPO: {repo}
REASON: {reason or '(not supplied)'}

You are the successor MAIN for the SAME user task. The previous MAIN is synchronously waiting for your result and must not continue editing after a successful write handoff.

Execution contract:
- {mode_text}
- Read AGENTS.md / repository governance before consequential work.
- Preserve historical evidence and project safety rules.
- Never perform human-GO-gated actions, credentials changes, private exchange actions, live/paper trading, destructive Git, or governance overrides without the user's explicit authorization.
- You MAY use `orch-delegate`, `orch-consult`, or `orch-parallel` for bounded checks if useful and delegate budget remains. Example:
    orch-consult --caller {target} --models auto --task "<bounded check>"
    orch-parallel --caller {target} --count 2 --models auto --task "<bounded check>"
- You MAY use `orch-collect` to inspect a Phase 3 batch.
- You MUST NOT call `orch-handoff`; handoff depth is capped at 1.
- Do not use provider-native agent/subagent spawning as the orchestration path.
- Worker claims are not authoritative; re-check important findings.
- Finish the user's task as far as safely possible.
- Your final response MUST include exactly one standalone completion line:
  ORCH_STATUS: COMPLETE|BLOCKED|NEEDS_USER|NEEDS_GO|FAILED

--- MULTIMODAL / SKILL CONTEXT ---
{v41_context or "(none)"}
--- END MULTIMODAL / SKILL CONTEXT ---

--- ORIGINAL USER TASK ---
{task}
--- END ORIGINAL USER TASK ---

--- HANDOFF CONTEXT / CHECKPOINT ---
{context or '(no additional context supplied; inspect current repo state directly)'}
--- END HANDOFF CONTEXT ---
"""


def handoff_cmd(args: argparse.Namespace) -> int:
    if int(os.environ.get("AI_ORCH_DELEGATION_DEPTH", "0") or "0") >= 1:
        print(json.dumps({"status": "BLOCKED", "blocker": "DELEGATED_WORKER_CANNOT_HANDOFF"}, indent=2))
        return 20
    if int(os.environ.get("AI_ORCH_HANDOFF_DEPTH", "0") or "0") >= 1:
        print(json.dumps({"status": "BLOCKED", "blocker": "MAX_HANDOFF_DEPTH_1"}, indent=2))
        return 21

    caller = (args.caller or "").strip()
    if not caller:
        raise SystemExit("--caller is required")

    if args.to == "auto":
        candidates = auto_targets(caller, 1, include_opus=args.include_opus)
        if not candidates:
            print(json.dumps({"status": "BLOCKED", "blocker": "NO_HANDOFF_TARGET"}, indent=2))
            return 24
        target = candidates[0]
    else:
        target = args.to
        if target not in TARGETS:
            raise SystemExit(f"unsupported handoff target: {target}")
        if target == "opus" and not args.include_opus:
            raise SystemExit("Opus is reserve-only; pass --include-opus")
        if target == caller_target(caller):
            raise SystemExit(f"self handoff prohibited: {caller} -> {target}")
        viable, detail = target_viable(target)
        if not viable and not args.ignore_health_quota:
            print(json.dumps({"status": "BLOCKED", "blocker": "TARGET_NOT_VIABLE", "detail": detail}, ensure_ascii=False, indent=2))
            return 24

    if args.mode == "write":
        active = running_write_delegate()
        if active:
            print(json.dumps({"status": "BLOCKED", "blocker": "WRITE_DELEGATE_ACTIVE", "active": active}, ensure_ascii=False, indent=2))
            return 25

    repo = Path(args.repo or os.environ.get("AI_ORCH_REPO") or os.getcwd()).expanduser().resolve()
    task, context = handoff_context(args)
    hid = new_id("handoff")
    hdir = HANDOFFS_DIR / hid
    hdir.mkdir(parents=True, exist_ok=False)
    meta = {
        "handoff_id": hid,
        "parent_job_id": job_id(),
        "caller": caller,
        "target": target,
        "mode": args.mode,
        "repo": str(repo),
        "reason": args.reason,
        "started_at": now(),
        "status": "RUNNING",
    }
    atomic_json(hdir / "handoff.json", meta)
    emit("HANDOFF_START", **meta)

    acquire_handoff_lock({"handoff_id": hid, "parent_job_id": meta["parent_job_id"], "target": target})
    workdir = repo
    ephemeral = False
    before = repo_snapshot(repo)
    started = now()

    try:
        if args.mode == "read_only":
            workdir, ephemeral = prepare_readonly_worktree(repo, hid)

        prompt = handoff_prompt(
            hid=hid, caller=caller, target=target, mode=args.mode, repo=repo,
            task=task, context=context, reason=args.reason,
        )
        (hdir / "handoff-prompt.md").write_text(prompt)

        env = os.environ.copy()
        env["AI_ORCH_HANDOFF_DEPTH"] = "1"
        env["AI_ORCH_DELEGATION_DEPTH"] = "0"
        env["AI_ORCH_MAIN_MODEL"] = target
        env["AI_ORCH_JOB_ID"] = meta["parent_job_id"]
        env["AI_ORCH_REPO"] = str(repo)
        env["AI_ORCH_PROJECT_BASE"] = str(PROJECT_BASE)
        env["GIT_OPTIONAL_LOCKS"] = "0"

        rc, final, raw_stdout, raw_stderr = invoke_target(target, workdir, prompt, args.timeout, env)
        (hdir / "provider.stdout.log").write_text(raw_stdout)
        (hdir / "provider.stderr.log").write_text(raw_stderr)
        (hdir / "successor-response.md").write_text(final)

        after = repo_snapshot(repo)
        orch_status = parse_orch_status(final)
        failure = classify_failure(rc, raw_stdout, raw_stderr)
        if failure:
            health_mark_failure(target, failure)
        elif rc == 0:
            health_mark_success(target)
        status = orch_status or ("FAILED" if rc else "UNSTRUCTURED")
        changed = changed_files_from_status(before.get("status"), after.get("status"))

        result = {
            **meta,
            "ended_at": now(),
            "duration_seconds": round(now() - started, 3),
            "status": status,
            "provider_returncode": rc,
            "failure_class": failure,
            "orch_status": orch_status,
            "response_path": str(hdir / "successor-response.md"),
            "authoritative_repo_before": before,
            "authoritative_repo_after": after,
            "observed_status_paths": changed,
            "read_only_ephemeral_worktree": str(workdir) if ephemeral else None,
            "trust_note": "Successor response is MAIN output, but evidence/facts remain subject to repository verification and governance.",
        }
        atomic_json(hdir / "handoff.json", result)
        emit(
            "HANDOFF_DONE",
            handoff_id=hid,
            parent_job_id=meta["parent_job_id"],
            caller=caller,
            target=target,
            mode=args.mode,
            status=status,
            duration_seconds=result["duration_seconds"],
            provider_returncode=rc,
            orch_status=orch_status,
        )

        print("ORCH_HANDOFF_RESPONSE")
        print(final.rstrip())
        print("ORCH_HANDOFF_META")
        print(json.dumps({
            "handoff_id": hid,
            "status": status,
            "target": target,
            "mode": args.mode,
            "provider_returncode": rc,
            "failure_class": failure,
            "observed_status_paths": changed,
            "result_path": str(hdir / "handoff.json"),
        }, ensure_ascii=False, indent=2))

        if rc != 0:
            return 40
        if orch_status is None:
            return 43
        return 0 if orch_status in {"COMPLETE", "BLOCKED", "NEEDS_USER", "NEEDS_GO"} else 41

    except KeyboardInterrupt:
        meta.update({"status": "CANCELLED", "ended_at": now()})
        atomic_json(hdir / "handoff.json", meta)
        emit("HANDOFF_DONE", handoff_id=hid, parent_job_id=meta["parent_job_id"], caller=caller, target=target, mode=args.mode, status="CANCELLED")
        return 130
    except subprocess.TimeoutExpired as e:
        meta.update({"status": "FAILED", "ended_at": now(), "error": f"timeout after {args.timeout}s"})
        atomic_json(hdir / "handoff.json", meta)
        emit("HANDOFF_DONE", handoff_id=hid, parent_job_id=meta["parent_job_id"], caller=caller, target=target, mode=args.mode, status="FAILED")
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        return 42
    except Exception as e:
        meta.update({"status": "FAILED", "ended_at": now(), "error": f"{type(e).__name__}: {e}"})
        atomic_json(hdir / "handoff.json", meta)
        emit("HANDOFF_DONE", handoff_id=hid, parent_job_id=meta["parent_job_id"], caller=caller, target=target, mode=args.mode, status="FAILED")
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        return 44
    finally:
        if ephemeral and workdir != repo:
            remove_worktree(repo, workdir)
        release_handoff_lock()


def selftest_cmd(_args: argparse.Namespace) -> int:
    assert normalized_claim("**Hello,  World!**") == "hello world"
    assert parse_orch_status("x\nORCH_STATUS: COMPLETE\n") == "COMPLETE"
    assert parse_orch_status("none") is None
    assert caller_pool("gemini-high") == "agy-gemini"
    assert caller_pool("Claude Sonnet 4.6") == "agy-third"
    assert caller_pool("MiMo V2.5 Pro") == "cmd"
    sample = {
        "batch_id": "x", "kind": "parallel", "parent_job_id": "j",
        "workers": [
            {"target": "a", "status": "COMPLETE", "summary": "ok", "result": {"findings": [{"claim": "Same claim", "confidence": "HIGH"}]}},
            {"target": "b", "status": "COMPLETE", "summary": "ok", "result": {"findings": [{"claim": "Same claim", "confidence": "MEDIUM"}]}},
        ],
    }
    coll = build_collection(sample)
    assert coll["status"] == "COMPLETE"
    assert coll["mechanical_exact_agreement"][0]["count"] == 2
    print("Phase 3 selftest PASS (no provider quota used)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="OrchBridge Phase 3 collaboration control plane")
    sub = p.add_subparsers(dest="command", required=True)

    def add_task_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--caller", required=True)
        sp.add_argument("--task")
        sp.add_argument("--task-file")
        sp.add_argument("--models", default="auto", help="auto or comma-separated targets")
        sp.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
        sp.add_argument("--include-opus", action="store_true", help="allow reserve Opus pool")
        sp.add_argument("--fresh", action="store_true", help="force a fresh single consultation")

    c = sub.add_parser("consult", help="one independent read-only second opinion")
    add_task_flags(c)
    c.set_defaults(func=lambda a: run_batch(a, "consult"), count=1, mode="read_only")

    par = sub.add_parser("parallel", help="run 1-3 independent workers concurrently")
    add_task_flags(par)
    par.add_argument("--count", type=int, default=None)
    par.add_argument("--mode", choices=["read_only", "write"], default="read_only")
    par.set_defaults(func=lambda a: run_batch(a, "parallel"))

    collect = sub.add_parser("collect", help="mechanically aggregate one Phase 3 batch")
    collect.add_argument("--batch", default="latest")
    collect.add_argument("--job")
    collect.set_defaults(func=collect_cmd)

    ls = sub.add_parser("list", help="list recent collaboration batches/handoffs")
    ls.add_argument("--job")
    ls.add_argument("--limit", type=int, default=20)
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=list_cmd)

    show = sub.add_parser("show", help="show a batch or handoff record")
    show.add_argument("id")
    show.set_defaults(func=show_cmd)

    h = sub.add_parser("handoff", help="synchronously transfer MAIN continuation to another provider")
    h.add_argument("--caller", required=True)
    h.add_argument("--to", default="auto", choices=["auto", *sorted(TARGETS)])
    h.add_argument("--mode", choices=["read_only", "write"], default="read_only")
    h.add_argument("--repo")
    h.add_argument("--task")
    h.add_argument("--task-file")
    h.add_argument("--context")
    h.add_argument("--context-file")
    h.add_argument("--reason")
    h.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    h.add_argument("--include-opus", action="store_true")
    h.add_argument("--ignore-health-quota", action="store_true")
    h.set_defaults(func=handoff_cmd)

    st = sub.add_parser("selftest", help="no-provider Phase 3 structural self-test")
    st.set_defaults(func=selftest_cmd)
    return p


def main() -> int:
    PHASE3_DIR.mkdir(parents=True, exist_ok=True)
    BATCHES_DIR.mkdir(parents=True, exist_ok=True)
    HANDOFFS_DIR.mkdir(parents=True, exist_ok=True)
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

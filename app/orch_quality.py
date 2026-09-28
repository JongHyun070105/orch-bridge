#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import json
import os
import re
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_DEEP = ("root cause", "원인", "디버그", "debug", "race", "concurrency", "architecture", "설계", "감사", "audit", "검증", "validation", "증거", "evidence", "holdout", "과학", "scientific")
_CRITICAL = ("live", "실거래", "private api", "aws", "iam", "security", "보안", "삭제", "destructive", "final holdout")
_MUTATE = ("고쳐", "수정", "구현", "implement", "fix", "refactor", "작성", "create", "patch", "change")
_BROAD = ("전체", "모두", "전부", "종합", "comprehensive", "repo", "프로젝트", "architecture")


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def task_tags(text: str, assessment: Any | None = None) -> list[str]:
    t = (text or "").lower()
    tags: list[str] = []

    def a(name: str, default: float = 0.0) -> float:
        if assessment is None:
            return default
        if isinstance(assessment, dict):
            value = assessment.get(name, default)
        else:
            value = getattr(assessment, name, default)
        try:
            return float(value)
        except Exception:
            return default

    if any(x in t for x in _DEEP) or a("reasoning") >= 0.65:
        tags.append("deep")
    if any(x in t for x in _CRITICAL) or a("risk") >= 0.65:
        tags.append("critical")
    if any(x in t for x in _MUTATE) or a("write_likelihood") >= 0.45:
        tags.append("mutate")
    if any(x in t for x in _BROAD) or a("scope") >= 0.60:
        tags.append("broad")
    if a("crosscheck") >= 0.70:
        tags.append("crosscheck")
    if re.fullmatch(r"\s*(안녕+|ㅎㅇ+|하이+|hello|hi|hey|고마워+|감사+|ㅇㅇ+|오케이+|ok|okay)[.!?~ ]*", t):
        tags.append("casual")
    if not tags:
        tags.append("general")
    return sorted(set(tags))


def independent_reviewer(current_vendor: str, candidate_vendor: str) -> bool:
    """A review only counts as independent when model vendors differ."""
    return bool(current_vendor and candidate_vendor and current_vendor != candidate_vendor)


def _stat_bucket(data: dict[str, Any], tag: str, candidate_key: str) -> dict[str, Any]:
    tags = data.setdefault("tags", {})
    tag_row = tags.setdefault(tag, {})
    return tag_row.setdefault(candidate_key, {
        "attempts": 0,
        "successes": 0,
        "failures": 0,
        "review_accepts": 0,
        "last_at": None,
    })


def record_router_outcome(
    path: Path,
    *,
    candidate_key: str,
    tags: list[str],
    success: bool,
    phase: str,
    terminal_status: str | None = None,
    failure_kind: str | None = None,
) -> None:
    """Record quality evidence separately from provider health.

    Only call this for quality-bearing outcomes: a valid terminal result, explicit
    task failure, or continuation exhaustion. Rate-limit/auth/network failures are
    availability evidence and should not be written here.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        data = _load(path)
        if int(data.get("schema_version") or 0) != SCHEMA_VERSION:
            data = {"schema_version": SCHEMA_VERSION, "tags": {}, "events": []}
        all_tags = sorted(set(["all", *tags]))
        ts = time.time()
        for tag in all_tags:
            bucket = _stat_bucket(data, tag, candidate_key)
            bucket["attempts"] = int(bucket.get("attempts") or 0) + 1
            if success:
                bucket["successes"] = int(bucket.get("successes") or 0) + 1
            else:
                bucket["failures"] = int(bucket.get("failures") or 0) + 1
            if phase == "review" and success:
                bucket["review_accepts"] = int(bucket.get("review_accepts") or 0) + 1
            bucket["last_at"] = ts

        events = data.setdefault("events", [])
        events.append({
            "at": ts,
            "candidate": candidate_key,
            "tags": tags,
            "success": bool(success),
            "phase": phase,
            "terminal_status": terminal_status,
            "failure_kind": failure_kind,
        })
        # Keep enough audit history to diagnose routing without unbounded growth.
        if len(events) > 500:
            del events[:-500]
        data["updated_at"] = ts
        atomic_json(path, data)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def router_learning_adjustment(
    path: Path,
    *,
    candidate_key: str,
    tags: list[str],
    min_observations: int = 3,
    max_abs: float = 0.12,
) -> tuple[float, dict[str, Any]]:
    """Return a small bounded empirical quality adjustment.

    Uses Laplace/Beta(2,2) smoothing. Specific task tags are preferred; if no
    specific tag has enough observations, the global `all` bucket is used.
    """
    data = _load(path)
    tag_data = data.get("tags", {}) if isinstance(data, dict) else {}
    samples: list[tuple[str, int, int]] = []
    for tag in tags:
        row = tag_data.get(tag, {}).get(candidate_key, {}) if isinstance(tag_data.get(tag, {}), dict) else {}
        n = int(row.get("attempts") or 0)
        s = int(row.get("successes") or 0)
        if n >= min_observations:
            samples.append((tag, n, s))

    if not samples:
        row = tag_data.get("all", {}).get(candidate_key, {}) if isinstance(tag_data.get("all", {}), dict) else {}
        n = int(row.get("attempts") or 0)
        s = int(row.get("successes") or 0)
        if n >= min_observations:
            samples.append(("all", n, s))

    if not samples:
        return 0.0, {"observations": 0, "score": None, "tags": []}

    total_n = sum(n for _, n, _ in samples)
    total_s = sum(s for _, _, s in samples)
    # Smoothed success probability with a neutral prior.
    score = (total_s + 2.0) / (total_n + 4.0)
    # 50% is neutral. The bounded adjustment prevents historical data from
    # overpowering capability/risk/quota routing.
    adjustment = max(-max_abs, min(max_abs, (score - 0.5) * (max_abs * 2.0)))
    return adjustment, {
        "observations": total_n,
        "successes": total_s,
        "score": round(score, 4),
        "tags": [tag for tag, _, _ in samples],
    }


def learning_summary(path: Path) -> dict[str, Any]:
    return _load(path)

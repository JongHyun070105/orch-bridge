#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable

from orch_quality import (
    independent_reviewer,
    record_router_outcome,
    router_learning_adjustment,
    task_tags,
)
from provider_runtime import effective_provider_enabled, resolve_provider_cli


def _quiet_keyboard_interrupt_excepthook(exc_type, exc, tb):
    if issubclass(exc_type, KeyboardInterrupt):
        # Ctrl+C is a normal user cancellation, not an application error.
        print("[ai-orch] interrupted", file=sys.stderr, flush=True)
        return
    sys.__excepthook__(exc_type, exc, tb)

sys.excepthook = _quiet_keyboard_interrupt_excepthook

HOME = Path.home()
CONFIG_PATH = HOME / ".config/orchbridge/config.json"
AGY_CACHE = HOME / ".cache/orchbridge/agy-status.json"
ROUTER_STATE = HOME / ".cache/orchbridge/router-state.json"
ROUTER_LEARNING = HOME / ".cache/orchbridge/router-learning.json"
CLAUDE_USAGE_CACHE = HOME / ".cache/orchbridge/claude-usage.json"
ROUTER_STATE.parent.mkdir(parents=True, exist_ok=True)

RATE_LIMIT_RE = re.compile(
    r"(rate|usage|message|hourly|daily|weekly).*?(limit|quota).*?"
    r"(reached|exceeded|exhausted)|resource[_ ]?exhausted|"
    r"quota.*?(exhausted|exceeded)|usage limit",
    re.I | re.S,
)
PERMISSION_RE = re.compile(
    r"permission.*?(auto-denied|cannot prompt|denied)|required the .*? permission",
    re.I | re.S,
)
AUTH_RE = re.compile(
    r"authentication required|please log in|login expired|authentication failed",
    re.I | re.S,
)

def log(msg: str) -> None:
    print(f"[ai-orch] {msg}", file=sys.stderr, flush=True)

def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {} if default is None else default

def save_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)

def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    h, rem = divmod(seconds, 3600)
    return f"{h}h{rem // 60:02d}m"

CLAUDE_SUBSCRIPTION_OVERRIDE_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)

def _truthy_override(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"", "0", "false", "no", "off", "none", "null"}

def claude_subscription_settings_issue(repo_path: str | Path | None = None) -> str | None:
    """Reject settings that would route native Claude Code away from the Pro subscription."""
    base = Path(repo_path or repo).expanduser().resolve() if "repo" in globals() else Path(repo_path or os.getcwd()).expanduser().resolve()
    paths = [
        HOME / ".claude/settings.json",
        base / ".claude/settings.json",
        base / ".claude/settings.local.json",
    ]
    for path in paths:
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            continue
        except Exception:
            continue
        env = data.get("env", {}) if isinstance(data, dict) else {}
        if not isinstance(env, dict):
            continue
        for key in CLAUDE_SUBSCRIPTION_OVERRIDE_KEYS:
            if _truthy_override(env.get(key)):
                return f"{path}: env.{key} overrides native Claude subscription routing"
    return None

def claude_subscription_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Use Claude Code OAuth/subscription auth, never inherited API/gateway billing overrides."""
    env = dict(extra) if extra is not None else os.environ.copy()
    for key in CLAUDE_SUBSCRIPTION_OVERRIDE_KEYS:
        env.pop(key, None)
    return env

def claude_subscription_ready() -> tuple[bool, str | None]:
    if not resolve_provider_cli("claude", config):
        return False, "Claude Code CLI not installed or not executable"
    issue = claude_subscription_settings_issue(repo)
    if issue:
        return False, issue
    return True, None

config = load_json(CONFIG_PATH, {})
repo = str(Path(os.environ.get(
    "AI_ORCH_REPO",
    config.get("default_repo", os.getcwd()),
)).expanduser().resolve())

task = " ".join(sys.argv[1:]).strip()
if not task and not sys.stdin.isatty():
    task = sys.stdin.read().strip()
if not task:
    print('usage: ai-orch "task"', file=sys.stderr)
    raise SystemExit(1)

decision_text = os.environ.get("AI_ORCH_USER_MESSAGE", task).strip() or task
permission_profile = os.environ.get("AI_ORCH_PERMISSION_PROFILE", "trusted").strip().lower()
if permission_profile not in {"trusted", "guarded"}:
    permission_profile = "trusted"

state = load_json(ROUTER_STATE, {
    "last_success": None,
    "blocked_until": {},
    "failures": {},
    "last_decision": None,
})
for k, v in (
    ("blocked_until", {}),
    ("failures", {}),
):
    state.setdefault(k, v)
state.setdefault("last_success", None)
state.setdefault("last_decision", None)

def save_state() -> None:
    save_json_atomic(ROUTER_STATE, state)

def block_info(key: str) -> dict[str, Any] | None:
    entry = state["blocked_until"].get(key)
    if not isinstance(entry, dict):
        return None
    try:
        until = float(entry.get("until", 0))
    except Exception:
        return None
    if until <= time.time():
        state["blocked_until"].pop(key, None)
        save_state()
        return None
    return entry

def blocked(key: str) -> bool:
    return block_info(key) is not None

def log_blocked(label: str, key: str) -> bool:
    info = block_info(key)
    if not info:
        return False
    log(
        f"skip {label}: cooldown "
        f"{fmt_duration(float(info['until']) - time.time())} "
        f"({info.get('reason', 'temporary failure')})"
    )
    return True

def failure_count(key: str) -> int:
    return int(state["failures"].get(key, 0))

def clear_failure(key: str) -> None:
    state["failures"].pop(key, None)
    state["blocked_until"].pop(key, None)
    save_state()

def block_with_backoff(key: str, reason: str, base_seconds: int = 300) -> None:
    count = failure_count(key) + 1
    state["failures"][key] = count
    mults = [1, 3, 6, 12]
    seconds = min(base_seconds * mults[min(count - 1, len(mults) - 1)], 3600)
    state["blocked_until"][key] = {
        "until": time.time() + seconds,
        "reason": reason,
        "failure_count": count,
    }
    save_state()
    log(f"{key} cooldown: {fmt_duration(seconds)} (failure #{count}: {reason})")

def block_until(key: str, until: float, reason: str) -> None:
    state["blocked_until"][key] = {
        "until": until,
        "reason": reason,
        "failure_count": failure_count(key),
    }
    save_state()
    log(f"{key} blocked until reset ({fmt_duration(until-time.time())}; {reason})")

def record_success(backend: str, model: str | None, pool: str | None) -> None:
    state["last_success"] = {
        "backend": backend,
        "model": model,
        "pool": pool,
        "at": time.time(),
    }
    clear_failure(backend)
    if pool:
        clear_failure(f"{backend}:{pool}")
    save_state()

@dataclass
class Assessment:
    reasoning: float
    uncertainty: float
    scope: float
    risk: float
    crosscheck: float
    write_likelihood: float
    source: str

    @property
    def need(self) -> float:
        n = (
            0.12
            + 0.34 * self.reasoning
            + 0.15 * self.uncertainty
            + 0.14 * self.scope
            + 0.12 * self.risk
            + 0.13 * self.crosscheck
        )
        if self.risk >= 0.8 or self.crosscheck >= 0.9:
            n = max(n, 0.82)
        return max(0.05, min(0.98, n))

def clamp(x: Any, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except Exception:
        return default

def is_casual(text: str) -> bool:
    t = text.strip().lower()
    return bool(re.fullmatch(
        r"(안녕+|ㅎㅇ+|하이+|hello|hi|hey|고마워+|감사+|ㅇㅇ+|오케이+|ok|okay)[.!?~ ]*",
        t,
    ))

def heuristic_assessment(text: str) -> Assessment:
    t = text.lower()
    if is_casual(text):
        return Assessment(.08, .05, .05, .02, .02, .01, "local-casual")

    reasoning = .35
    uncertainty = .35
    scope = .25
    risk = .20
    crosscheck = .25
    write = .15

    deep = ("root cause", "원인", "디버그", "debug", "race", "concurrency",
            "architecture", "설계", "감사", "audit", "검증", "validation",
            "증거", "evidence", "holdout", "과학", "scientific")
    critical = ("live", "실거래", "private api", "aws", "iam", "security",
                "보안", "삭제", "destructive", "final holdout")
    mutate = ("고쳐", "수정", "구현", "implement", "fix", "refactor", "작성", "create")
    broad = ("전체", "모두", "전부", "종합", "comprehensive", "repo", "프로젝트")

    if any(x in t for x in deep):
        reasoning += .30
        uncertainty += .20
        crosscheck += .25
    if any(x in t for x in critical):
        risk += .55
        crosscheck += .30
    if any(x in t for x in mutate):
        write += .55
        reasoning += .10
    if any(x in t for x in broad):
        scope += .40
        uncertainty += .10
    if len(text) > 500:
        scope += .20
    if "?" in text or "모르" in t or "uncertain" in t:
        uncertainty += .10

    return Assessment(
        clamp(reasoning), clamp(uncertainty), clamp(scope),
        clamp(risk), clamp(crosscheck), clamp(write), "local-fallback",
    )

def parse_cmd_ndjson(stdout: str) -> tuple[str, dict[str, Any] | None]:
    final_text = ""
    result_obj = None
    for line in stdout.splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") == "result":
            result_obj = obj
            final_text = str(obj.get("finalText", ""))
    return final_text, result_obj

def microjudge_assessment(text: str) -> Assessment | None:
    dcfg = config.get("decision", {})
    if not dcfg.get("enabled", True) or is_casual(text):
        return None

    model = dcfg.get("judge_model", "xiaomi/mimo-v2.5-pro")
    effort = dcfg.get("judge_effort", "low")
    timeout = int(dcfg.get("judge_timeout_seconds", 75))

    prompt = f"""Assess the CURRENT user task for an AI coding/research orchestrator.
Do NOT solve the task. Return JSON only, no markdown.

Task:
{text}

Return exactly:
{{
  "reasoning": 0.0,
  "uncertainty": 0.0,
  "scope": 0.0,
  "risk": 0.0,
  "crosscheck": 0.0,
  "write_likelihood": 0.0
}}

All numbers are 0.0-1.0.
reasoning: depth of reasoning needed.
uncertainty: likelihood hidden complexity appears after inspection.
scope: breadth/files/subsystems likely involved.
risk: consequences of a wrong answer/action.
crosscheck: value of an independent second model.
write_likelihood: likelihood the task requires modifying files.
"""
    judge_exe = resolve_provider_cli("commandcode", config)
    if not judge_exe:
        log("decision micro-judge unavailable: Command Code executable not found")
        return None
    cmd = [
        judge_exe, "-p", prompt,
        "--plan", "--skip-onboarding",
        "--output-format", "json",
        "--max-turns", "1",
        "-m", model,
    ]
    try:
        r = subprocess.run(
            cmd, cwd=repo, capture_output=True, text=True, timeout=timeout,
        )
    except Exception as e:
        log(f"decision micro-judge unavailable: {e}")
        return None

    if r.returncode in (5, 10):
        block_with_backoff(
            "commandcode",
            "judge rate/credit limit",
            900 if r.returncode == 5 else 1800,
        )
        return None
    if r.returncode != 0:
        log(f"decision micro-judge failed: exit={r.returncode}")
        return None

    final, _ = parse_cmd_ndjson(r.stdout)
    if not final:
        final = r.stdout.strip()

    m = re.search(r"\{.*\}", final, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return None

    return Assessment(
        clamp(obj.get("reasoning")),
        clamp(obj.get("uncertainty")),
        clamp(obj.get("scope")),
        clamp(obj.get("risk")),
        clamp(obj.get("crosscheck")),
        clamp(obj.get("write_likelihood")),
        "cmd-microjudge",
    )

assessment = microjudge_assessment(decision_text) or heuristic_assessment(decision_text)
state["last_decision"] = {
    **asdict(assessment),
    "need": assessment.need,
    "task_preview": decision_text[:300],
    "at": time.time(),
}
save_state()
log(
    "decision: "
    f"need={assessment.need:.2f} reason={assessment.reasoning:.2f} "
    f"uncertainty={assessment.uncertainty:.2f} scope={assessment.scope:.2f} "
    f"risk={assessment.risk:.2f} crosscheck={assessment.crosscheck:.2f} "
    f"source={assessment.source}"
)

# ----------------------------------------------------------------------
# Quota snapshot
# ----------------------------------------------------------------------
agy_status = load_json(AGY_CACHE, {})
quota = agy_status.get("quota", {}) if isinstance(agy_status, dict) else {}
quota_at = agy_status.get("_quota_captured_at_unix", agy_status.get("_captured_at_unix"))
routing_cfg = config.get("routing", {})
max_age = int(routing_cfg.get("quota_max_age_seconds", 3600))

try:
    quota_fresh = time.time() - float(quota_at) <= max_age
except Exception:
    quota_fresh = False

def pool_quota(prefix: str) -> tuple[float | None, float | None]:
    vals, resets = [], []
    if not quota_fresh:
        return None, None
    for key, value in quota.items():
        if not str(key).startswith(prefix) or not isinstance(value, dict):
            continue
        try:
            vals.append(float(value["remaining_fraction"]))
        except Exception:
            pass
        try:
            resets.append(float(value["reset_in_seconds"]))
        except Exception:
            pass
    return (min(vals) if vals else None, max(resets) if resets else None)

gemini_q, gemini_reset = pool_quota("gemini-")
third_q, third_reset = pool_quota("3p-")

def fmt_q(v: float | None) -> str:
    return "unknown" if v is None else f"{v*100:.1f}%"

log(f"quota: 3p={fmt_q(third_q)} gemini={fmt_q(gemini_q)} claude-pro=subscription")

if gemini_q is not None and gemini_q <= 0 and gemini_reset:
    block_until("agy:gemini", time.time() + gemini_reset, "cached Gemini quota exhausted")
if third_q is not None and third_q <= 0 and third_reset:
    block_until("agy:third", time.time() + third_reset, "cached 3p quota exhausted")

models = config.get("models", {})
providers_cfg = config.get("providers", {}) if isinstance(config.get("providers", {}), dict) else {}
PROVIDER_BINARIES = {"codex": "codex", "claude": "claude", "agy": "agy", "commandcode": "cmd"}

def provider_enabled(backend: str) -> bool:
    # Keep public provider on/off/auto semantics, but resolve CLIs through PATH,
    # common runtime locations, NVM bins, and login/interactive shell PATH.
    return effective_provider_enabled(config, backend)

last = state.get("last_success") if isinstance(state.get("last_success"), dict) else {}

@dataclass
class Candidate:
    key: str
    label: str
    backend: str
    vendor: str
    model: str | None
    pool: str | None
    capability: float
    cost: float
    quota: float | None = None
    reset_in: float | None = None
    utility: float = -999.0
    quality_adjustment: float = 0.0
    quality_observations: int = 0

def quota_availability(pool: str | None, q: float | None, reset_in: float | None) -> float:
    if pool is None:
        return 0.72
    reserve = float(routing_cfg.get(
        "gemini_reserve" if pool == "gemini" else "third_party_reserve",
        0.10,
    ))
    reset_soon = float(routing_cfg.get("reset_soon_seconds", 3600))

    if q is None:
        return 0.38 if pool == "gemini" else 0.58

    if q < reserve:
        if reset_in is not None and reset_in <= reset_soon:
            return min(0.85, 0.55 + 0.30 * (1 - reset_in / max(reset_soon, 1)))
        return max(0.05, 0.25 * q / max(reserve, 0.001))

    bonus = 0.15 if reset_in is not None and reset_in <= reset_soon else 0.0
    return min(1.0, 0.35 + 0.65 * q + bonus)

candidates = [
    Candidate("cmd", "Command Code", "commandcode", "xiaomi",
              models.get("commandcode", "xiaomi/mimo-v2.5-pro"), None, .50, .12),
    Candidate("gemini-low", "AGY/Gemini 3.8 Flash Low", "agy", "google",
              models.get("gemini_low", "gemini-3.8-flash-low"),
              "gemini", .48, .10, gemini_q, gemini_reset),
    Candidate("gemini-medium", "AGY/Gemini 3.8 Flash Medium", "agy", "google",
              models.get("gemini_medium", "gemini-3.8-flash-medium"),
              "gemini", .63, .18, gemini_q, gemini_reset),
    Candidate("gemini-high", "AGY/Gemini 3.8 Flash High", "agy", "google",
              models.get("gemini_high", "gemini-3.8-flash-high"),
              "gemini", .80, .29, gemini_q, gemini_reset),
    Candidate("claude-sonnet", "Claude Code/Sonnet (Pro)", "claude", "anthropic",
              models.get("claude_sonnet", "sonnet"),
              "claude-pro", .95, .46),
    Candidate("sonnet", "AGY/Sonnet 4.6 Thinking", "agy", "anthropic",
              models.get("sonnet", "claude-sonnet-4-6-thinking"),
              "third", .94, .50, third_q, third_reset),
    Candidate("codex", "Codex", "codex", "openai", None, None, .93, .62),
    Candidate("claude-opus", "Claude Code/Opus (Pro)", "claude", "anthropic",
              models.get("claude_opus", "opus"),
              "claude-pro", .99, .90),
    Candidate("opus", "AGY/Opus 4.6 Thinking", "agy", "anthropic",
              models.get("opus", "claude-opus-4-6-thinking"),
              "third", .98, .88, third_q, third_reset),
]

force = os.environ.get("AI_ORCH_FORCE_MODEL", "").strip().lower()
force_alias = {
    "cmd": "cmd", "commandcode": "cmd",
    "gemini-low": "gemini-low", "gemini-medium": "gemini-medium", "gemini-high": "gemini-high",
    "sonnet": "sonnet",
    "claude": "claude-sonnet", "claude-sonnet": "claude-sonnet",
    "claude-opus": "claude-opus",
    "codex": "codex", "opus": "opus",
}

def candidate_blocked(c: Candidate) -> bool:
    if not provider_enabled(c.backend):
        return True
    if blocked(c.backend):
        return True
    if c.backend == "claude":
        ready, reason = claude_subscription_ready()
        if not ready:
            return True
        if c.pool and blocked(f"claude:{c.pool}"):
            return True
        return False
    if c.backend == "agy" and blocked("agy"):
        return True
    if c.backend == "agy" and c.pool and blocked(f"agy:{c.pool}"):
        return True
    return False

if force and force != "auto":
    key = force_alias.get(force)
    if not key:
        log(f"unknown AI_ORCH_FORCE_MODEL={force}; using auto")
    else:
        candidates = sorted(candidates, key=lambda c: 0 if c.key == key else 1)
        assessment = Assessment(1, 1, 1, 1, 1, assessment.write_likelihood, "forced")
        log(f"manual model override: {key}")

need = assessment.need
quality_tags = task_tags(decision_text, assessment)
quality_enabled = bool(routing_cfg.get("quality_learning_enabled", True))
quality_weight = max(0.0, min(0.20, float(routing_cfg.get("quality_learning_weight", 0.10))))
quality_min_obs = max(2, int(routing_cfg.get("quality_learning_min_observations", 3)))
state["last_decision"]["quality_tags"] = quality_tags
save_state()

for c in candidates:
    if candidate_blocked(c):
        c.utility = -100
        continue

    avail = quota_availability(c.pool, c.quota, c.reset_in)
    under = max(0.0, need - c.capability)
    over = max(0.0, c.capability - need)

    # Prefer the least expensive model that is comfortably sufficient.
    u = (
        1.10
        - 3.6 * under
        - 0.42 * over
        + 0.62 * avail
        - 0.38 * c.cost
    )

    if last:
        if last.get("backend") == c.backend:
            u += 0.05
        if last.get("model") == c.model:
            u += 0.08
        if c.pool and last.get("pool") == c.pool:
            u += 0.04

    # High-risk work should not be routed to a weak candidate merely because it is cheap.
    if assessment.risk > .65 and c.capability < .80:
        u -= .70
    if assessment.crosscheck > .80 and c.capability < .75:
        u -= .35

    if quality_enabled:
        qadj, qmeta = router_learning_adjustment(
            ROUTER_LEARNING,
            candidate_key=c.key,
            tags=quality_tags,
            min_observations=quality_min_obs,
            max_abs=quality_weight,
        )
        c.quality_adjustment = qadj
        c.quality_observations = int(qmeta.get("observations") or 0)
        u += qadj

    c.utility = u

if not (force and force != "auto" and force_alias.get(force)):
    candidates.sort(key=lambda c: c.utility, reverse=True)

log("route: " + " > ".join(
    f"{c.key}({c.utility:.2f}"
    f"{',q%+.2f' % c.quality_adjustment if c.quality_adjustment else ''})"
    for c in candidates[:5]
))

def orchestrator_prompt(main_name: str, original_task: str, prior: str | None = None) -> str:
    prior_section = ""
    if prior:
        prior_section = f"""
PRIOR LOWER-TIER RESULT TO VERIFY:
{prior}

This is an escalation/review pass. Verify the prior result against current
repository evidence and correct it rather than merely agreeing with it.
"""
    return f"""You are the MAIN orchestrator for the current task.

Repository:
{repo}
Current MAIN:
{main_name}

Before substantive conclusions:
- read and obey AGENTS.md
- use the orchbridge skill when available
- inspect current Git/repository/evidence state directly
- treat conversation/handoff text as context, not mutable ground truth
- delegate through orch-delegate only when it materially improves the result
- before delegating, inspect/list active or recent delegations and avoid semantically
  duplicating work that is already running or already has sufficient evidence

LOCAL EXECUTION PERMISSIONS:
- Permission profile: {permission_profile}.
- In trusted mode, routine reversible local development actions are pre-authorized: inspect/edit files in the current repository, create/move task-related files, run build/test/lint/format commands, install project-scoped dependencies with the repository's package manager, use read-only network/package/Git metadata operations, and create/switch local Git branches when useful.
- Branch autonomy is enabled for MAIN. Prefer `orch-branch status|list|switch|new|back` for branch lifecycle changes. You may create a task branch or switch to an existing clean branch when that materially improves isolation or matches the task.
- Record important branch changes in your progress/final report; do not silently strand work on an unexpected branch.
- In guarded mode, take only clearly necessary local actions and avoid optional mutations.
- These permissions do NOT authorize destructive Git (`reset --hard`, `clean`, branch deletion), force-push, automatic merge/push, credential changes, purchases, private/live trading, or destructive/high-impact cloud/AWS operations. Those retain the existing explicit user/GO gates.

Safety:
- no private exchange APIs, balances, orders, withdrawals, paper/live trading
- preserve historical PASS/FAIL evidence
- do not weaken validation gates to manufacture PASS
- protect unrelated dirty/untracked files
- one writer per checkout; use worktrees for concurrent writers
- high-impact AWS/infrastructure operations require the project's human GO gate

The current user message is authoritative. Do not resume an older task unless
the current message actually asks to continue it.

TASK COMPLETION PROTOCOL:
- Do the requested work before announcing completion.
- Do not stop after saying what you are about to inspect, run, verify, or change.
- Tool/backend failures are not task completion. Recover, use another permitted
  method, or report a genuine task-level blocker.
- Your FINAL response MUST end with exactly one machine-readable line:
  ORCH_STATUS: COMPLETE
  ORCH_STATUS: BLOCKED
  ORCH_STATUS: NEEDS_USER
  ORCH_STATUS: NEEDS_GO
  ORCH_STATUS: FAILED
- Use COMPLETE only when the requested work for this turn is actually finished.
- Use BLOCKED only for a genuine external/task blocker that another model cannot
  solve merely by trying harder.
- Use NEEDS_USER only when essential user information or a user choice is required.
- Use NEEDS_GO when the project's explicit human GO gate is reached. Stop there;
  never bypass the gate.
- Use FAILED only when the task itself could not be completed and no safe permitted
  recovery remains.
- Never emit ORCH_STATUS before the final line.
- During substantial work, emit short user-visible progress updates before
  important tool phases and after important evidence is found. State WHAT you
  are checking and WHAT was observed; do not expose hidden chain-of-thought.

{prior_section}
USER TASK:
{original_task}
""".strip()

@dataclass
class Outcome:
    ok: bool
    text: str
    kind: str
    detail: str = ""
    task_status: str | None = None


def _missing_cli_outcome(name: str) -> Outcome:
    return Outcome(
        False,
        "",
        "missing_binary",
        f"{name} executable not found via configured binary, override, PATH, common runtime paths, or login/interactive shell PATH",
    )

def run_with_heartbeat(argv: list[str], timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    start = time.monotonic()
    proc = subprocess.Popen(
        argv, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    stderr_parts: list[str] = []

    def stderr_reader() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_parts.append(line)

    t = threading.Thread(target=stderr_reader, daemon=True)
    t.start()
    while proc.poll() is None:
        if time.monotonic() - start > timeout:
            proc.kill()
            break
        elapsed = int(time.monotonic() - start)
        if elapsed and elapsed % 8 == 0:
            log(f"working: {elapsed}s")
        time.sleep(1)
    out = proc.stdout.read() if proc.stdout else ""
    rc = proc.wait()
    t.join(timeout=1)
    return subprocess.CompletedProcess(argv, rc, out, "".join(stderr_parts))


def _redact_progress(value: Any) -> str:
    try:
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, default=str)
        else:
            text = str(value)
    except Exception:
        text = repr(value)

    text = re.sub(
        r'(?i)(access[_-]?key|secret[_-]?key|api[_-]?key|token|password)'
        r'(\s*["\']?\s*[:=]\s*["\']?)[^\s,"\'}]+',
        r'\1\2***',
        text,
    )
    text = " ".join(text.split())
    return text if len(text) <= 700 else text[:697] + "..."

def _model_progress_label(model: str | None, backend: str) -> str:
    m = (model or "").lower()
    if backend == "claude" and "opus" in m:
        return "Claude-Pro/Opus"
    if backend == "claude":
        return "Claude-Pro/Sonnet"
    if "sonnet" in m:
        return "Sonnet"
    if "opus" in m:
        return "Opus"
    if "gemini-3.8-flash-high" in m:
        return "Gemini-High"
    if "gemini-3.8-flash-medium" in m:
        return "Gemini-Medium"
    if "gemini-3.8-flash-low" in m:
        return "Gemini-Low"
    if backend == "commandcode":
        return "CMD"
    if backend == "codex":
        return "Codex"
    return model or backend

def _emit_visible_progress(label: str, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    for paragraph in re.split(r"\n+", text):
        paragraph = paragraph.strip()
        if paragraph:
            log(f"[{label}] {paragraph}")

def _phase1_agy_caller(model: str) -> str:
    m = (model or "").lower()
    if "sonnet" in m:
        return "sonnet"
    if "opus" in m:
        return "opus"
    if "gemini-3.8-flash-high" in m:
        return "gemini-high"
    if "gemini-3.8-flash-medium" in m:
        return "gemini-medium"
    if "gemini-3.8-flash-low" in m:
        return "gemini-low"
    if "mimo" in m or "command code" in m:
        return "cmd"
    if "gpt-6" in m or "codex" in m:
        return "codex"
    return "agy"


def _phase1_wrap_prompt(prompt: str, caller: str) -> str:
    if str(os.environ.get("AI_ORCH_DELEGATION_DEPTH", "0")) != "0":
        return prompt

    strict = str(os.environ.get("AI_ORCH_STRICT_TASK", "0")) == "1"
    prompt_sha = str(os.environ.get("AI_ORCH_PROMPT_SHA256", ""))[:12]
    max_delegates = str(os.environ.get("AI_ORCH_MAX_DELEGATES_PER_JOB", "3"))
    handoff_depth = str(os.environ.get("AI_ORCH_HANDOFF_DEPTH", "0"))

    instructions = f"""CROSS-PROVIDER ORCHESTRATION — PHASE 3 CONTROL PLANE

You are the MAIN/supervisor.
Caller identity: {caller}
Current prompt fingerprint: {prompt_sha or "(unavailable)"}
Per-job delegate budget: {max_delegates}
Current handoff depth: {handoff_depth}

SUPPORTED LOCAL ORCHESTRATION COMMANDS

1) One bounded worker — cheapest/default:
  cat > /tmp/orch-worker-task.txt <<'EOF'
  <one independently checkable subtask>
  EOF
  orch-delegate --caller {caller} --model auto --mode read_only \
    --task-file /tmp/orch-worker-task.txt

2) One independent second opinion:
  orch-consult --caller {caller} --models auto \
    --task-file /tmp/orch-worker-task.txt

3) Parallel independent review (1-3 workers, read-only by default):
  orch-parallel --caller {caller} --count 2 --models auto \
    --task-file /tmp/orch-worker-task.txt

4) Mechanically collect a completed Phase 3 batch:
  orch-collect --batch <batch-id>

5) Synchronous MAIN handoff when another provider should continue the SAME task:
  cat > /tmp/orch-handoff-context.txt <<'EOF'
  <brief work completed, verified facts, blockers, pending next steps>
  EOF
  orch-handoff --caller {caller} --to auto --mode read_only \
    --context-file /tmp/orch-handoff-context.txt \
    --reason "<why successor is materially better or current MAIN is blocked>"

  Use --mode write ONLY when the SAME user task requires repository edits and
  the successor should take over those edits in the authoritative checkout.

CHOOSING THE TOOL
- `orch-delegate`: a single bounded implementation/research/checking subtask.
- `orch-consult`: one second opinion before a consequential or uncertain choice.
- `orch-parallel`: independent evidence where disagreement itself is useful;
  normally 2 reviewers, max 3. Do not parallelize trivial work.
- `orch-collect`: aggregate worker outputs mechanically. It intentionally does
  NOT claim semantic consensus; you must synthesize and verify important facts.
- `orch-handoff`: transfer MAIN continuation only when the current MAIN is
  blocked, a poor fit, quota-constrained, or a successor has materially better
  capability for the remaining work. It is not ordinary delegation.

PHASE 3 CONTROL-PLANE RULES
- Max delegate depth = 1. Workers may not orchestrate other workers.
- Max handoff depth = 1. A successor MAIN may not handoff again.
- Never self-call the same target.
- Obey per-job delegate budget and provider quota/health circuit breakers.
- Auto selection prefers distinct provider/quota pools and conserves expensive
  pools. Opus is reserve-only unless explicitly justified; do not use
  --include-opus casually.
- Parallel write workers are prohibited. For concurrent work, keep workers
  read-only. One isolated write delegate is allowed when appropriate.
- A write MAIN handoff is synchronous/exclusive and must not run while a write
  delegate is active.
- Read-only delegates use isolated disposable worktrees. Write delegates use
  dedicated worktrees and are never auto-merged.
- Handoff read-only uses an isolated worktree. Handoff write uses the
  authoritative checkout and records before/after Git state.
- Worker prose remains WORKER_CLAIM_UNVERIFIED unless mechanically verified.
- Exact-text agreement from `orch-collect` is NOT semantic consensus.
- Re-check important findings before adopting them as project truth.
- Fact-ledger entries may become stale after repo HEAD/tree changes.
- Do not bypass quota/health or use ignore flags unless the user explicitly asks.
- Do NOT use provider-native agent/subagent spawning as the orchestration path.
- Never delegate or handoff final human-GO decisions, credentials, private
  exchange actions, live/paper trading, destructive Git, or governance overrides.
- If `orch-handoff` succeeds in WRITE mode, do not continue modifying files in
  the old MAIN. Relay/synthesize the successor result and finish.
- IMPORTANT completion contract: ai-orch requires `ORCH_STATUS: ...`.
  Do not omit it. If the user requires an exact final marker/last line, put
  `ORCH_STATUS: COMPLETE` immediately BEFORE that marker.
"""

    if strict:
        instructions += """
STRICT CURRENT-TASK MODE:
- The current user message is the complete objective for this run.
- Do not infer, resume, or broaden into older project work.
- Use at most one delegate/consult unless the user explicitly asks for multiple
  independent reviews. Do not use parallel or handoff merely to spend capacity.
- Do not run unrelated tests, repository-wide scans, network/AWS operations, or
  extra investigations unless required by the current bounded task.
- Finish as soon as acceptance criteria are satisfied.
"""

    return instructions.strip() + "\n\nCURRENT TASK:\n" + prompt


def run_codex(prompt: str) -> Outcome:
    label = "Codex"
    prompt = _phase1_wrap_prompt(prompt, "codex")
    exe = resolve_provider_cli("codex", config)
    if not exe:
        return _missing_cli_outcome("codex")
    argv = [
        exe, "exec",
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "-",
    ]
    proc = subprocess.Popen(
        argv,
        cwd=repo,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    stderr_parts: list[str] = []
    error_messages: list[str] = []
    final_text = ""
    pending_narration = ""

    def read_err() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_parts.append(line)

    t = threading.Thread(target=read_err, daemon=True)
    t.start()

    assert proc.stdin is not None
    proc.stdin.write(prompt)
    proc.stdin.close()

    assert proc.stdout is not None
    for line in proc.stdout:
        try:
            obj = json.loads(line)
        except Exception:
            continue

        etype = obj.get("type")
        item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
        itype = item.get("type")

        if etype == "thread.started":
            log("[Codex] session started")

        elif etype == "item.completed" and itype == "agent_message":
            text = str(item.get("text", ""))
            if text:
                pending_narration = text
                final_text = text

        elif etype == "item.started":
            if pending_narration:
                _emit_visible_progress(label, pending_narration)
                pending_narration = ""

            if itype == "command_execution":
                log(f"[Codex tool] command: {_redact_progress(item.get('command', ''))}")
            elif itype == "file_change":
                log(f"[Codex tool] file change: {_redact_progress(item.get('changes', ''))}")
            elif itype:
                log(f"[Codex tool] {itype}: {_redact_progress(item)}")

        elif etype == "item.completed" and itype == "command_execution":
            exit_code = item.get("exit_code")
            output = str(item.get("aggregated_output", "")).strip()
            if exit_code not in (0, None):
                log(f"[Codex tool] command failed exit={exit_code}: {_redact_progress(output)}")
            elif output and len(output) <= 500:
                log(f"[Codex tool] output: {_redact_progress(output)}")

        elif etype == "item.completed" and itype == "file_change":
            log(f"[Codex tool] file change done: {_redact_progress(item.get('changes', ''))}")

        elif etype == "error":
            err = str(obj.get("message", obj))
            error_messages.append(err)
            log(f"[Codex] error: {_redact_progress(err)}")

    rc = proc.wait()
    t.join(timeout=1)
    stderr = "".join(stderr_parts)
    combined = stderr + "\n" + "\n".join(error_messages)

    if rc == 0 and final_text:
        return Outcome(True, final_text, "success")
    if RATE_LIMIT_RE.search(combined) or re.search(
        r"usage limit|try again at|quota|rate.?limit", combined, re.I
    ):
        return Outcome(False, "", "rate_limit", combined)
    if AUTH_RE.search(combined):
        return Outcome(False, "", "auth", combined)
    return Outcome(False, "", "agent_error", combined or f"codex exit={rc}")

def run_agy(prompt: str, model: str) -> Outcome:
    label = _model_progress_label(model, "agy")
    caller = _phase1_agy_caller(model)
    prompt = _phase1_wrap_prompt(prompt, caller)
    exe = resolve_provider_cli("agy", config)
    if not exe:
        return _missing_cli_outcome("agy")
    argv = [
        exe, "-p", prompt,
        "--model", model,
        "--output-format", "stream-json",
        "--dangerously-skip-permissions",
        "--print-timeout", "30m",
    ]
    proc = subprocess.Popen(
        argv, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )

    stderr_parts: list[str] = []
    result: dict[str, Any] | None = None
    pending_narration = ""
    last_tool_key = None

    def read_err() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_parts.append(line)

    thread = threading.Thread(target=read_err, daemon=True)
    thread.start()

    assert proc.stdout is not None
    for line in proc.stdout:
        try:
            obj = json.loads(line)
        except Exception:
            continue

        event = obj.get("event")

        if event == "init":
            mode = obj.get("init", {}).get("permission_mode")
            log(f"[{label}] started · permissions={mode or 'unknown'}")

        elif event == "step_update":
            step = obj.get("step_update", {})
            stype = step.get("step_type")
            state_name = str(step.get("state", "")).upper()

            if stype == "agent_response":
                delta = step.get("text_delta")
                if delta:
                    pending_narration += str(delta)

            elif stype == "tool":
                if state_name == "ACTIVE":
                    if pending_narration.strip():
                        _emit_visible_progress(label, pending_narration)
                        pending_narration = ""

                    tool = (
                        step.get("tool_name")
                        or step.get("tool_info", {}).get("name")
                        or "tool"
                    )
                    info = step.get("tool_info") or {}
                    key = (step.get("step_index"), tool, state_name)
                    if key != last_tool_key:
                        detail = _redact_progress(info)
                        if detail and detail not in ("{}", "None"):
                            log(f"[{label} tool] {tool}: {detail}")
                        else:
                            log(f"[{label} tool] {tool}")
                        last_tool_key = key

                elif state_name == "DONE":
                    tool = (
                        step.get("tool_name")
                        or step.get("tool_info", {}).get("name")
                        or "tool"
                    )
                    info = step.get("tool_info") or {}
                    detail = _redact_progress(info)
                    if any(x in detail.lower() for x in ("error", "failed", "exit_code")):
                        log(f"[{label} tool] {tool} done: {detail}")

        elif event == "result":
            result = obj.get("result", {})

    rc = proc.wait()
    thread.join(timeout=1)

    stderr = "".join(stderr_parts)
    status = str((result or {}).get("status", ""))
    response = str((result or {}).get("response", ""))
    combined = stderr + "\n" + json.dumps(result or {}, ensure_ascii=False)

    if rc == 0 and status == "SUCCESS" and response:
        return Outcome(True, response, "success")
    if RATE_LIMIT_RE.search(combined):
        return Outcome(False, "", "rate_limit", combined)
    if PERMISSION_RE.search(combined):
        return Outcome(False, "", "permission", combined)
    if AUTH_RE.search(combined):
        return Outcome(False, "", "auth", combined)
    return Outcome(False, "", "agent_error", combined)

def _save_claude_usage_event(obj: dict[str, Any]) -> None:
    try:
        info = obj.get("rate_limit_info") if isinstance(obj, dict) else None
        if not isinstance(info, dict):
            return
        windows = info.get("unifiedWindows") if isinstance(info.get("unifiedWindows"), dict) else {}
        out: dict[str, Any] = {
            "captured_at": time.time(),
            "source": "claude-stream-rate_limit_event",
            "status": info.get("status"),
            "rate_limit_type": info.get("rateLimitType"),
            "is_using_overage": info.get("isUsingOverage"),
            "overage_status": info.get("overageStatus"),
        }
        for src, prefix in (("five_hour", "five_hour"), ("seven_day", "seven_day")):
            row = windows.get(src) if isinstance(windows, dict) else None
            if not isinstance(row, dict):
                continue
            util = row.get("utilization")
            reset = row.get("resetsAt")
            try:
                u = max(0.0, min(1.0, float(util)))
                out[f"{prefix}_used_pct"] = u * 100.0
                out[f"{prefix}_remaining_pct"] = (1.0 - u) * 100.0
            except Exception:
                pass
            try:
                out[f"{prefix}_reset_at"] = float(reset)
            except Exception:
                pass
        save_json_atomic(CLAUDE_USAGE_CACHE, out)
    except Exception as e:
        log(f"Claude usage cache update skipped: {e}")

def run_claude(prompt: str, model: str) -> Outcome:
    label = _model_progress_label(model, "claude")
    caller = "claude-opus" if "opus" in (model or "").lower() else "claude-sonnet"
    prompt = _phase1_wrap_prompt(prompt, caller)
    issue = claude_subscription_settings_issue(repo)
    if issue:
        return Outcome(False, "", "auth", issue)
    exe = resolve_provider_cli("claude", config)
    if not exe:
        return _missing_cli_outcome("claude")
    argv = [
        exe, "-p",
        "--model", model,
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    env = claude_subscription_env()
    proc = subprocess.Popen(
        argv, cwd=repo, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    assert proc.stdin is not None
    proc.stdin.write(prompt)
    proc.stdin.close()
    stderr_parts: list[str] = []
    final = ""
    result_obj: dict[str, Any] | None = None
    pending_narration = ""

    def read_err() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_parts.append(line)

    t = threading.Thread(target=read_err, daemon=True)
    t.start()
    assert proc.stdout is not None
    for line in proc.stdout:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        etype = obj.get("type")
        if etype == "system" and obj.get("subtype") == "init":
            actual = obj.get("model") or model
            log(f"[{label}] started · model={actual}")
        elif etype == "assistant":
            message = obj.get("message", {}) if isinstance(obj.get("message"), dict) else {}
            content = message.get("content", []) if isinstance(message.get("content"), list) else []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = str(block.get("text") or "")
                    if text:
                        pending_narration = text
        elif etype == "rate_limit_event":
            _save_claude_usage_event(obj)
        elif etype == "result":
            result_obj = obj
            final = str(obj.get("result") or "")

    rc = proc.wait()
    t.join(timeout=1)
    stderr = "".join(stderr_parts)
    if pending_narration.strip() and not final:
        final = pending_narration.strip()

    subtype = str((result_obj or {}).get("subtype") or "")
    is_error = bool((result_obj or {}).get("is_error"))
    if rc == 0 and result_obj and subtype == "success" and not is_error and final:
        return Outcome(True, final, "success")

    combined = stderr + "\n" + json.dumps(result_obj or {}, ensure_ascii=False)
    if RATE_LIMIT_RE.search(combined) or re.search(r"usage limit|rate.?limit|quota|try again", combined, re.I):
        return Outcome(False, "", "rate_limit", combined)
    if AUTH_RE.search(combined) or re.search(r"not logged in|login required|oauth", combined, re.I):
        return Outcome(False, "", "auth", combined)
    if PERMISSION_RE.search(combined):
        return Outcome(False, "", "permission", combined)
    return Outcome(False, "", "agent_error", combined or f"claude exit={rc}")

def run_cmd(prompt: str, model: str, effort: str = "medium") -> Outcome:
    label = "CMD"
    prompt = _phase1_wrap_prompt(prompt, "cmd")
    exe = resolve_provider_cli("commandcode", config)
    if not exe:
        return _missing_cli_outcome("commandcode")
    argv = [
        exe, "-p", prompt,
        "--skip-onboarding",
        "--yolo",
        "--output-format", "json",
        "-m", model,
    ]
    proc = subprocess.Popen(
        argv, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )

    stderr_parts: list[str] = []
    final = ""
    result_obj: dict[str, Any] | None = None
    pending_narration = ""
    queued_inputs: dict[str, Any] = {}
    reasoning_announced = False

    def read_err() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_parts.append(line)

    t = threading.Thread(target=read_err, daemon=True)
    t.start()

    assert proc.stdout is not None
    for line in proc.stdout:
        try:
            obj = json.loads(line)
        except Exception:
            continue

        if obj.get("type") == "event":
            event = obj.get("event", {})
            etype = event.get("type")
            call_id = str(event.get("toolCallId", ""))

            if etype == "text_delta":
                delta = event.get("delta")
                if delta is None:
                    delta = event.get("textDelta")
                if delta is None:
                    delta = event.get("text")
                if delta:
                    pending_narration += str(delta)

            elif etype in ("thinking_start", "thinking_delta"):
                if not reasoning_announced:
                    log("[CMD] reasoning…")
                    reasoning_announced = True

            elif etype == "tool_queued":
                if call_id:
                    queued_inputs[call_id] = event.get("input")

            elif etype == "tool_running":
                if pending_narration.strip():
                    _emit_visible_progress(label, pending_narration)
                    pending_narration = ""

                tool = event.get("toolName") or event.get("tool") or "tool"
                detail = event.get("description")
                if not detail and call_id:
                    detail = queued_inputs.get(call_id)

                if detail is not None:
                    log(f"[CMD tool] {tool}: {_redact_progress(detail)}")
                else:
                    log(f"[CMD tool] {tool}")

            elif etype == "tool_update":
                partial = event.get("partial")
                if partial:
                    log(f"[CMD tool] update: {_redact_progress(partial)}")

            elif etype == "tool_completed":
                tool = event.get("toolName") or event.get("tool") or "tool"
                result = event.get("result")
                if result not in (None, "", []):
                    compact = _redact_progress(result)
                    if len(compact) <= 500:
                        log(f"[CMD tool] {tool} done: {compact}")

            elif etype == "tool_errored":
                tool = event.get("toolName") or event.get("tool") or "tool"
                err = event.get("error")
                log(f"[CMD tool] {tool} ERROR: {_redact_progress(err)}")

            elif etype == "subagent_start":
                log(f"[CMD subagent] started: {_redact_progress(event.get('subagentType', 'agent'))}")

            elif etype == "subagent_progress":
                tool = event.get("toolName") or "tool"
                inp = event.get("toolInput")
                log(f"[CMD subagent] {tool}: {_redact_progress(inp)}")

            elif etype == "subagent_stop":
                log(f"[CMD subagent] stopped: {_redact_progress(event.get('subagentType', 'agent'))}")

        elif obj.get("type") == "result":
            result_obj = obj
            final = str(obj.get("finalText", ""))

    rc = proc.wait()
    t.join(timeout=1)
    stderr = "".join(stderr_parts)

    if rc == 0 and result_obj and result_obj.get("subtype") == "success" and final:
        return Outcome(True, final, "success")
    if rc == 5:
        return Outcome(False, "", "rate_limit", stderr)
    if rc == 10:
        return Outcome(False, "", "credits", stderr)

    combined = stderr + "\n" + json.dumps(result_obj or {}, ensure_ascii=False)
    if RATE_LIMIT_RE.search(combined):
        return Outcome(False, "", "rate_limit", combined)
    return Outcome(False, "", "agent_error", combined)

TASK_STATUS_RE = re.compile(
    r"(?im)^[ \t]*(?:[-*][ \t]*)?(?:\*\*)?"
    r"ORCH_STATUS(?:\*\*)?[ \t]*:[ \t]*(?:\*\*)?"
    r"(COMPLETE|BLOCKED|NEEDS_USER|NEEDS_GO|FAILED|INCOMPLETE)"
    r"(?:\*\*)?[ \t]*`?[ \t]*$"
)

TERMINAL_TASK_STATUSES = {"COMPLETE", "BLOCKED", "NEEDS_USER", "NEEDS_GO"}

def parse_task_status(text: str) -> tuple[str | None, str]:
    matches = list(TASK_STATUS_RE.finditer(text or ""))
    status = matches[-1].group(1).upper() if matches else None
    cleaned = TASK_STATUS_RE.sub("", text or "").rstrip()
    return status, cleaned

def continuation_prompt(
    candidate: Candidate,
    original_task: str,
    partial_text: str,
    reason: str,
    attempt: int,
) -> str:
    return f"""You are continuing the SAME task because the prior response was not a valid
terminal task result.

Repository:
{repo}
Current MAIN:
{candidate.label}
Continuation attempt:
{attempt}

Reason continuation is required:
{reason}

The previous response was:
--- BEGIN PREVIOUS RESPONSE ---
{partial_text}
--- END PREVIOUS RESPONSE ---

Continue executing the original task now.
- Do not merely restate the plan.
- Inspect/run/verify the necessary permitted steps.
- Preserve all project safety/governance rules from AGENTS.md and the original task.
- Do not claim completion until the actual requested work is complete.
- End the FINAL response with exactly one:
  ORCH_STATUS: COMPLETE
  ORCH_STATUS: BLOCKED
  ORCH_STATUS: NEEDS_USER
  ORCH_STATUS: NEEDS_GO
  ORCH_STATUS: FAILED

ORIGINAL USER TASK:
{original_task}
""".strip()

def ensure_task_terminal(
    candidate: Candidate,
    outcome: Outcome,
    original_task: str,
) -> Outcome:
    if not outcome.ok:
        return outcome

    max_cont = int(routing_cfg.get("max_continuations", 2))
    current = outcome

    for attempt in range(0, max_cont + 1):
        status, cleaned = parse_task_status(current.text)
        current.text = cleaned
        current.task_status = status

        if status in TERMINAL_TASK_STATUSES:
            return current

        if status == "FAILED":
            current.ok = False
            current.kind = "task_failed"
            current.detail = current.detail or "agent explicitly reported task failure"
            return current

        if attempt >= max_cont:
            current.ok = False
            current.kind = "incomplete"
            current.detail = (
                f"no valid terminal task status after {max_cont} continuation(s)"
                if status is None
                else f"task remained {status} after {max_cont} continuation(s)"
            )
            return current

        why = (
            "the response omitted ORCH_STATUS and therefore cannot be accepted as complete"
            if status is None
            else f"the response reported ORCH_STATUS: {status}"
        )
        log(
            f"continuation required for {candidate.label}: "
            f"{why} (attempt {attempt + 1}/{max_cont})"
        )

        next_prompt = continuation_prompt(
            candidate,
            original_task,
            current.text,
            why,
            attempt + 1,
        )
        current = invoke_candidate(candidate, next_prompt)
        if not current.ok:
            return current

    return current

def invoke_candidate(c: Candidate, prompt: str) -> Outcome:
    log(f"trying MAIN: {c.label}" + (f" ({c.model})" if c.model else ""))
    if c.backend == "codex":
        return run_codex(prompt)
    if c.backend == "agy":
        assert c.model
        return run_agy(prompt, c.model)
    if c.backend == "claude":
        assert c.model
        return run_claude(prompt, c.model)
    assert c.model
    effort = "low" if assessment.need < .35 else ("medium" if assessment.need < .72 else "high")
    return run_cmd(prompt, c.model, effort)

def handle_failure(c: Candidate, outcome: Outcome) -> bool:
    """Return True if remaining candidates of the same AGY backend should be skipped."""
    detail = outcome.detail
    if outcome.kind == "missing_binary":
        # Missing/broken local executables are environment problems, not provider
        # service failures. Do not poison router health with a long cooldown.
        log(f"{c.backend} unavailable: CLI executable unavailable (no cooldown; run /doctor)")
        return c.backend == "agy"

    if c.backend == "codex":
        if outcome.kind == "rate_limit":
            block_with_backoff("codex", "rate/quota limit", 900)
        elif outcome.kind == "auth":
            block_with_backoff("codex", "authentication failure", 900)
        else:
            block_with_backoff("codex", "agent failure", 300)
        return False

    if c.backend == "commandcode":
        if outcome.kind == "rate_limit":
            block_with_backoff("commandcode", "rate limit", 900)
        elif outcome.kind == "credits":
            block_with_backoff("commandcode", "insufficient credits", 1800)
        else:
            block_with_backoff("commandcode", "agent failure", 300)
        return False

    if c.backend == "claude":
        if outcome.kind == "rate_limit":
            block_with_backoff("claude:claude-pro", "Claude Pro usage/rate limit", 900)
        elif outcome.kind == "auth":
            block_with_backoff("claude", "Claude Code subscription authentication/configuration", 600)
        elif outcome.kind == "permission":
            block_with_backoff("claude", "Claude Code headless permission configuration", 300)
        else:
            block_with_backoff("claude:claude-pro", "Claude Code provider failure", 300)
        return False

    # AGY
    if outcome.kind == "permission":
        block_with_backoff("agy", "headless permission configuration", 300)
        return True
    if outcome.kind == "auth":
        block_with_backoff("agy", "authentication failure", 900)
        return True
    if outcome.kind == "rate_limit":
        key = f"agy:{c.pool}"
        if c.reset_in and c.reset_in > 0:
            block_until(key, time.time() + c.reset_in, "quota/rate limit")
        else:
            block_with_backoff(key, "quota/rate limit", 900)
    else:
        block_with_backoff(f"agy:{c.pool}", "model-specific failure", 300)
    return False

def uncertainty_signal(text: str) -> bool:
    return bool(re.search(
        r"\bUNKNOWN\b|\bBLOCKED\b|cannot verify|unable to verify|"
        r"unresolved|근거 부족|확인 불가|판단 불가|원인 미확인",
        text,
        re.I,
    ))

def stronger_review_candidate(current: Candidate, all_candidates: list[Candidate]) -> Candidate | None:
    # Review escalation is only useful as independent verification when the
    # reviewer comes from a different model vendor. This still allows Gemini
    # <-> Claude review even though both are invoked through the AGY backend.
    viable = [
        c for c in all_candidates
        if (
            c.capability > current.capability + .08
            and independent_reviewer(current.vendor, c.vendor)
            and not candidate_blocked(c)
        )
    ]
    viable.sort(key=lambda c: (c.utility, c.capability), reverse=True)
    return viable[0] if viable else None

def record_quality_if_applicable(c: Candidate, outcome: Outcome, *, phase: str) -> None:
    if not quality_enabled:
        return
    quality_bearing = outcome.ok or outcome.kind in {"incomplete", "task_failed"}
    if not quality_bearing:
        return
    record_router_outcome(
        ROUTER_LEARNING,
        candidate_key=c.key,
        tags=quality_tags,
        success=bool(outcome.ok and outcome.task_status in TERMINAL_TASK_STATUSES),
        phase=phase,
        terminal_status=outcome.task_status,
        failure_kind=None if outcome.ok else outcome.kind,
    )


successful_candidate: Candidate | None = None
response = ""
task_status: str | None = None
skip_agy = False

for c in candidates:
    if candidate_blocked(c):
        if c.backend == "claude":
            ready, reason = claude_subscription_ready()
            if not ready and reason:
                log(f"skip {c.label}: {reason}")
            elif c.pool:
                log_blocked(c.label, f"claude:{c.pool}")
            else:
                log_blocked(c.label, "claude")
        else:
            log_blocked(c.label, "agy" if c.backend == "agy" and blocked("agy") else (
                f"agy:{c.pool}" if c.backend == "agy" and c.pool and blocked(f"agy:{c.pool}") else c.backend
            ))
        continue
    if skip_agy and c.backend == "agy":
        continue

    outcome = invoke_candidate(c, orchestrator_prompt(c.label, task))
    if outcome.ok:
        outcome = ensure_task_terminal(c, outcome, task)

    record_quality_if_applicable(c, outcome, phase="main")

    if outcome.ok:
        successful_candidate = c
        response = outcome.text
        task_status = outcome.task_status
        record_success(c.backend, c.model, c.pool)
        log(f"selected MAIN: {c.label}" + (f" ({c.model})" if c.model else ""))
        log(f"TASK_STATUS: {task_status}")
        break

    log(f"{c.label} failed: {outcome.kind}")
    if outcome.kind not in ("incomplete", "task_failed"):
        if handle_failure(c, outcome):
            skip_agy = True

if not successful_candidate:
    print("ALL_ORCHESTRATORS_UNAVAILABLE", file=sys.stderr)
    raise SystemExit(1)

# Phase-boundary adaptive escalation: at most one stronger verification pass.
max_escalations = int(routing_cfg.get("max_escalations", 1))
should_review = (
    max_escalations > 0
    and task_status == "COMPLETE"
    and (
        (
            assessment.crosscheck >= .78
            and successful_candidate.capability < .86
        )
        or (
            assessment.uncertainty >= .72
            and successful_candidate.capability < .82
        )
        or (
            uncertainty_signal(response)
            and successful_candidate.capability < .88
        )
    )
)

if should_review:
    reviewer = stronger_review_candidate(successful_candidate, candidates)
    if reviewer:
        log(f"adaptive escalation: {successful_candidate.key} -> {reviewer.key}")
        review = invoke_candidate(
            reviewer,
            orchestrator_prompt(reviewer.label, task, prior=response),
        )
        if review.ok:
            review = ensure_task_terminal(reviewer, review, task)

        record_quality_if_applicable(reviewer, review, phase="review")

        if review.ok and review.task_status in TERMINAL_TASK_STATUSES:
            successful_candidate = reviewer
            response = review.text
            task_status = review.task_status
            record_success(reviewer.backend, reviewer.model, reviewer.pool)
            log(f"escalation accepted: {reviewer.label}")
            log(f"TASK_STATUS: {task_status}")
        else:
            log(
                f"escalation failed/incomplete ({review.kind}); "
                "keeping prior successful result"
            )
            if review.kind not in ("incomplete", "task_failed"):
                handle_failure(reviewer, review)

if task_status is None:
    task_status = "FAILED"

log(f"FINAL_TASK_STATUS: {task_status}")
sys.stdout.write(response)

#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import CommandPalette
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import Collapsible, Footer, RichLog, Static, TextArea

from desktop_notify import backend_status as desktop_notify_backend_status, send_notification
from platform_support import copy_text

from orch_runtime import (
    classify_main_worker,
    discover_legacy_main_worker,
    load_json as runtime_load_json,
    process_pgid,
    write_main_lease,
)

HOME = Path.home()
BASE = HOME / ".local/share/orchbridge"
WORKSPACE_REGISTRY = BASE / "global/workspaces.json"


def _canonical_repo(path: Path) -> Path:
    path = path.expanduser().resolve()
    probe = path.parent if path.is_file() else path
    try:
        p = subprocess.run(
            ["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if p.returncode == 0 and p.stdout.strip():
            return Path(p.stdout.strip()).expanduser().resolve()
    except Exception:
        pass
    return path


def _resolve_startup_context() -> tuple[Path, Path, str | None, str | None, str]:
    repo = _canonical_repo(Path(os.getenv("AI_ORCH_REPO", str(Path.cwd()))))
    explicit_base = os.getenv("AI_ORCH_PROJECT_BASE")
    explicit_wid = os.getenv("AI_ORCH_WORKSPACE_ID") or None
    explicit_name = os.getenv("AI_ORCH_PROJECT_NAME") or None
    if explicit_base:
        return (
            repo,
            Path(explicit_base).expanduser().resolve(),
            explicit_wid,
            explicit_name,
            "env",
        )

    # A manually restarted `ai-chat-tui` inside a registered repo should recover
    # its project-local state instead of silently falling back to global legacy
    # state. Exact canonical repo equality is required; no fuzzy/path-prefix match.
    try:
        data = json.loads(WORKSPACE_REGISTRY.read_text())
        rows = data.get("workspaces", {}) if isinstance(data, dict) else {}
    except Exception:
        rows = {}
    for wid, item in rows.items() if isinstance(rows, dict) else []:
        if not isinstance(item, dict) or not item.get("repo") or not item.get("project_base"):
            continue
        try:
            registered_repo = _canonical_repo(Path(str(item["repo"])))
        except Exception:
            continue
        if registered_repo != repo:
            continue
        project_base = Path(str(item["project_base"])).expanduser().resolve()
        name = str(item.get("name") or repo.name)
        os.environ["AI_ORCH_REPO"] = str(repo)
        os.environ["AI_ORCH_WORKSPACE_ID"] = str(wid)
        os.environ["AI_ORCH_PROJECT_BASE"] = str(project_base)
        os.environ["AI_ORCH_PROJECT_NAME"] = name
        return repo, project_base, str(wid), name, "registry-auto"

    return repo, BASE.resolve(), None, None, "legacy-global"


REPO_DEFAULT, PROJECT_BASE, WORKSPACE_ID, PROJECT_NAME, WORKSPACE_SOURCE = _resolve_startup_context()
APP_DIR = BASE / "app"
JOBS_DIR = PROJECT_BASE / "jobs"
TUI_STATE_FILE = PROJECT_BASE / "tui-state.json"
QUEUE_FILE = PROJECT_BASE / "task-queue.json"
ROUTER_STATE_FILE = HOME / ".cache/orchbridge/router-state.json"
DELEGATION_EVENTS_FILE = PROJECT_BASE / "delegations/events.jsonl"
DELEGATION_REGISTRY_DB = PROJECT_BASE / "delegations/registry.sqlite3"
WORKER = APP_DIR / "ai_job_worker.py"
UPDATE_MANAGER = APP_DIR / "ai_update_manager.py"
VERSION_FILE = BASE / "VERSION.json"
JOBS_DIR.mkdir(parents=True, exist_ok=True)
(PROJECT_BASE / "delegations").mkdir(parents=True, exist_ok=True)


def _resolved_path(value: object) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return Path(text).expanduser().resolve()
    except Exception:
        return None


def job_belongs_to_context(
    job: dict[str, Any],
    *,
    current_repo: Path,
    workspace_id: str | None,
    project_base: Path,
    global_base: Path = BASE,
) -> bool:
    job_repo = _resolved_path(job.get("repo"))
    if job_repo is None or job_repo != _canonical_repo(current_repo):
        return False

    current_base = project_base.expanduser().resolve()
    global_base = global_base.expanduser().resolve()
    job_base = _resolved_path(job.get("project_base"))
    job_wid = str(job.get("workspace_id") or "").strip() or None

    if current_base != global_base:
        # Project-local TUI: require both the exact project base and workspace id.
        # This deliberately refuses legacy/global jobs even when their repo matches.
        if workspace_id is None or job_wid != workspace_id:
            return False
        return job_base == current_base

    # Legacy/global TUI: only same-repo jobs with no workspace identity belong
    # here. A project-scoped job must never leak back into the global frontend.
    if job_wid is not None:
        return False
    return job_base in {None, global_base}


def iter_known_job_dirs() -> list[Path]:
    rows: list[Path] = []
    seen: set[str] = set()
    roots = [BASE / "jobs"]
    projects = BASE / "projects"
    if projects.is_dir():
        roots.extend(p / "jobs" for p in projects.iterdir() if p.is_dir())
    for jobs_root in roots:
        if not jobs_root.is_dir():
            continue
        for d in jobs_root.glob("job-*"):
            try:
                key = str(d.resolve())
            except Exception:
                key = str(d)
            if key not in seen:
                seen.add(key)
                rows.append(d)
    return rows

ATTEMPT_RE = re.compile(r"^\[ai-orch\] trying MAIN:\s*(.+?)\s*$")
TASK_STATUS_RE = re.compile(r"^\[ai-orch\] (?:FINAL_)?TASK_STATUS:\s*(\w+)")
ROUTE_RE = re.compile(r"^\[ai-orch\] route:\s*(.+)$")
QUOTA_RE = re.compile(r"^\[ai-orch\] quota:\s*(.+)$")
MODEL_TOOL_RE = re.compile(r"^\[ai-orch\] \[([^\]]+?) tool\]\s*(.+)$")
MODEL_PROGRESS_RE = re.compile(r"^\[ai-orch\] \[([^\]]+)\]\s*(.+)$")
SUBAGENT_RE = re.compile(r"^\[ai-orch\] \[([^\]]+?) subagent\]\s*(.+)$")
RATE_HINT_RE = re.compile(
    r"usage limit|rate.?limit|quota/rate limit|blocked until reset|try again at",
    re.I,
)
NETWORK_HINT_RE = re.compile(
    r"network issue|connection reset|temporar(?:y|ily) unavailable|timed out|timeout",
    re.I,
)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def orchestrator_version() -> str:
    source_version = Path(__file__).resolve().parents[1] / "VERSION.json"
    for version_file in (source_version, VERSION_FILE):
        try:
            data = json.loads(version_file.read_text())
            version = str(data.get("release") or "").strip()
            if version:
                return version
        except Exception:
            continue
    return "unknown"


TUI_COMPONENT_VERSION = orchestrator_version()


SLASH_COMMANDS = [
    ("/help", "단축키와 전체 명령어 보기"),
    ("/status", "오케스트레이터 / 작업 / 저장소 상태 보기"),
    ("/quota", "모든 모델 제공자 쿼터 보기"),
    ("/history", "최근 영구 작업 기록 보기"),
    ("/session", "현재 TUI 작업 / 세션 보기"),
    ("/current", "현재 작업의 정확한 프롬프트 / 지문 보기"),
    ("/project", "현재 프로젝트 워크스페이스 보기"),
    ("/project list", "프로젝트 창 목록 보기"),
    ("/project open my-project", "프로젝트 창으로 전환 / 열기"),
    ("/project new my-app", "프로젝트 루트 아래 새 프로젝트 만들기"),
    ("/project root", "새 프로젝트 생성 루트 보기 / 변경"),
    ("/project alias reviewai", "tmux 탭의 짧은 별칭 설정"),
    ("/project close", "현재 프로젝트 창 닫기"),
    ("/project delete <name>", "등록된 프로젝트를 목록에서 제거 (repo/state 보존)"),
    ("/restart", "현재 프로젝트 TUI를 같은 창에서 즉시 재시작"),
    ("/reload", "프로젝트/TUI 저장 설정을 다시 읽기"),
    ("/version", "현재 로드된 TUI와 설치된 버전 비교"),
    ("/settings", "연결된 AI 제공자 / 모델 설정 보기"),
    ("/settings init", "기본 설정 파일 생성 / 감지"),
    ("/notify", "작업 완료 데스크탑 알림 설정 보기"),
    ("/notify smart", "단일 작업/대기열 최종 완료와 확인 필요 상태만 알림 (기본값)"),
    ("/notify all", "모든 MAIN 작업의 종료 상태 알림"),
    ("/notify important", "BLOCKED/NEEDS_USER 같은 확인 필요 상태만 알림"),
    ("/notify off", "데스크탑 알림 끄기"),
    ("/notify test", "데스크탑 알림 테스트 (macOS/Linux)"),
    ("/doctor", "모델 CLI / PATH / npm 상태 진단"),
    ("/steer <지시>", "현재 작업의 방향을 수정하고 같은 job으로 이어서 실행"),
    ("/new", "새 TUI 세션 시작"),
    ("/queue", "현재 프로젝트의 다음 작업 대기열 보기"),
    ("/queue add <prompt>", "다음 작업을 대기열 끝에 추가"),
    ("/queue remove <번호>", "대기 중 작업 하나 삭제"),
    ("/queue clear", "실행 중 작업은 유지하고 대기열 비우기"),
    ("/queue pause", "현재 작업은 유지하고 다음 작업 자동 시작 멈추기"),
    ("/queue resume", "대기열 자동 실행 재개; 중단 게이트는 명시적으로 건너뛰기"),
    ("/branch", "현재 브랜치 / HEAD / dirty 상태 보기"),
    ("/branch list", "로컬 브랜치 목록 보기"),
    ("/branch switch <name>", "기존 브랜치로 안전하게 전환"),
    ("/branch new <name>", "현재 HEAD에서 새 브랜치를 만들고 전환"),
    ("/branch next <name>", "다음 프롬프트/대기 작업을 기존 브랜치에서 실행"),
    ("/branch next-new <name>", "다음 프롬프트/대기 작업용 새 브랜치 예약"),
    ("/branch next-clear", "예약한 다음 작업 브랜치 계획 취소"),
    ("/permissions", "에이전트 로컬 권한 프로필 보기"),
    ("/permissions trusted", "로컬 개발 작업을 넓게 허용 (기본값)"),
    ("/permissions guarded", "보수적인 로컬 권한 프로필 사용"),
    ("/scope contextual", "기존 맥락을 이어가는 일반 작업 모드"),
    ("/scope strict", "현재 메시지만 처리하는 제한 작업 모드"),

    ("/router on", "적응형 라우터 켜기"),
    ("/router off", "적응형 라우터 끄기"),
    ("/router reset", "라우터 쿨다운 / 실패 상태 초기화"),

    ("/model auto", "작업에 맞게 모델 자동 선택"),
    ("/model cmd", "MiMo V2.5 Pro로 모델 고정"),
    ("/model codex", "GPT-6 Luna로 모델 고정"),
    ("/model sonnet", "AGY Claude Sonnet 4.6 Thinking으로 모델 고정"),
    ("/model opus", "AGY Claude Opus 4.6 Thinking으로 모델 고정"),
    ("/model claude", "새 Claude Pro 계정의 Claude Code Sonnet으로 모델 고정"),
    ("/model claude-opus", "새 Claude Pro 계정의 Claude Code Opus로 모델 고정"),
    ("/model gemini-low", "Gemini 3.8 Flash low로 모델 고정"),
    ("/model gemini-medium", "Gemini 3.8 Flash medium으로 모델 고정"),
    ("/model gemini-high", "Gemini 3.8 Flash high로 모델 고정"),

    ("/decision", "최근 라우터 판단 보기"),
    ("/agents", "모델 시도 기록 / 현재 에이전트 보기"),
    ("/workers", "현재 worker / 작업 상태 보기"),
    ("/delegates", "다른 모델 제공자의 delegate worker 보기"),
    ("/collab", "Phase 3 consult / parallel / handoff 활동 보기"),
    ("/attach", "파일 / 이미지 첨부; 인자 없으면 Finder 열기"),
    ("/skills", "필요할 때 불러오는 스킬 목록 / 현재 선택 보기"),
    ("/tools", "도구 사용 권한 정책 보기 / 설정"),
    ("/mcp", "현재 작업 범위의 MCP 등록 정보 보기"),
    ("/collect", "최근 / 현재 Phase 3 결과 모음 보기"),
    ("/facts", "작업 + 저장소 fact ledger 항목 보기"),
    ("/draft", "검증 근거로 commit / PR 초안 보기"),
    ("/health", "delegate 모델 제공자 상태 / 쿨다운 보기"),
    ("/recover", "오래된 delegate lease 복구"),
    ("/update", "모델 제공자 CLI 업데이트 관리"),
    ("/diag", "프로세스 / 스트림 멈춤 진단 보기"),
    ("/skills", "프로젝트 스킬 보기"),
    ("/mcp", "감지된 MCP 설정 보기"),
    ("/copy", "마지막 Assistant 답변을 클립보드에 복사"),
    ("/copy user", "현재 / 마지막 사용자 프롬프트 복사"),
    ("/copy all", "User / Assistant 대화 내용만 복사"),
    ("/copy worked", "마지막 답변 + WORKED 요약 복사"),
    ("/copy prompt", "현재 작업의 정확한 프롬프트 복사"),
    ("/compact", "재개용 압축 체크포인트 작성"),

    ("/jobs", "영구 작업 목록 보기"),
    ("/job", "현재 작업 메타데이터 보기"),
    ("/pause", "현재 작업 일시정지"),
    ("/resume", "일시정지된 작업 재개"),
    ("/retry", "현재 일시정지 작업 재시도 / 재개"),
    ("/cancel", "현재 작업 취소"),

    ("/verbose normal", "간단한 진행 상황만 표시"),
    ("/verbose verbose", "요약된 도구 이벤트까지 표시"),
    ("/verbose trace", "원시 이벤트 스트림 표시"),
    ("/details", "상세 표시 모드 순환 변경"),
    ("/quit", "TUI 종료"),
]



def now() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or now()).astimezone().isoformat(timespec="seconds")


# Terminal/tmux transport safety. Raw SGR mouse or ANSI reports must never become
# task text (for example ESC[<43;33;54M or its printable ^[[<... form).
_ACTUAL_OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)", re.DOTALL)
_ACTUAL_DCS_RE = re.compile(r"\x1b[P^_].*?\x1b\\", re.DOTALL)
_ACTUAL_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_LITERAL_CSI_RE = re.compile(r"\^\[\[[0-?]*[ -/]*[@-~]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SUBMIT_DEDUPE_SECONDS = 12.0


def sanitize_terminal_input(text: str) -> tuple[str, bool]:
    original = str(text or "")
    cleaned = original.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _ACTUAL_OSC_RE.sub("", cleaned)
    cleaned = _ACTUAL_DCS_RE.sub("", cleaned)
    cleaned = _ACTUAL_CSI_RE.sub("", cleaned)
    cleaned = _LITERAL_CSI_RE.sub("", cleaned)
    cleaned = _CONTROL_RE.sub("", cleaned)
    return cleaned, cleaned != original


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


def append_jsonl(path: Path, data: dict[str, Any]) -> None:
    with path.open("a") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def clean(line: str) -> str:
    return ANSI_RE.sub("", line).rstrip("\r\n")


def _validated_task_status(status: str | None, rc: int) -> str | None:
    """Never accept COMPLETE from a failed ai-orch process."""
    normalized = str(status or "").strip().upper() or None
    if rc != 0 and normalized == "COMPLETE":
        return None
    return normalized


def _pretty_slug(slug: str) -> str:
    x = slug.strip()
    lo = x.lower()
    if "gpt-6-luna" in lo:
        return "GPT-6 Luna"
    if "gemini-3.8-flash-high" in lo:
        return "Gemini 3.8 Flash · high"
    if "gemini-3.8-flash-medium" in lo:
        return "Gemini 3.8 Flash · medium"
    if "gemini-3.8-flash-low" in lo:
        return "Gemini 3.8 Flash · low"
    if "claude-sonnet-4-6" in lo or "sonnet 4.6" in lo:
        return "Claude Sonnet 4.6 Thinking" if "thinking" in lo else "Claude Sonnet 4.6"
    if "claude-opus-4-6" in lo or "opus 4.6" in lo:
        return "Claude Opus 4.6 Thinking" if "thinking" in lo else "Claude Opus 4.6"
    if "mimo-v2.5-pro" in lo:
        return "MiMo V2.5 Pro"
    if "mimo-v2.5" in lo:
        return "MiMo V2.5"
    x = x.split("/")[-1].replace("_", " ").replace("-", " ")
    return " ".join(
        part.upper() if part.lower() == "gpt" else part.title()
        for part in x.split()
    )


def _codex_display_name() -> str:
    model = None
    effort = None
    try:
        import tomllib
        cfg = tomllib.loads((HOME / ".codex/config.toml").read_text())
        model = cfg.get("model")
        effort = cfg.get("model_reasoning_effort")
    except Exception:
        pass
    pretty = _pretty_slug(str(model)) if model else "GPT-6 Luna"
    return f"{pretty} · {effort}" if effort else pretty


def pretty_model_name(raw: str) -> str:
    lo = raw.lower()
    if "claude code/opus" in lo or "claude-pro/opus" in lo:
        return "Claude Code Opus · Pro"
    if "claude code/sonnet" in lo or "claude-pro/sonnet" in lo:
        return "Claude Code Sonnet · Pro"
    if lo.strip() == "codex" or lo.startswith("codex "):
        return _codex_display_name()
    m = re.search(r"\(([^()]+)\)\s*$", raw)
    slug = m.group(1) if m else raw
    if "gemini" in lo:
        return _pretty_slug(slug)
    if "sonnet" in lo:
        return _pretty_slug(slug)
    if "opus" in lo:
        return _pretty_slug(slug)
    if "mimo" in lo or "command code" in lo:
        return _pretty_slug(slug)
    return _pretty_slug(slug)


def pretty_route(route: str) -> str:
    mapping = {
        "codex": _codex_display_name(),
        "claude-sonnet": "Claude Code Sonnet · Pro",
        "claude-opus": "Claude Code Opus · Pro",
        "sonnet": "AGY Claude Sonnet 4.6 Thinking",
        "opus": "AGY Claude Opus 4.6 Thinking",
        "gemini-high": "Gemini 3.8 Flash · high",
        "gemini-medium": "Gemini 3.8 Flash · medium",
        "gemini-low": "Gemini 3.8 Flash · low",
        "cmd": "MiMo V2.5 Pro",
    }
    out = route
    for key, label in mapping.items():
        out = re.sub(rf"\b{re.escape(key)}(?=\()", label, out)
    return out


def parse_reset(text: str) -> float | None:
    m = re.search(
        r"try again at\s+([A-Z][a-z]{2})\s+(\d{1,2})(?:st|nd|rd|th)?,\s+"
        r"(\d{4})\s+(\d{1,2}):(\d{2})\s+(AM|PM)",
        text,
        re.I,
    )
    if m:
        mon, day, year, hour, minute, ap = m.groups()
        try:
            return datetime.strptime(
                f"{mon} {day} {year} {hour}:{minute} {ap.upper()}",
                "%b %d %Y %I:%M %p",
            ).astimezone().timestamp()
        except Exception:
            pass

    m = re.search(
        r"blocked until reset\s*\((?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?",
        text,
        re.I,
    )
    if m and any(m.groups()):
        d, h, minute = (int(x or 0) for x in m.groups())
        return now() + d * 86400 + h * 3600 + minute * 60

    m = re.search(
        r"resets?\s+in\s+(?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m)?",
        text,
        re.I,
    )
    if m and any(m.groups()):
        d, h, minute = (int(x or 0) for x in m.groups())
        return now() + d * 86400 + h * 3600 + minute * 60
    return None


def countdown(target: float | None) -> str:
    if not target:
        return ""
    sec = max(0, int(target - now()))
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h or d:
        parts.append(f"{h}h")
    parts.append(f"{m:02d}m")
    parts.append(f"{s:02d}s")
    return " ".join(parts)


def git_branch(repo: Path) -> str:
    try:
        p = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return p.stdout.strip() or "detached"
    except Exception:
        return "-"


def git_head(repo: Path) -> str:
    try:
        p = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo,
            capture_output=True, text=True, timeout=3,
        )
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def git_dirty_count(repo: Path) -> int:
    try:
        p = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=repo,
            capture_output=True, text=True, timeout=5,
        )
        if p.returncode != 0:
            return -1
        return len([x for x in p.stdout.splitlines() if x.strip()])
    except Exception:
        return -1


def git_ref_oid(repo: Path, ref: str) -> str:
    try:
        p = subprocess.run(
            ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=repo,
            capture_output=True, text=True, timeout=5,
        )
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def git_valid_branch_name(repo: Path, name: str) -> bool:
    try:
        p = subprocess.run(
            ["git", "check-ref-format", "--branch", name], cwd=repo,
            capture_output=True, text=True, timeout=3,
        )
        return p.returncode == 0
    except Exception:
        return False


def git_local_branch_exists(repo: Path, name: str) -> bool:
    try:
        return subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"], cwd=repo,
            timeout=3,
        ).returncode == 0
    except Exception:
        return False


def git_origin_branch_exists(repo: Path, name: str) -> bool:
    try:
        return subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{name}"], cwd=repo,
            timeout=3,
        ).returncode == 0
    except Exception:
        return False


def cached_cmd_quota() -> str:
    p = HOME / ".cache/orchbridge/cmd-usage.json"
    try:
        d = json.loads(p.read_text())
    except Exception:
        return "CMD —"

    monthly_remaining = d.get("monthly_remaining")
    monthly_pct = d.get("monthly_remaining_pct")
    weekly = d.get("weekly_remaining_pct")
    five = d.get("five_hour_remaining_pct")

    if monthly_remaining is not None:
        if monthly_pct is not None:
            return f"CMD month {monthly_pct:.0f}% · ${float(monthly_remaining):.2f}"
        return f"CMD month ${float(monthly_remaining):.2f}"
    if monthly_pct is not None:
        return f"CMD month {float(monthly_pct):.0f}%"
    if weekly is not None:
        return f"CMD wk {float(weekly):.0f}%"
    if five is not None:
        return f"CMD 5h {float(five):.0f}%"
    return "CMD —"


@dataclass
class Attempt:
    model: str
    started: float = field(default_factory=now)
    ended: float | None = None
    lines: list[tuple[str, str]] = field(default_factory=list)
    panel: Collapsible | None = None
    log: RichLog | None = None
    state: str = "RUNNING"
    last_event_at: float = field(default_factory=now)
    quota_summary: str | None = None

    def elapsed(self) -> str:
        sec = int((self.ended or now()) - self.started)
        m, s = divmod(sec, 60)
        if m >= 60:
            h, m = divmod(m, 60)
            return f"{h}h {m:02d}m"
        return f"{m}m {s:02d}s"


class Line(Message):
    def __init__(self, text: str):
        self.text = text
        super().__init__()


class Done(Message):
    def __init__(self, rc: int, result: Path):
        self.rc, self.result = rc, result
        super().__init__()


class QuotaUpdate(Message):
    def __init__(self, attempt: Attempt, data: dict[str, Any]):
        self.attempt = attempt
        self.data = data
        super().__init__()


class UpdateResult(Message):
    def __init__(self, title: str, rc: int, text: str):
        self.title = title
        self.rc = rc
        self.text = text
        super().__init__()


class TimelineMessage(Static):
    """Non-scrollable, auto-height transcript card.

    USER / ASSISTANT / status cards must grow with their content so the outer
    #feed VerticalScroll remains the only transcript scroll owner.  Keep
    RichLog for append-heavy THINKING / WORKED detail panes instead.
    """

    def __init__(self, role: str, text: str, *, css_class: str) -> None:
        body = role if not text else f"{role}\n\n{text}"
        super().__init__(
            body,
            markup=False,
            classes=f"timeline-message {css_class}",
        )


class OrchBridgeApp(App):
    TITLE = f"OrchBridge v{orchestrator_version()}"
    ENABLE_COMMAND_PALETTE = True

    CSS = """
    Screen {
        background: #0b0d10;
        color: #d8dee9;
        layout: vertical;
    }

    #brand {
        height: 3;
        padding: 0 2;
        background: #11151b;
        color: #e5e9f0;
        border-bottom: solid #2b3440;
    }

    #runtime {
        height: 2;
        padding: 0 2;
        background: #0f1318;
        color: #8f9aaa;
        border-bottom: solid #202832;
    }

    #feed {
        height: 1fr;
        padding: 1 2 0 2;
        scrollbar-size: 1 1;
    }

    Collapsible {
        margin: 0 0 1 0;
        padding: 0;
        background: #0f1318;
        border-left: thick #334155;
    }

    .timeline-message {
        height: auto;
        min-height: 2;
        margin: 0 0 1 0;
        padding: 1 2;
        background: #10141a;
        color: #d8dee9;
        border-left: thick #334155;
    }

    .user-message {
        background: #15191f;
        border-left: thick #64748b;
    }

    .assistant-message {
        background: #0f1318;
        border-left: thick #7aa2f7;
    }

    .status-message {
        background: #0d1117;
        border-left: thick #475569;
        color: #aebbc9;
    }

    .system-message {
        background: #0d1117;
        border-left: thick #263241;
        color: #94a3b8;
    }

    .prompt-full {
        height: auto;
        min-height: 1;
        padding: 0 1 1 2;
        background: #0f1318;
        color: #cbd5e1;
    }

    Collapsible.-collapsed {
        height: auto;
    }

    .attempt-log {
        height: auto;
        min-height: 1;
        padding: 0 1 1 2;
        background: #0f1318;
        color: #cbd5e1;
    }

    #agentdock {
        height: auto;
        min-height: 2;
        max-height: 8;
        padding: 0 2;
        background: #11151b;
        color: #9fb3c8;
        border-top: solid #202832;
    }

    #commandbar {
        display: none;
        height: auto;
        max-height: 4;
        padding: 0 2;
        background: #0f1318;
        color: #7aa2f7;
        border-top: solid #202832;
    }

    #composer-label {
        height: 1;
        padding: 0 2;
        background: #0b0d10;
        color: #6f7f90;
    }

    #prompt {
        height: 6;
        padding: 0 1;
        background: #11151b;
        color: #edf2f7;
        border: round #334155;
    }

    #prompt:focus {
        border: round #7aa2f7;
    }

    Footer {
        background: #11151b;
        color: #8f9aaa;
    }
    """

    BINDINGS = [
        Binding("enter", "send", "Send", priority=True),
        Binding("shift+enter", "newline", show=False, priority=True),
        Binding("alt+enter", "newline", show=False, priority=True),
        Binding("ctrl+enter", "send", show=False, priority=True),
        Binding("tab", "complete_command", show=False, priority=True),
        Binding("f2", "details", "Detail"),
        Binding("ctrl+o", "attach", "Attach"),
        Binding("ctrl+t", "toggle_last", "Last work"),
        Binding("ctrl+u", "clear_prompt", "Clear input", show=False, priority=True),
        Binding("ctrl+y", "copy_last", "Copy", priority=True),
        Binding("ctrl+r", "resume", "Retry/Resume"),
        Binding("ctrl+x", "cancel", "Cancel"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.repo = Path(os.getenv("AI_ORCH_REPO", str(REPO_DEFAULT))).resolve()
        self.job_dir: Path | None = None
        self.job: dict[str, Any] | None = None
        self.proc: subprocess.Popen[str] | None = None
        self.attempts: list[Attempt] = []
        self.current: Attempt | None = None
        self.verbosity = "normal"
        self.route = ""
        self.quota = ""
        self.task_status: str | None = None
        self.raw: list[str] = []
        self._copy_messages: list[tuple[str, str]] = []
        self.pending_attachments: list[str] = []
        self._last_quota_refresh = 0.0
        self._cmd_quota_summary = "CMD —"

        # Phase 1 cross-provider delegation UI state.
        self._delegate_event_offset = 0
        self.delegate_states: dict[str, dict[str, Any]] = {}

        # Main worker liveness / quiet-stream diagnostics.
        self._last_main_output_at = now()
        self._last_proc_diag_at = 0.0
        self._proc_diag = "idle"
        self._stall_notice_level = 0

        # Startup recovery gets a grace window so an old persistent job cannot
        # silently steal a freshly typed task.
        self._recovery_grace_until = 0.0
        self._recovered_main_result_posted = False
        self._last_main_recovery_poll = 0.0

        # Frontend routing state mirrors the legacy CLI commands.
        self.router_enabled = True
        self.model_override = "auto"
        self.scope_mode = "contextual"
        self._load_tui_state()
        self._notification_seen: set[str] = set()
        self._notify_last_result: dict[str, Any] | None = None
        self._notify_error_noted = False

        # Project-scoped persistent next-task queue. Queue writes are disabled
        # in legacy-global panes so one repository cannot consume another's work.
        self._queue_notice_key = ""
        self.queue_state = self._load_queue_state()

        # One terminal/key event must not become both a running job and an
        # identical queued follow-up.
        self._last_submit_fingerprint = ""
        self._last_submit_at = 0.0

    def _format_age(self, seconds: float) -> str:
        seconds = max(0, int(seconds))
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}h {m:02d}m"
        if m:
            return f"{m}m {s:02d}s"
        return f"{s}s"

    def _model_target_from_display(self, model: str) -> str | None:
        lo = (model or "").lower()
        if "gpt-6 luna" in lo or lo.strip() == "codex":
            return "codex"
        if "mimo" in lo or "command code" in lo:
            return "cmd"
        if "claude code" in lo and "opus" in lo:
            return "claude-opus"
        if "claude code" in lo or "claude-pro/sonnet" in lo:
            return "claude-sonnet"
        if "claude sonnet" in lo or "sonnet" in lo:
            return "sonnet"
        if "claude opus" in lo or "opus" in lo:
            return "opus"
        if "gemini" in lo:
            if "high" in lo:
                return "gemini-high"
            if "medium" in lo:
                return "gemini-medium"
            if "low" in lo:
                return "gemini-low"
        return None

    def _refresh_attempt_quota_async(self, attempt: Attempt) -> None:
        target = self._model_target_from_display(attempt.model)
        if not target:
            return

        helper = HOME / ".local/bin/ai-model-quota"
        if not helper.exists():
            return

        def work() -> None:
            try:
                p = subprocess.run(
                    [str(helper), target, "--json"],
                    capture_output=True,
                    text=True,
                    timeout=85,
                )
                rows = [x for x in p.stdout.splitlines() if x.strip()]
                data = json.loads(rows[-1]) if rows else {}
                if isinstance(data, dict):
                    self.call_from_thread(self.post_message, QuotaUpdate(attempt, data))
            except Exception:
                return

        threading.Thread(target=work, daemon=True).start()

    async def on_quota_update(self, msg: QuotaUpdate) -> None:
        attempt = msg.attempt
        data = msg.data
        summary = str(data.get("summary") or "").strip()
        if not summary:
            return

        attempt.quota_summary = summary

        if attempt.panel and attempt.ended is not None:
            attempt.panel.title = (
                f"THINKING · {attempt.model} · {attempt.elapsed()} "
                f"· {len(attempt.lines)} events"
            )

        self.update_agentdock()

    def _refresh_process_diag(self) -> None:
        if now() - self._last_proc_diag_at < 5:
            return
        self._last_proc_diag_at = now()

        if not self.proc or self.proc.poll() is not None:
            self._proc_diag = "worker exited"
            return

        try:
            p = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,stat=,etime=,command="],
                capture_output=True,
                text=True,
                timeout=3,
            )
        except Exception as e:
            self._proc_diag = f"worker alive · ps unavailable: {e}"
            return

        rows: dict[int, tuple[int, str, str, str]] = {}
        for line in p.stdout.splitlines():
            parts = line.strip().split(None, 4)
            if len(parts) < 5:
                continue
            try:
                pid = int(parts[0])
                ppid = int(parts[1])
            except Exception:
                continue
            rows[pid] = (ppid, parts[2], parts[3], parts[4])

        root = self.proc.pid
        descendants = []
        frontier = [root]
        seen = {root}
        while frontier:
            parent = frontier.pop()
            for pid, (ppid, stat, etime, cmd) in rows.items():
                if ppid != parent or pid in seen:
                    continue
                seen.add(pid)
                frontier.append(pid)
                descendants.append((pid, stat, etime, cmd))

        interesting = []
        for pid, stat, etime, cmd in descendants:
            lo = cmd.lower()
            if any(x in lo for x in (" agy", "/agy", " codex", "/codex", " command-code", " cmd ", "/cmd", "ai_orch")):
                name = Path(cmd.split()[0]).name if cmd.split() else cmd
                interesting.append(f"{name}:{stat}:{etime}")

        if interesting:
            self._proc_diag = "worker alive · child " + ", ".join(interesting[:4])
        elif descendants:
            self._proc_diag = f"worker alive · {len(descendants)} child process(es)"
        else:
            self._proc_diag = "worker alive · no child process visible"

    def _diag_text(self) -> str:
        current = self.current
        if current and current.ended is None:
            quiet = now() - current.last_event_at
            quiet_text = self._format_age(quiet)
            model = current.model
        else:
            quiet_text = "-"
            model = "-"

        running = bool(self.proc and self.proc.poll() is None)
        return (
            f"main_worker: {'RUNNING' if running else 'idle'}\n"
            f"current_model: {model}\n"
            f"last_stream_event_age: {quiet_text}\n"
            f"process_tree: {self._proc_diag}\n"
            f"delegate_workers_seen: {len(self.delegate_states)}\n"
            "watchdog: informational only; v2.13 does not auto-kill a quiet model"
        )

    def _load_tui_state(self) -> None:
        try:
            data = json.loads(TUI_STATE_FILE.read_text())
        except Exception:
            data = {}
        self.router_enabled = bool(data.get("router_enabled", True))
        self.model_override = str(data.get("model_override", "auto"))
        scope = str(data.get("scope_mode", "contextual")).lower()
        self.scope_mode = scope if scope in {"contextual", "strict"} else "contextual"
        perm = str(data.get("permission_profile", "guarded")).lower()
        self.permission_profile = perm if perm in {"trusted", "guarded"} else "guarded"
        notify = str(data.get("notify_mode", "smart")).lower()
        self.notify_mode = notify if notify in {"smart", "all", "important", "off"} else "smart"
        plan = data.get("pending_branch_plan")
        self.pending_branch_plan = plan if isinstance(plan, dict) else None

    def _save_tui_state(self) -> None:
        atomic_json(
            TUI_STATE_FILE,
            {
                "router_enabled": self.router_enabled,
                "model_override": self.model_override,
                "scope_mode": self.scope_mode,
                "permission_profile": self.permission_profile,
                "notify_mode": self.notify_mode,
                "pending_branch_plan": self.pending_branch_plan,
                "updated_at": iso(),
            },
        )

    def _current_runtime_state(self) -> dict[str, Any]:
        return {
            "model_override": self.model_override,
            "router_enabled": self.router_enabled,
            "scope_mode": self.scope_mode,
            "permission_profile": self.permission_profile,
        }


    def _installed_version_info(self) -> dict[str, Any]:
        try:
            data = json.loads(VERSION_FILE.read_text())
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _version_status_text(self) -> str:
        data = self._installed_version_info()
        installed = str(data.get("release") or "unknown")
        installed_tui = installed
        current = installed == TUI_COMPONENT_VERSION
        return (
            f"loaded_tui: {TUI_COMPONENT_VERSION}\n"
            f"installed_release: {installed}\n"
            f"installed_tui: {installed_tui}\n"
            f"status: {'CURRENT' if current else 'RESTART_NEEDED'}\n"
            f"pid: {os.getpid()}\n"
            f"project: {PROJECT_NAME or self.repo.name}\n"
            f"workspace: {WORKSPACE_ID or '-'}"
        )

    def _reload_project_settings(self) -> str:
        before = self._current_runtime_state()
        self._load_tui_state()
        self.queue_state = self._load_queue_state()
        self._last_quota_refresh = 0.0
        self.update_banner()
        self.update_commandbar()
        after = self._current_runtime_state()
        changed = [
            key for key in ("model_override", "router_enabled", "scope_mode", "permission_profile", "notify_mode")
            if before.get(key) != after.get(key)
        ]
        changed_text = ", ".join(changed) if changed else "none"
        return (
            "프로젝트/TUI 저장 설정을 다시 읽었습니다.\n"
            f"changed: {changed_text}\n"
            "provider config는 각 새 MAIN 실행 시 디스크에서 다시 읽습니다.\n"
            "현재 입력창/첨부/실행 중 worker는 건드리지 않았습니다."
        )

    def _schedule_self_restart(self) -> None:
        helper = HOME / ".local/bin/orch-project"
        if not helper.exists():
            self.note(f"restart helper missing: {helper}", title="RESTART ERROR", collapsed=False)
            return
        try:
            self._save_tui_state()
            self._save_queue_state()
            if self.job_dir:
                self.checkpoint()
            target = WORKSPACE_ID or str(self.repo)
            subprocess.Popen(
                [
                    str(helper), "restart", target,
                    "--no-attach", "--delay", "0.8",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except Exception as e:
            self.note(f"TUI 재시작 예약 실패: {e}", title="RESTART ERROR", collapsed=False)
            return

        # MAIN workers are launched in their own process group. Do not terminate
        # them here: the fresh TUI will recover the persisted lease/result.
        self.exit()

    def _notify_status_text(self) -> str:
        backend = desktop_notify_backend_status()
        descriptions = {
            "smart": "단일 COMPLETE, queue 최종 COMPLETE, BLOCKED/NEEDS_USER/NEEDS_GO를 알림",
            "all": "모든 MAIN 종료 상태를 알림 (delegate 개별 완료는 제외)",
            "important": "BLOCKED/NEEDS_USER/NEEDS_GO/FAILED 등 확인 필요 상태만 알림",
            "off": "알림 비활성화",
        }
        return (
            f"mode: {self.notify_mode}\n"
            f"backend: {backend}\n"
            f"behavior: {descriptions.get(self.notify_mode, descriptions['smart'])}\n"
            "delegate/subagent 개별 완료는 어떤 모드에서도 알리지 않습니다."
        )

    def _job_elapsed_text(self) -> str:
        if not self.job:
            return "-"
        try:
            created = datetime.fromisoformat(str(self.job.get("created_at") or ""))
            seconds = max(0, int(now() - created.timestamp()))
        except Exception:
            if self.attempts:
                return self.attempts[-1].elapsed()
            return "-"
        h, rem = divmod(seconds, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return f"{h}h {m:02d}m"
        if m:
            return f"{m}m {sec:02d}s"
        return f"{sec}s"

    def _notification_payload(
        self,
        status: str,
        result: dict[str, Any] | None = None,
        rc: int = 0,
    ) -> dict[str, str] | None:
        mode = str(getattr(self, "notify_mode", "smart") or "smart").lower()
        if mode == "off":
            return None

        status = str(status or "").upper()
        result = result if isinstance(result, dict) else {}
        attention = status in {"BLOCKED", "NEEDS_USER", "NEEDS_GO", "FAILED"}
        if status == "CANCELLED" and mode != "all":
            return None
        if status == "COMPLETE" and mode == "important":
            return None
        if not attention and status not in {"COMPLETE", "CANCELLED"}:
            return None

        items = self.queue_state.get("items", []) if isinstance(self.queue_state, dict) else []
        items = items if isinstance(items, list) else []
        active = str(self.queue_state.get("active_entry_id") or "") if isinstance(self.queue_state, dict) else ""
        gate = self.queue_state.get("gate") if isinstance(self.queue_state, dict) else None
        queue_context = bool(items or active or isinstance(gate, dict))
        pending = max(0, len(items) - 1) if active else len(items)
        paused = bool(self.queue_state.get("paused")) if isinstance(self.queue_state, dict) else False

        model = str(result.get("main_name") or "").strip()
        if not model and self.attempts:
            model = str(self.attempts[-1].model or "").strip()
        model = model or str(self.model_override or "auto")
        elapsed = self._job_elapsed_text()
        project = PROJECT_NAME or self.repo.name
        branch = str((self.job or {}).get("branch_at_end") or git_branch(self.repo))
        task = str((self.job or {}).get("title") or "task").strip()
        title = f"OrchBridge · {project}"

        if attention:
            queue_note = f" · queue {pending} waiting" if queue_context and pending else ""
            return {
                "key": f"job:{str((self.job or {}).get('id') or '-')}:attention:{status}",
                "title": title,
                "subtitle": f"{status} · 확인 필요",
                "message": f"{task} · {model} · {elapsed} · branch {branch}{queue_note}",
                "sound": "Basso",
            }

        if status == "CANCELLED":
            return {
                "key": f"job:{str((self.job or {}).get('id') or '-')}:cancelled",
                "title": title,
                "subtitle": f"CANCELLED · {model}",
                "message": f"{task} · {elapsed} · branch {branch}",
                "sound": "",
            }

        # COMPLETE
        if queue_context:
            if pending > 0:
                if paused:
                    # The chain cannot continue automatically, so smart should tell the user.
                    return {
                        "key": f"job:{str((self.job or {}).get('id') or '-')}:queue-paused",
                        "title": title,
                        "subtitle": f"COMPLETE · Queue paused · {pending} waiting",
                        "message": f"{task} · {model} · {elapsed} · branch {branch}",
                        "sound": "Glass",
                    }
                if mode == "smart":
                    return None
                return {
                    "key": f"job:{str((self.job or {}).get('id') or '-')}:complete",
                    "title": title,
                    "subtitle": f"COMPLETE · {model} · queue {pending} remaining",
                    "message": f"{task} · {elapsed} · branch {branch}",
                    "sound": "Glass",
                }
            # Last claimed queue entry completed. In smart mode this is the single
            # completion notification for the whole automatic queue chain.
            if active:
                return {
                    "key": f"job:{str((self.job or {}).get('id') or '-')}:queue-complete",
                    "title": title,
                    "subtitle": f"QUEUE COMPLETE · {model} · {elapsed}",
                    "message": f"마지막 작업 완료 · {task} · branch {branch}",
                    "sound": "Glass",
                }

        return {
            "key": f"job:{str((self.job or {}).get('id') or '-')}:complete",
            "title": title,
            "subtitle": f"COMPLETE · {model} · {elapsed}",
            "message": f"{task} · branch {branch}",
            "sound": "Glass",
        }

    def _send_notification_payload(self, payload: dict[str, str] | None) -> None:
        if not payload:
            return
        key = str(payload.get("key") or "")
        if key and key in self._notification_seen:
            return
        if key:
            self._notification_seen.add(key)

        def work() -> None:
            result = send_notification(
                title=str(payload.get("title") or "OrchBridge"),
                subtitle=str(payload.get("subtitle") or ""),
                message=str(payload.get("message") or ""),
                sound=str(payload.get("sound") or ""),
                group=f"orchbridge:{WORKSPACE_ID or self.repo.name}",
            )
            self._notify_last_result = result
            if not bool(result.get("ok")) and not self._notify_error_noted:
                self._notify_error_noted = True
                try:
                    self.call_from_thread(
                        self.note,
                        f"데스크탑 알림 전송 실패: {result.get('reason') or 'unknown'}\n/notify test 로 다시 확인할 수 있습니다.",
                        title="NOTIFY",
                        collapsed=False,
                    )
                except Exception:
                    pass

        threading.Thread(target=work, name="orch-desktop-notify", daemon=True).start()

    def _maybe_notify_terminal(
        self,
        status: str,
        result: dict[str, Any] | None = None,
        rc: int = 0,
    ) -> None:
        self._send_notification_payload(self._notification_payload(status, result, rc))

    def _notify_test(self) -> dict[str, Any]:
        result = send_notification(
            title=f"OrchBridge · {PROJECT_NAME or self.repo.name}",
            subtitle=f"v{TUI_COMPONENT_VERSION} · 알림 테스트",
            message="데스크탑 알림이 정상적으로 연결되었습니다.",
            sound="Glass",
            group=f"orchbridge:{WORKSPACE_ID or self.repo.name}:test",
        )
        self._notify_last_result = result
        return result

    def _branch_plan_text(self, plan: dict[str, Any] | None) -> str:
        if not plan:
            return "현재 브랜치 그대로"
        mode = str(plan.get("mode") or "pin")
        if mode in {"pin", "switch"}:
            return f"{mode}:{plan.get('branch') or '-'}"
        if mode == "new":
            return f"new:{plan.get('branch') or '-'} <- {plan.get('from') or 'HEAD'}"
        return str(plan)

    def _branch_default_plan(self) -> dict[str, Any] | None:
        branch = git_branch(self.repo)
        if branch in {"-", "detached", ""}:
            return None
        return {"mode": "pin", "branch": branch}

    def _branch_next_plan(self) -> dict[str, Any] | None:
        return dict(self.pending_branch_plan) if isinstance(self.pending_branch_plan, dict) else self._branch_default_plan()

    def _branch_consume_pending(self) -> None:
        if self.pending_branch_plan is not None:
            self.pending_branch_plan = None
            self._save_tui_state()

    def _branch_live_blocker(self) -> str | None:
        if self.proc and self.proc.poll() is None:
            return "현재 MAIN 작업이 실행 중입니다. 브랜치 변경은 작업 종료 후 가능합니다."
        live = self._live_main_worker()
        if live:
            return f"현재/복구 MAIN worker가 실행 중입니다 (pid {live.get('pid')})."
        foreign = self._foreign_live_same_repo_worker()
        if foreign:
            return f"같은 저장소의 다른 MAIN worker가 실행 중입니다 (pid {foreign.get('pid')})."
        return None

    def _branch_switch_existing(self, branch: str) -> None:
        blocker = self._branch_live_blocker()
        if blocker:
            raise RuntimeError(blocker)
        branch = branch.strip()
        if not git_valid_branch_name(self.repo, branch):
            raise RuntimeError(f"유효하지 않은 브랜치 이름: {branch}")
        current = git_branch(self.repo)
        if current == branch:
            return
        dirty = git_dirty_count(self.repo)
        if dirty != 0:
            raise RuntimeError(
                f"작업 트리가 clean이 아닙니다 ({dirty if dirty >= 0 else '?'} changes). "
                "기존 브랜치 전환은 commit/stash 후 다시 시도하세요."
            )
        if git_local_branch_exists(self.repo, branch):
            argv = ["git", "switch", branch]
        elif git_origin_branch_exists(self.repo, branch):
            argv = ["git", "switch", "--track", "-c", branch, f"origin/{branch}"]
        else:
            raise RuntimeError(f"로컬/origin에 브랜치가 없습니다: {branch}")
        p = subprocess.run(argv, cwd=self.repo, capture_output=True, text=True, timeout=30)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.strip() or p.stdout.strip() or f"git switch exit={p.returncode}")
        if git_branch(self.repo) != branch:
            raise RuntimeError(f"브랜치 전환 검증 실패: expected={branch} actual={git_branch(self.repo)}")

    def _branch_create(self, branch: str, from_ref: str = "HEAD") -> None:
        blocker = self._branch_live_blocker()
        if blocker:
            raise RuntimeError(blocker)
        branch = branch.strip(); from_ref = (from_ref or "HEAD").strip()
        if not git_valid_branch_name(self.repo, branch):
            raise RuntimeError(f"유효하지 않은 브랜치 이름: {branch}")
        if git_local_branch_exists(self.repo, branch):
            if git_branch(self.repo) == branch:
                return
            raise RuntimeError(f"이미 존재하는 로컬 브랜치입니다: {branch}")
        base_oid = git_ref_oid(self.repo, from_ref)
        if not base_oid:
            raise RuntimeError(f"기준 ref를 찾을 수 없습니다: {from_ref}")
        current_oid = git_head(self.repo)
        dirty = git_dirty_count(self.repo)
        # Creating from the exact current HEAD only changes the branch ref and
        # safely carries the existing working tree. Creating from another ref
        # would rewrite files, so require a clean tree.
        if dirty != 0 and base_oid != current_oid:
            raise RuntimeError(
                f"작업 트리가 clean이 아닙니다 ({dirty if dirty >= 0 else '?'} changes). "
                f"다른 기준({from_ref})에서 새 브랜치를 만들려면 commit/stash가 필요합니다."
            )
        p = subprocess.run(
            ["git", "switch", "-c", branch, from_ref], cwd=self.repo,
            capture_output=True, text=True, timeout=30,
        )
        if p.returncode != 0:
            raise RuntimeError(p.stderr.strip() or p.stdout.strip() or f"git switch -c exit={p.returncode}")
        if git_branch(self.repo) != branch:
            raise RuntimeError(f"새 브랜치 검증 실패: expected={branch} actual={git_branch(self.repo)}")

    def _branch_apply_plan(self, plan: dict[str, Any] | None) -> None:
        if not plan:
            return
        mode = str(plan.get("mode") or "pin")
        branch = str(plan.get("branch") or "").strip()
        if not branch:
            return
        if mode in {"pin", "switch"}:
            self._branch_switch_existing(branch)
            return
        if mode == "new":
            # Idempotent after a crash/retry: if the target branch was already
            # created and is current, the plan is already satisfied.
            if git_branch(self.repo) == branch:
                return
            self._branch_create(branch, str(plan.get("from") or "HEAD"))
            return
        raise RuntimeError(f"알 수 없는 branch plan mode: {mode}")

    def _queue_default_state(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "paused": False,
            "halted_reason": None,
            "gate": None,
            "active_entry_id": None,
            "items": [],
            "updated_at": iso(),
        }

    def _queue_workspace_ready(self) -> bool:
        return bool(WORKSPACE_ID) and PROJECT_BASE != BASE.resolve()

    def _load_queue_state(self) -> dict[str, Any]:
        default = self._queue_default_state()
        if not QUEUE_FILE.exists():
            return default
        try:
            data = json.loads(QUEUE_FILE.read_text())
            if not isinstance(data, dict):
                raise ValueError("queue root is not an object")
            items = data.get("items", [])
            if not isinstance(items, list):
                raise ValueError("queue items is not a list")
            out = default
            out.update(data)
            out["items"] = [x for x in items if isinstance(x, dict)]
            out["schema_version"] = 1
            return out
        except Exception as e:
            # Fail closed. Atomic writes make this unlikely, but never silently
            # overwrite a malformed queue that may contain user tasks.
            default["paused"] = True
            default["halted_reason"] = f"QUEUE_FILE_INVALID: {type(e).__name__}: {e}"
            return default

    def _save_queue_state(self) -> None:
        self.queue_state["schema_version"] = 1
        self.queue_state["updated_at"] = iso()
        atomic_json(QUEUE_FILE, self.queue_state)

    def _queue_gate_for_current_job(self) -> dict[str, Any] | None:
        if not self.job or not self.job_dir:
            return None
        return {
            "job_id": str(self.job.get("id") or self.job_dir.name),
            "job_dir": str(self.job_dir.resolve()),
        }

    def _queue_busy_gate(self) -> dict[str, Any] | None:
        if self.proc is not None and self.proc.poll() is None:
            return self._queue_gate_for_current_job()
        live = self._live_main_worker()
        if live:
            return self._queue_gate_for_current_job()
        foreign = self._foreign_live_same_repo_worker()
        if foreign:
            return {
                "job_id": str(foreign.get("job_id") or ""),
                "job_dir": str(foreign.get("job_dir") or ""),
            }
        return None

    def _queue_gate_snapshot(self) -> tuple[str | None, dict[str, Any] | None]:
        gate = self.queue_state.get("gate")
        if not isinstance(gate, dict):
            return None, None
        job_dir_text = str(gate.get("job_dir") or "").strip()
        if not job_dir_text:
            return "MISSING", None
        path = Path(job_dir_text) / "job.json"
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                return "INVALID", None
        except FileNotFoundError:
            return "MISSING", None
        except Exception:
            return "INVALID", None
        return str(data.get("status") or "UNKNOWN").upper(), data

    def _submission_fingerprint(self, prompt: str, attachments: list[str]) -> str:
        payload = {
            "prompt": str(prompt).strip(),
            "attachments": sorted({str(x) for x in attachments if str(x)}),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _recent_submit_duplicate(self, prompt: str, attachments: list[str]) -> bool:
        fp = self._submission_fingerprint(prompt, attachments)
        return bool(
            fp
            and fp == self._last_submit_fingerprint
            and 0.0 <= now() - self._last_submit_at <= SUBMIT_DEDUPE_SECONDS
        )

    def _remember_submit(self, prompt: str, attachments: list[str]) -> None:
        self._last_submit_fingerprint = self._submission_fingerprint(prompt, attachments)
        self._last_submit_at = now()

    def _recent_queue_duplicate(
        self, prompt_sha: str, attachments: list[str]
    ) -> dict[str, Any] | None:
        wanted_attachments = sorted({str(x) for x in attachments if str(x)})
        for item in reversed(self.queue_state.get("items", [])):
            if str(item.get("prompt_sha256") or "") != prompt_sha:
                continue
            existing_attachments = sorted(
                {str(x) for x in item.get("attachments", []) if str(x)}
            )
            if existing_attachments != wanted_attachments:
                continue
            try:
                created = datetime.fromisoformat(str(item.get("created_at") or ""))
                age = now() - created.timestamp()
            except Exception:
                continue
            if 0.0 <= age <= SUBMIT_DEDUPE_SECONDS:
                return item
        return None

    def _queue_entry_index(self, entry_id: str) -> int | None:
        for i, item in enumerate(self.queue_state.get("items", [])):
            if str(item.get("id") or "") == entry_id:
                return i
        return None

    def _queue_enqueue(
        self,
        prompt: str,
        attachments: list[str],
        *,
        gate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._queue_workspace_ready():
            raise RuntimeError(
                "persistent queue requires a registered project workspace; "
                "reopen with orch-project open <repo>"
            )
        prompt, contaminated = sanitize_terminal_input(prompt)
        if contaminated:
            raise ValueError(
                "terminal control sequence detected in queued task; re-enter the prompt"
            )
        prompt = prompt.strip()
        if not prompt and not attachments:
            raise ValueError("queued task is empty")
        if not prompt:
            prompt = "Analyze the attached file(s) and complete the task implied by their contents."
        prompt_sha = self._prompt_sha(prompt)
        duplicate = self._recent_queue_duplicate(prompt_sha, attachments)
        if duplicate is not None:
            result = dict(duplicate)
            result["_duplicate_suppressed"] = True
            return result
        entry_id = (
            "queue-" + datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-")
            + hashlib.sha256((prompt + str(now())).encode("utf-8")).hexdigest()[:8]
        )
        entry = {
            "id": entry_id,
            "created_at": iso(),
            "prompt": prompt,
            "prompt_sha256": prompt_sha,
            "attachments": list(dict.fromkeys(str(x) for x in attachments if str(x))),
            "runtime": self._current_runtime_state(),
            "branch_plan": self._branch_next_plan(),
        }
        self.queue_state.setdefault("items", []).append(entry)
        if gate and not self.queue_state.get("gate") and not self.queue_state.get("active_entry_id"):
            self.queue_state["gate"] = gate
        self._save_queue_state()
        return entry

    def _queue_halt(self, reason: str) -> None:
        reason = str(reason).strip() or "queue halted"
        if self.queue_state.get("halted_reason") == reason:
            return
        self.queue_state["halted_reason"] = reason
        self._save_queue_state()
        self.note(
            reason + "\n/queue resume 을 실행하기 전에는 다음 작업을 자동 시작하지 않습니다.",
            title="QUEUE HALTED",
            collapsed=False,
        )

    def _queue_repair_active_gate(self) -> None:
        active = str(self.queue_state.get("active_entry_id") or "")
        if not active:
            return
        gate = self.queue_state.get("gate")
        if isinstance(gate, dict) and gate.get("job_dir"):
            return
        if self.job and self.job_dir and str(self.job.get("queue_entry_id") or "") == active:
            self.queue_state["gate"] = self._queue_gate_for_current_job()
            self._save_queue_state()
            return
        for d in sorted(JOBS_DIR.glob("job-*"), reverse=True):
            try:
                j = json.loads((d / "job.json").read_text())
            except Exception:
                continue
            if str(j.get("queue_entry_id") or "") == active:
                self.queue_state["gate"] = {
                    "job_id": str(j.get("id") or d.name),
                    "job_dir": str(d.resolve()),
                }
                self._save_queue_state()
                return
        # Claim happened before a job was durably created. Leave the item in
        # place and release the claim so it can be tried again.
        self.queue_state["active_entry_id"] = None
        self._save_queue_state()

    def _queue_apply_runtime(self, entry: dict[str, Any]) -> None:
        runtime = entry.get("runtime") if isinstance(entry.get("runtime"), dict) else {}
        model = str(runtime.get("model_override") or self.model_override)
        scope = str(runtime.get("scope_mode") or self.scope_mode).lower()
        perm = str(runtime.get("permission_profile") or self.permission_profile).lower()
        self.model_override = model
        self.router_enabled = bool(runtime.get("router_enabled", self.router_enabled))
        self.scope_mode = scope if scope in {"contextual", "strict"} else self.scope_mode
        self.permission_profile = perm if perm in {"trusted", "guarded"} else self.permission_profile
        self._save_tui_state()

    def _queue_launch_next(self) -> None:
        if self.queue_state.get("paused") or self.queue_state.get("halted_reason"):
            return
        items = self.queue_state.get("items", [])
        if not items or self.queue_state.get("active_entry_id"):
            return
        if self._queue_busy_gate():
            return

        entry = items[0]
        entry_id = str(entry.get("id") or "")
        if not entry_id:
            self._queue_halt("QUEUE_ENTRY_INVALID: id가 없는 대기 작업을 발견했습니다.")
            return

        # Persist the claim before creating the job. A restart can repair this
        # claim from job.queue_entry_id, or release it if no job was created.
        self.queue_state["active_entry_id"] = entry_id
        self._save_queue_state()
        try:
            self._branch_apply_plan(entry.get("branch_plan") if isinstance(entry.get("branch_plan"), dict) else None)
            self._queue_apply_runtime(entry)
            attachments = [str(x) for x in entry.get("attachments", []) if str(x)]
            self.new_job(
                str(entry.get("prompt") or ""),
                attachments=attachments,
                queue_entry_id=entry_id,
            )
            self.queue_state["gate"] = self._queue_gate_for_current_job()
            self.queue_state["halted_reason"] = None
            self._save_queue_state()
            self.note(
                f"다음 작업 자동 시작 · {entry_id}\n남은 대기 작업: {max(0, len(items)-1)}",
                title="QUEUE START",
                collapsed=False,
            )
        except Exception as e:
            # Keep the queue item. If a job was created, recovery will repair
            # the active claim; otherwise release it for an explicit retry.
            if not (self.job and str(self.job.get("queue_entry_id") or "") == entry_id):
                self.queue_state["active_entry_id"] = None
            self.queue_state["halted_reason"] = (
                f"QUEUE_LAUNCH_FAILED: {type(e).__name__}: {e}"
            )
            self._save_queue_state()
            self.note(
                self.queue_state["halted_reason"],
                title="QUEUE HALTED",
                collapsed=False,
            )

    def _queue_complete_gate(self) -> None:
        active = str(self.queue_state.get("active_entry_id") or "")
        if active:
            self.queue_state["items"] = [
                x for x in self.queue_state.get("items", [])
                if str(x.get("id") or "") != active
            ]
        self.queue_state["active_entry_id"] = None
        self.queue_state["gate"] = None
        self.queue_state["halted_reason"] = None
        self._save_queue_state()

    def _queue_tick(self) -> None:
        if not self._queue_workspace_ready():
            return
        self._queue_repair_active_gate()
        items = self.queue_state.get("items", [])
        if not items:
            if self.queue_state.get("active_entry_id") or self.queue_state.get("gate"):
                self.queue_state["active_entry_id"] = None
                self.queue_state["gate"] = None
                self._save_queue_state()
            return

        # If work is already running but no gate was persisted yet, attach the
        # queue to that exact job instead of treating the repository as idle.
        busy_gate = self._queue_busy_gate()
        if busy_gate and not self.queue_state.get("gate"):
            self.queue_state["gate"] = busy_gate
            self._save_queue_state()

        gate = self.queue_state.get("gate")
        if isinstance(gate, dict):
            status, job = self._queue_gate_snapshot()
            if status == "COMPLETE":
                self._queue_complete_gate()
            elif status in {"BLOCKED", "NEEDS_USER", "NEEDS_GO", "FAILED", "CANCELLED"}:
                jid = str((job or {}).get("id") or gate.get("job_id") or "unknown")
                self._queue_halt(
                    f"{jid} 이(가) {status} 상태로 끝나 대기열을 안전하게 중단했습니다."
                )
                return
            elif status == "READY":
                # A crash can leave a claimed queued job durably READY before
                # worker launch. Re-adopt and start it instead of duplicating it.
                active = str(self.queue_state.get("active_entry_id") or "")
                job_dir_text = str(gate.get("job_dir") or "")
                if active and job and str(job.get("queue_entry_id") or "") == active and not self._queue_busy_gate():
                    self.job_dir = Path(job_dir_text)
                    self.job = job
                    try:
                        prompt = (self.job_dir / "original-prompt.md").read_text()
                    except Exception:
                        prompt = ""
                    if prompt:
                        self._mount_user_message(prompt)
                    self.start(False)
                return
            elif status in {"RUNNING", "PAUSED_QUOTA", "PAUSED_RETRY", "PAUSED_USER"}:
                return
            elif status in {"MISSING", "INVALID", "UNKNOWN", None}:
                self._queue_halt(
                    f"QUEUE_GATE_{status or 'UNKNOWN'}: 이전 작업 상태를 안전하게 확인할 수 없습니다."
                )
                return
            else:
                return

        if self.queue_state.get("paused") or self.queue_state.get("halted_reason"):
            return
        if self._queue_busy_gate():
            return
        self._queue_launch_next()

    def _queue_status_text(self) -> str:
        items = self.queue_state.get("items", [])
        active = str(self.queue_state.get("active_entry_id") or "")
        gate_status, gate_job = self._queue_gate_snapshot()
        if self.queue_state.get("halted_reason"):
            state = "HALTED"
        elif self.queue_state.get("paused"):
            state = "PAUSED"
        else:
            state = "ACTIVE"
        lines = [
            f"상태: {state}",
            f"파일: {QUEUE_FILE}",
            f"게이트: {str((gate_job or {}).get('id') or (self.queue_state.get('gate') or {}).get('job_id') or '-')} · {gate_status or '-'}",
            f"대기 항목: {len(items)}",
        ]
        if self.queue_state.get("halted_reason"):
            lines.append(f"중단 이유: {self.queue_state.get('halted_reason')}")
        for i, item in enumerate(items, 1):
            prompt = str(item.get("prompt") or "").splitlines()[0][:100]
            marker = "RUNNING" if str(item.get("id") or "") == active else "PENDING"
            attachments = len(item.get("attachments", []) or [])
            model = str((item.get("runtime") or {}).get("model_override") or "auto")
            branch_plan = self._branch_plan_text(item.get("branch_plan") if isinstance(item.get("branch_plan"), dict) else None)
            lines.append(
                f"{i}. [{marker}] {prompt or '(attachment task)'} · model={model} · branch={branch_plan} · attachments={attachments} · {item.get('id')}"
            )
        if not items:
            lines.append("대기 중인 다음 작업이 없습니다.")
        return "\n".join(lines)

    def _queue_remove(self, token: str) -> tuple[bool, str]:
        items = self.queue_state.get("items", [])
        if not items:
            return False, "대기열이 비어 있습니다."
        index: int | None = None
        if token.isdigit():
            n = int(token)
            if 1 <= n <= len(items):
                index = n - 1
        else:
            for i, item in enumerate(items):
                if str(item.get("id") or "") == token:
                    index = i
                    break
        if index is None:
            return False, "해당 대기 작업을 찾지 못했습니다."
        item = items[index]
        if str(item.get("id") or "") == str(self.queue_state.get("active_entry_id") or ""):
            return False, "이미 실행을 시작한 큐 작업은 삭제할 수 없습니다. 현재 작업은 /cancel 로 제어하세요."
        removed = items.pop(index)
        self.queue_state["items"] = items
        if not items and not self.queue_state.get("active_entry_id"):
            self.queue_state["gate"] = None
            self.queue_state["halted_reason"] = None
        self._save_queue_state()
        return True, f"삭제됨: {removed.get('id')}"

    def _queue_clear_pending(self) -> int:
        active = str(self.queue_state.get("active_entry_id") or "")
        old = list(self.queue_state.get("items", []))
        if active:
            keep = [x for x in old if str(x.get("id") or "") == active]
        else:
            keep = []
        removed = len(old) - len(keep)
        self.queue_state["items"] = keep
        if not keep:
            self.queue_state["gate"] = None
            self.queue_state["active_entry_id"] = None
            self.queue_state["halted_reason"] = None
        self._save_queue_state()
        return removed

    def _queue_resume_explicit(self) -> None:
        gate_status, _job = self._queue_gate_snapshot()
        active = str(self.queue_state.get("active_entry_id") or "")
        # Explicit resume is the user's authorization to move past a terminal
        # non-COMPLETE gate. If it was a queued task, drop that finished/blocked
        # item; a missing/invalid gate instead retries the same queued item.
        if self.queue_state.get("halted_reason"):
            if gate_status in {"BLOCKED", "NEEDS_USER", "NEEDS_GO", "FAILED", "CANCELLED"}:
                if active:
                    self.queue_state["items"] = [
                        x for x in self.queue_state.get("items", [])
                        if str(x.get("id") or "") != active
                    ]
                self.queue_state["active_entry_id"] = None
                self.queue_state["gate"] = None
            elif gate_status in {"MISSING", "INVALID", "UNKNOWN", None}:
                self.queue_state["active_entry_id"] = None
                self.queue_state["gate"] = None
        self.queue_state["paused"] = False
        self.queue_state["halted_reason"] = None
        self._save_queue_state()
        self._queue_tick()

    def _prompt_sha(self, prompt: str) -> str:
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    def _parse_job_directives(self, prompt: str) -> tuple[str, int]:
        """
        Explicit prompt directives understood by the local control plane.

        ORCH_SCOPE: STRICT|CONTEXTUAL
        ORCH_MAX_DELEGATES: N
        """
        scope = self.scope_mode
        max_delegates = 1 if scope == "strict" else 3

        for line in prompt.splitlines()[:8]:
            m = re.match(r"^\s*ORCH_SCOPE\s*:\s*(STRICT|CONTEXTUAL)\s*$", line, re.I)
            if m:
                scope = m.group(1).lower()
                max_delegates = 1 if scope == "strict" else max_delegates
                continue

            m = re.match(r"^\s*ORCH_MAX_DELEGATES\s*:\s*(\d+)\s*$", line, re.I)
            if m:
                max_delegates = max(1, min(8, int(m.group(1))))

        return scope, max_delegates

    def _current_prompt_text(self) -> str:
        if not self.job_dir:
            return ""
        p = self.job_dir / "original-prompt.md"
        try:
            return p.read_text()
        except Exception:
            return ""

    def _latest_router_lines(self) -> tuple[str | None, str | None]:
        decision = None
        route = self.route or None
        if self.job_dir:
            p = self.job_dir / "raw-events.jsonl"
            if p.exists():
                try:
                    for row in p.read_text().splitlines()[-1200:]:
                        ev = json.loads(row)
                        line = str(ev.get("line", ""))
                        if "[ai-orch] decision:" in line:
                            decision = line.split("[ai-orch] decision:", 1)[1].strip()
                        elif "[ai-orch] route:" in line:
                            route = line.split("[ai-orch] route:", 1)[1].strip()
                except Exception:
                    pass
        return decision, route

    def _project_skills_text(self) -> str:
        roots = [
            self.repo / ".agents/skills",
            self.repo / ".codex/skills",
            self.repo / ".commandcode/skills",
        ]
        rows = []
        seen = set()
        for root in roots:
            if not root.exists():
                continue
            names = []
            for child in sorted(root.iterdir()):
                if not child.is_dir() and not child.is_symlink():
                    continue
                if child.name in seen:
                    continue
                seen.add(child.name)
                names.append(child.name)
            if names:
                rows.append(f"{root.relative_to(self.repo)}: " + ", ".join(names))
        return "\n".join(rows) if rows else "No project skills detected."

    def _mcp_text(self) -> str:
        rows = []

        # Command Code project MCP/settings.
        for p in (
            self.repo / ".commandcode/settings.local.json",
            self.repo / ".commandcode/settings.json",
            self.repo / ".mcp.json",
        ):
            if not p.exists():
                continue
            try:
                data = json.loads(p.read_text())
            except Exception:
                rows.append(f"{p.name}: present (unparsed)")
                continue

            names = []
            for key in ("mcpServers", "mcp_servers", "mcp"):
                value = data.get(key)
                if isinstance(value, dict):
                    names.extend(value.keys())
            rows.append(
                f"{p.relative_to(self.repo)}: "
                + (", ".join(sorted(set(names))) if names else "present")
            )

        # Codex TOML MCP servers.
        codex_cfg = HOME / ".codex/config.toml"
        if codex_cfg.exists():
            try:
                import tomllib
                data = tomllib.loads(codex_cfg.read_text())
                servers = data.get("mcp_servers", {})
                if isinstance(servers, dict):
                    rows.append(
                        "~/.codex/config.toml: "
                        + (", ".join(sorted(servers)) if servers else "no MCP servers")
                    )
            except Exception:
                rows.append("~/.codex/config.toml: present (unparsed)")

        # AGY / Gemini settings: show discovered MCP names without secrets.
        agy_settings = HOME / ".gemini/antigravity-cli/settings.json"
        if agy_settings.exists():
            try:
                data = json.loads(agy_settings.read_text())
                names = []
                for key in ("mcpServers", "mcp_servers", "mcp"):
                    value = data.get(key)
                    if isinstance(value, dict):
                        names.extend(value.keys())
                rows.append(
                    "~/.gemini/antigravity-cli/settings.json: "
                    + (", ".join(sorted(set(names))) if names else "present")
                )
            except Exception:
                rows.append("~/.gemini/antigravity-cli/settings.json: present (unparsed)")

        return "\n".join(rows) if rows else "No MCP configuration detected."

    def _clear_feed_for_new_session(self) -> None:
        feed = self.query_one("#feed", VerticalScroll)
        try:
            feed.remove_children()
        except Exception:
            pass
        self.attempts.clear()
        self.current = None
        self.route = ""
        self.quota = ""
        self.task_status = None
        self.raw.clear()
        self._copy_messages.clear()
        self.job = None
        self.job_dir = None
        self._recovered_main_result_posted = False
        self.update_agentdock()
        self.update_banner()

    def _delegate_label(self, target: str) -> str:
        mapping = {
            "codex": "GPT-6 Luna",
            "cmd": "MiMo V2.5 Pro",
            "claude-sonnet": "Claude Code Sonnet · Pro",
            "claude-opus": "Claude Code Opus · Pro",
            "sonnet": "AGY Claude Sonnet 4.6 Thinking",
            "opus": "AGY Claude Opus 4.6 Thinking",
            "gemini-low": "Gemini 3.8 Flash · low",
            "gemini-medium": "Gemini 3.8 Flash · medium",
            "gemini-high": "Gemini 3.8 Flash · high",
        }
        return mapping.get(target, target)

    def _poll_delegate_events(self) -> None:
        p = DELEGATION_EVENTS_FILE
        if not p.exists():
            return

        current_job = (self.job or {}).get("id")
        if not current_job:
            try:
                self._delegate_event_offset = p.stat().st_size
            except Exception:
                pass
            return

        try:
            size = p.stat().st_size
            if self._delegate_event_offset > size:
                self._delegate_event_offset = 0

            with p.open("r") as f:
                f.seek(self._delegate_event_offset)
                rows = f.readlines()
                self._delegate_event_offset = f.tell()
        except Exception:
            return

        for row in rows:
            try:
                ev = json.loads(row)
            except Exception:
                continue

            parent = ev.get("parent_job_id")
            if current_job and parent != current_job:
                continue

            wid = str(ev.get("worker_id", ""))
            if not wid:
                continue

            previous = self.delegate_states.get(wid, {})
            merged = dict(previous)
            merged.update(ev)
            self.delegate_states[wid] = merged

            event = str(ev.get("event", ""))
            target = str(ev.get("target", ""))
            label = str(ev.get("target_label") or self._delegate_label(target))
            mode = str(ev.get("mode", "read_only"))
            status = str(ev.get("status") or event)
            summary = str(ev.get("summary") or "").strip()

            if event == "START":
                # Live delegate state belongs in the compact agent dock and the
                # final WORKED details, not as another timeline toggle.
                pass

            elif event == "PROGRESS":
                # Keep the live phase in delegate_states / AGENTS dock without
                # creating a new card for every stage.
                pass

            elif event in {"DONE", "QUOTA_BLOCK", "BUDGET_BLOCK", "DEDUP_BLOCK", "CACHE_HIT", "LEASE_EXPIRED"}:
                duration = ev.get("duration_seconds")
                suffix = f" · {float(duration):.1f}s" if duration is not None else ""
                text = f"{label} · {status}{suffix} · {wid}"
                if summary:
                    text += f"\n{summary}"
                quota_before = str(ev.get("quota_before_summary") or "").strip()
                quota_summary = str(ev.get("quota_summary") or "").strip()
                if quota_before:
                    text += f"\nquota before: {quota_before}"
                if quota_summary:
                    text += f"\nquota after:  {quota_summary}"

                result_path = ev.get("result_path")
                if result_path:
                    text += f"\nresult: {result_path}"

                if event in {"QUOTA_BLOCK", "BUDGET_BLOCK", "DEDUP_BLOCK", "LEASE_EXPIRED"}:
                    self._mount_timeline_message(
                        f"DELEGATE {event}",
                        text,
                        css_class="status-message",
                    )

        if rows:
            self.update_agentdock()

    def _delegate_summary_text(self) -> str:
        if not self.delegate_states:
            return "No cross-provider delegates in this TUI session."

        rows = []
        for wid, ev in sorted(
            self.delegate_states.items(),
            key=lambda item: float(item[1].get("epoch") or 0),
            reverse=True,
        )[:20]:
            label = ev.get("target_label") or self._delegate_label(str(ev.get("target", "")))
            state = ev.get("status") or ev.get("event") or "unknown"
            mode = ev.get("mode") or "-"
            duration = ev.get("duration_seconds")
            suffix = f" · {float(duration):.1f}s" if duration is not None else ""
            quota_before = str(ev.get("quota_before_summary") or "").strip()
            quota = str(ev.get("quota_summary") or "").strip()

            block = [f"{wid} · {label} · {mode} · {state}{suffix}"]
            if quota_before:
                block.append(f"  before  {quota_before}")
            if quota:
                block.append(f"  after   {quota}")
            rows.append("\n".join(block))
        return "\n\n".join(rows)

    def _mount_timeline_message(
        self,
        role: str,
        text: str,
        *,
        css_class: str = "system-message",
    ) -> TimelineMessage:
        message = TimelineMessage(role, text, css_class=css_class)
        self.query_one("#feed", VerticalScroll).mount(message)
        return message

    def _mount_user_message(self, prompt: str) -> None:
        self._copy_messages.append(("user", prompt))
        self._mount_timeline_message(
            "YOU",
            prompt,
            css_class="user-message",
        )

    def _worked_summary(self, *, status: str | None, rc: int) -> tuple[str, str]:
        total_seconds = 0.0
        action_count = 0
        body: list[str] = []

        if self.attempts:
            body.append("MAIN ATTEMPTS")
            for a in self.attempts:
                end = a.ended or now()
                total_seconds += max(0.0, end - a.started)
                action_count += len(a.lines)
                line = f"- {a.model} · {a.elapsed()} · {len(a.lines)} events"
                if a.quota_summary:
                    line += f" · quota {a.quota_summary}"
                body.append(line)

                for kind, text in a.lines[-80:]:
                    if kind == "tool":
                        body.append(f"    · {text}")
                    elif kind == "subagent":
                        body.append(f"    ↳ {text}")
                    elif kind in {"finding", "status"}:
                        body.append(f"    {text}")

        current_job = (self.job or {}).get("id")
        delegates = []
        for wid, ev in self.delegate_states.items():
            if current_job and ev.get("parent_job_id") != current_job:
                continue
            delegates.append((wid, ev))

        if delegates:
            body.append("")
            body.append("DELEGATES")
            for wid, ev in delegates[-20:]:
                action_count += 1
                label = ev.get("target_label") or self._delegate_label(str(ev.get("target", "")))
                state = ev.get("status") or ev.get("event") or "?"
                duration = ev.get("duration_seconds")
                suffix = f" · {float(duration):.1f}s" if duration is not None else ""
                body.append(f"- {label} · {state}{suffix} · {wid}")
                before = str(ev.get("quota_before_summary") or "").strip()
                after = str(ev.get("quota_summary") or "").strip()
                if before:
                    body.append(f"    before {before}")
                if after:
                    body.append(f"    after  {after}")

        body.append("")
        body.append(f"process rc: {rc}")
        body.append(f"task status: {status or 'UNKNOWN'}")

        mins, secs = divmod(int(total_seconds), 60)
        if mins >= 60:
            hours, mins = divmod(mins, 60)
            elapsed = f"{hours}h {mins:02d}m"
        else:
            elapsed = f"{mins}m {secs:02d}s"

        title = f"WORKED · {elapsed} · {action_count} actions"
        return title, "\n".join(body).strip()

    def _mount_worked(self, *, status: str | None, rc: int) -> None:
        title, body = self._worked_summary(status=status, rc=rc)
        log = RichLog(
            wrap=True,
            markup=False,
            highlight=False,
            classes="attempt-log",
        )
        panel = Collapsible(log, title=title, collapsed=True)
        self.query_one("#feed", VerticalScroll).mount(panel)
        log.write(body or "No detailed actions recorded.")

    def _latest_result_data(self) -> dict[str, Any]:
        if not self.job_dir:
            return {}
        for path in sorted(self.job_dir.glob("run-*-result.json"), reverse=True):
            try:
                data = json.loads(path.read_text())
            except Exception:
                continue
            if isinstance(data, dict):
                return data
        return {}

    def _last_assistant_text(self) -> str:
        for role, body in reversed(self._copy_messages):
            if role == "assistant" and body.strip():
                return body.strip()
        data = self._latest_result_data()
        return str(data.get("response") or "").strip()

    def _current_user_copy_text(self) -> str:
        prompt = self._current_prompt_text().strip()
        if prompt:
            return prompt
        for role, body in reversed(self._copy_messages):
            if role == "user" and body.strip():
                return body.strip()
        return ""

    def _copy_payload(self, mode: str) -> str:
        mode = mode.lower().strip()
        if mode in {"last", "answer", "assistant"}:
            return self._last_assistant_text()
        if mode == "user":
            return self._current_user_copy_text()
        if mode == "prompt":
            return self._current_prompt_text().strip()
        if mode == "all":
            messages = list(self._copy_messages)
            if messages and not any(role == "assistant" for role, _ in messages):
                latest = self._last_assistant_text()
                if latest:
                    messages.append(("assistant", latest))
            if not messages:
                user = self._current_user_copy_text()
                answer = self._last_assistant_text()
                if user:
                    messages.append(("user", user))
                if answer:
                    messages.append(("assistant", answer))
            blocks = []
            for role, body in messages:
                label = "USER" if role == "user" else "ASSISTANT"
                blocks.append(f"{label}\n{body.strip()}")
            return "\n\n".join(blocks).strip()
        if mode == "worked":
            answer = self._last_assistant_text()
            latest = self._latest_result_data()
            rc = int(latest.get("rc", 0) or 0)
            status = str((self.job or {}).get("task_status") or latest.get("task_status") or "").upper() or None
            title, body = self._worked_summary(status=status, rc=rc)
            parts = []
            if answer:
                parts.append(answer)
            parts.append(f"--- {title} ---\n{body}")
            return "\n\n".join(parts).strip()
        return ""

    def _copy_to_clipboard(self, mode: str = "last") -> None:
        payload = self._copy_payload(mode)
        if not payload:
            self.note(f"Nothing available for /copy {mode}.", title="COPY", collapsed=False)
            return
        result = copy_text(payload)
        if not result.get("ok"):
            self.note(
                str(result.get("reason") or "clipboard copy failed"),
                title="COPY ERROR",
                collapsed=False,
            )
            return
        self.note(
            f"Copied {mode} to clipboard · {len(payload):,} chars · {result.get('backend')}",
            title="COPY",
            collapsed=False,
        )

    def _run_update_async(self, args: list[str], title: str) -> None:
        if not UPDATE_MANAGER.exists():
            self._mount_timeline_message(
                "UPDATE",
                f"Update manager missing: {UPDATE_MANAGER}",
                css_class="status-message",
            )
            return

        self._mount_timeline_message(
            "UPDATE",
            f"{title} started…",
            css_class="system-message",
        )

        def work() -> None:
            try:
                p = subprocess.run(
                    [str(BASE / "venv/bin/python"), str(UPDATE_MANAGER), *args],
                    capture_output=True,
                    text=True,
                    timeout=900,
                )
                output = (p.stdout.strip() or p.stderr.strip() or "No output.")
                self.call_from_thread(
                    self.post_message,
                    UpdateResult(title, p.returncode, output),
                )
            except Exception as e:
                self.call_from_thread(
                    self.post_message,
                    UpdateResult(title, 1, f"update command failed: {e}"),
                )

        threading.Thread(target=work, daemon=True).start()

    async def on_update_result(self, msg: UpdateResult) -> None:
        self._mount_timeline_message(
            "UPDATE",
            msg.text,
            css_class="status-message" if msg.rc == 0 else "system-message",
        )

    def compose(self) -> ComposeResult:
        yield Static("", id="brand")
        yield Static("", id="runtime")
        yield VerticalScroll(id="feed")
        yield Static("No active agents", id="agentdock")
        yield Static("", id="commandbar")
        yield Static("MESSAGE  ·  Enter send/queue  ·  Shift+Enter newline  ·  Ctrl+U clear  ·  Ctrl+Y copy  ·  / commands", id="composer-label")
        yield TextArea("", id="prompt", soft_wrap=True, show_line_numbers=False)
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(1.0, self.tick)
        self.recover()
        self.update_banner()
        self.query_one("#prompt", TextArea).focus()

    def update_banner(self) -> None:
        branch = git_branch(self.repo)
        job_state = self.job.get("status", "READY") if self.job else "READY"
        self.query_one("#brand", Static).update(
            f"  ORCHBRIDGE v{orchestrator_version()}   {job_state}   ·   {self.repo.name}   ·   {branch}"
        )

        route = pretty_route(self.route) if self.route else "AUTO"
        active_scope = (
            str(self.job.get("scope_mode"))
            if self.job and self.job.get("scope_mode")
            else self.scope_mode
        )
        prompt_sha = (
            str(self.job.get("prompt_sha256", ""))[:8]
            if self.job
            else "-"
        )
        mode = (
            f"router {'on' if self.router_enabled else 'off'}"
            f" · model {self.model_override}"
            f" · scope {active_scope}"
            f" · prompt {prompt_sha}"
        )
        self.query_one("#runtime", Static).update(
            f"  {route}   ·   {mode}   ·   detail {self.verbosity}   ·   {self._cmd_quota_summary}"
        )

    def _main_lease_path(self, job_dir: Path | None = None) -> Path | None:
        d = job_dir or self.job_dir
        return (d / "main-worker-lease.json") if d else None

    def _runtime_for_job(
        self,
        job: dict[str, Any] | None = None,
        job_dir: Path | None = None,
        *,
        adopt_legacy: bool = True,
    ) -> dict[str, Any]:
        job = job or self.job
        job_dir = job_dir or self.job_dir
        if not job or not job_dir:
            return {"status": "NO_JOB"}

        lease_path = self._main_lease_path(job_dir)
        assert lease_path is not None
        lease = runtime_load_json(lease_path)

        if not lease and adopt_legacy and str(job.get("status") or "") == "RUNNING":
            found = discover_legacy_main_worker(job_dir, Path(str(job.get("repo") or self.repo)))
            if found:
                run_number = int(job.get("attempt") or 0)
                result_file = str(found.get("result_file") or "")
                lease = write_main_lease(
                    lease_path,
                    job_id=str(job.get("id") or job_dir.name),
                    repo=str(job.get("repo") or self.repo),
                    pid=int(found["pid"]),
                    pgid=int(found.get("pgid") or found["pid"]),
                    run_number=run_number,
                    prompt_sha256=str(job.get("prompt_sha256") or ""),
                    result_file=result_file,
                    state="ADOPTED_LEGACY",
                )
                job["worker"] = {
                    "pid": int(found["pid"]),
                    "pgid": int(found.get("pgid") or found["pid"]),
                    "run_number": run_number,
                    "result_file": result_file,
                    "state": "ADOPTED_LEGACY",
                    "adopted_at": iso(),
                }
                if job is self.job:
                    self.save()
                else:
                    atomic_json(job_dir / "job.json", job)

        if not lease:
            return {"status": "NO_LEASE"}

        return classify_main_worker(
            lease,
            expected_job_id=str(job.get("id") or job_dir.name),
            expected_repo=str(job.get("repo") or self.repo),
        )

    def _live_main_worker(self) -> dict[str, Any] | None:
        if self.proc is not None and self.proc.poll() is None:
            return {
                "status": "LIVE_OWNED",
                "pid": self.proc.pid,
                "pgid": process_pgid(self.proc.pid) or self.proc.pid,
                "source": "local-handle",
            }
        state = self._runtime_for_job()
        if state.get("status") in {"LIVE_OWNED", "LIVE_UNKNOWN"}:
            return state
        return None

    def _scan_live_main_workers(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for d in iter_known_job_dirs():
            p = d / "job.json"
            try:
                job = json.loads(p.read_text())
            except Exception:
                continue
            if str(job.get("status") or "") != "RUNNING":
                continue
            repo = _resolved_path(job.get("repo"))
            if repo is None:
                continue
            lease = runtime_load_json(d / "main-worker-lease.json")
            state: dict[str, Any]
            if lease:
                state = classify_main_worker(
                    lease,
                    expected_job_id=str(job.get("id") or d.name),
                    expected_repo=str(repo),
                )
            else:
                found = discover_legacy_main_worker(d, repo)
                if not found:
                    continue
                state = {
                    "status": "LIVE_OWNED",
                    "pid": int(found.get("pid") or 0),
                    "pgid": int(found.get("pgid") or found.get("pid") or 0),
                    "source": "legacy-discovery",
                }
            if state.get("status") not in {"LIVE_OWNED", "LIVE_UNKNOWN"}:
                continue
            rows.append({
                "job_id": str(job.get("id") or d.name),
                "job_dir": str(d.resolve()),
                "repo": str(repo),
                "workspace_id": job.get("workspace_id"),
                "project_base": job.get("project_base"),
                **state,
            })
        return rows

    def _foreign_live_same_repo_worker(self) -> dict[str, Any] | None:
        current_dir = str(self.job_dir.resolve()) if self.job_dir else None
        current_repo = _canonical_repo(self.repo)
        for row in self._scan_live_main_workers():
            try:
                row_repo = _canonical_repo(Path(str(row.get("repo") or "")))
            except Exception:
                continue
            if row_repo != current_repo:
                continue
            if current_dir and str(row.get("job_dir") or "") == current_dir:
                continue
            return row
        return None

    def _mark_main_worker_finished(self, rc: int) -> None:
        if not self.job:
            return
        worker = self.job.get("worker") if isinstance(self.job.get("worker"), dict) else {}
        worker = dict(worker)
        worker.update({"state": "EXITED", "exit_code": int(rc), "ended_at": iso()})
        self.job["worker"] = worker
        self.save()

    def _terminate_main_worker(self) -> tuple[bool, str]:
        if self.proc is not None and self.proc.poll() is None:
            pid = self.proc.pid
            pgid = process_pgid(pid) or pid
            try:
                os.killpg(pgid, signal.SIGTERM)
                return True, f"terminated MAIN process group {pgid}"
            except Exception:
                try:
                    self.proc.terminate()
                    return True, f"terminated MAIN pid {pid}"
                except Exception as e:
                    return False, f"failed to terminate MAIN pid {pid}: {e}"

        state = self._runtime_for_job()
        if state.get("status") == "LIVE_UNKNOWN":
            return False, "worker is alive but ownership could not be verified; refusing to signal it"
        if state.get("status") != "LIVE_OWNED":
            return False, "no verified live MAIN worker"

        pid = int(state.get("pid") or 0)
        pgid = int(state.get("pgid") or pid)
        try:
            os.killpg(pgid, signal.SIGTERM)
            return True, f"terminated recovered MAIN process group {pgid}"
        except Exception as e:
            return False, f"failed to terminate recovered MAIN worker: {e}"

    def _poll_recovered_main_worker(self) -> None:
        if now() - self._last_main_recovery_poll < 2:
            return
        self._last_main_recovery_poll = now()

        if not self.job or not self.job_dir or self.proc is not None:
            return
        if str(self.job.get("status") or "") != "RUNNING":
            return

        state = self._runtime_for_job()
        if state.get("status") in {"LIVE_OWNED", "LIVE_UNKNOWN"}:
            self._proc_diag = (
                f"recovered worker alive · pid {state.get('pid')} · "
                f"{state.get('status')}"
            )
            return

        if self._recovered_main_result_posted:
            return

        result_file = str(state.get("result_file") or "")
        if not result_file:
            worker = self.job.get("worker") if isinstance(self.job.get("worker"), dict) else {}
            result_file = str(worker.get("result_file") or "")
        result_path = Path(result_file) if result_file else None

        if result_path and result_path.exists():
            try:
                data = json.loads(result_path.read_text())
                rc = int(data.get("rc", 1))
            except Exception:
                rc = int(runtime_load_json(self._main_lease_path() or Path("/nonexistent")).get("exit_code") or 1)
            self._recovered_main_result_posted = True
            self.post_message(Done(rc, result_path))
            return

        self.job.update({"status": "PAUSED_RETRY", "resume_at": now() + 15})
        self.save()
        self.note(
            "Recovered MAIN worker is no longer alive, but no result file was found. "
            "The job was moved to PAUSED_RETRY and will retry in 15s.",
            title="RECOVERY",
            collapsed=False,
        )

    def recover(self) -> None:
        for d in sorted(JOBS_DIR.glob("job-*"), key=lambda p: p.stat().st_mtime, reverse=True):
            p = d / "job.json"
            if not p.exists():
                continue
            try:
                j = json.loads(p.read_text())
            except Exception:
                continue

            if j.get("status") not in {
                "RUNNING",
                "PAUSED_QUOTA",
                "PAUSED_RETRY",
                "PAUSED_USER",
            }:
                continue
            if not job_belongs_to_context(
                j,
                current_repo=self.repo,
                workspace_id=WORKSPACE_ID,
                project_base=PROJECT_BASE,
            ):
                continue

            self.job_dir, self.job = d, j
            self._recovered_main_result_posted = False
            changed = False
            recovery_detail = ""

            if j["status"] == "RUNNING":
                runtime = self._runtime_for_job(j, d, adopt_legacy=True)
                state = str(runtime.get("status") or "")
                if state in {"LIVE_OWNED", "LIVE_UNKNOWN"}:
                    recovery_detail = (
                        f"\nMAIN worker still alive · pid {runtime.get('pid')} · {state}. "
                        "Automatic duplicate resume is disabled; this TUI will watch the persisted result."
                    )
                    self._proc_diag = f"recovered worker alive · pid {runtime.get('pid')} · {state}"
                else:
                    j["status"] = "PAUSED_RETRY"
                    j["resume_at"] = now() + 15
                    self._recovery_grace_until = float(j["resume_at"])
                    recovery_detail = f"\nNo owned live MAIN worker found ({state or 'unknown'}); retry grace 15s."
                    changed = True

            if j["status"] in {"PAUSED_QUOTA", "PAUSED_RETRY"}:
                resume_at = float(j.get("resume_at") or 0)
                if resume_at <= now():
                    j["resume_at"] = now() + 15
                    self._recovery_grace_until = float(j["resume_at"])
                    changed = True

            if changed:
                self.save()

            try:
                recovered_prompt = (d / "original-prompt.md").read_text()
            except Exception:
                recovered_prompt = ""
            if recovered_prompt:
                self._mount_user_message(recovered_prompt)

            msg = f"Recovered {j['id']} · {j['status']} · {j.get('title','task')}" + recovery_detail
            if self._recovery_grace_until and j["status"] != "RUNNING":
                msg += (
                    "\nStartup recovery grace: 15s. "
                    "A new prompt will not be allowed while a verified old MAIN worker is alive."
                )

            self.note(msg, title="SESSION", collapsed=False)
            break

    def save(self) -> None:
        if self.job_dir and self.job:
            self.job["updated_at"] = iso()
            atomic_json(self.job_dir / "job.json", self.job)

    def event(self, kind: str, text: str) -> None:
        if self.job_dir:
            append_jsonl(
                self.job_dir / "events.jsonl",
                {"ts": iso(), "kind": kind, "text": text},
            )

    def checkpoint(self) -> None:
        if not self.job_dir:
            return
        p = self.job_dir / "events.jsonl"
        rows: list[str] = []
        seen: set[str] = set()
        if p.exists():
            for line in p.read_text().splitlines()[-1200:]:
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if ev.get("kind") not in {"progress", "finding", "status"}:
                    continue
                text = str(ev.get("text", "")).strip()
                if text and text not in seen:
                    seen.add(text)
                    rows.append(text)

        body = (
            "# Visible-progress checkpoint\n\n"
            "Navigation context only; freshness-sensitive facts must be reverified.\n\n"
            + "\n".join(f"- {x}" for x in rows[-100:])
            + "\n"
        )
        (self.job_dir / "checkpoint.md").write_text(body)

    def _fresh_repo_facts_context(self) -> str:
        if str((self.job or {}).get("scope_mode", self.scope_mode)) == "strict":
            return ""
        py = BASE / "venv/bin/python"
        if not py.exists():
            return ""
        try:
            q = subprocess.run(
                [
                    str(py), str(APP_DIR / "phase1_supervisor.py"),
                    "facts", "--repo", str(self.repo), "--status", "VERIFIED",
                    "--fresh-only", "--limit", "12",
                ],
                capture_output=True, text=True, timeout=10,
                env={**os.environ.copy(), "AI_ORCH_PROJECT_BASE": str(PROJECT_BASE), "AI_ORCH_REPO": str(self.repo)},
            )
            if q.returncode != 0 or not q.stdout.strip():
                return ""
            rows = json.loads(q.stdout)
        except Exception:
            return ""
        if not isinstance(rows, list):
            return ""
        claims: list[str] = []
        seen: set[str] = set()
        for row in reversed(rows):
            if not isinstance(row, dict) or row.get("_fresh") is not True:
                continue
            claim = str(row.get("claim") or "").strip()
            if (
                not claim
                or claim.startswith("delegate main checkout unchanged")
                or claim.startswith("delegate actual changed files")
                or claim in seen
            ):
                continue
            seen.add(claim)
            source = str(row.get("source_type") or "fact")
            claims.append(f"- [{source}] {claim}")
            if len(claims) >= 8:
                break
        if not claims:
            return ""
        claims.reverse()
        return (
            "REPO FACT CACHE (mechanically verified and fresh for the current Git/worktree identity; "
            "reverify if the task is safety-critical):\n" + "\n".join(claims)
        )

    def task_text(self, resume: bool) -> tuple[str, str]:
        assert self.job_dir
        original = (self.job_dir / "original-prompt.md").read_text()
        context = ""
        try:
            pctx = subprocess.run(
                [str(HOME / ".local/bin/orch-context"), "build", "--job", str(self.job.get("id"))],
                capture_output=True, text=True, timeout=15,
                env={**os.environ.copy(), "AI_ORCH_PROJECT_BASE": str(PROJECT_BASE), "AI_ORCH_REPO": str(self.repo), "AI_ORCH_JOB_ID": str(self.job.get("id"))},
            )
            if pctx.returncode == 0:
                context = pctx.stdout.strip()
        except Exception:
            context = ""
        repo_facts = self._fresh_repo_facts_context()
        effective_original = original
        if context:
            effective_original += "\n\n--- ORCHESTRATOR TASK CONTEXT ---\n" + context
        if repo_facts:
            effective_original += "\n\n--- FRESH REPO FACTS ---\n" + repo_facts
        steer_path = self.job_dir / "steer-history.jsonl"
        if steer_path.exists():
            rows: list[str] = []
            for line in steer_path.read_text().splitlines()[-20:]:
                try:
                    item = json.loads(line)
                    instruction = str(item.get("instruction") or "").strip()
                    if instruction:
                        rows.append(f"- {instruction}")
                except Exception:
                    continue
            if rows:
                effective_original += (
                    "\n\n--- STEERING INSTRUCTIONS ---\n"
                    "These are authoritative updates to the SAME task. "
                    "The latest instruction overrides earlier conflicting guidance, "
                    "but all original safety/governance gates remain in force.\n"
                    + "\n".join(rows)
                )
        if not resume:
            return effective_original, original

        self.checkpoint()
        cp = (self.job_dir / "checkpoint.md").read_text()
        wrapped = f"""Continue the SAME task from this persisted checkpoint.

RESUME RULES:
- Do not restart the whole investigation from scratch.
- Checkpoint text is navigation context, not authoritative evidence.
- Reverify freshness-sensitive facts: current Git HEAD/tree/dirty state,
  protected-artifact hashes, credentials, AWS/S3 state, and evidence identity.
- Avoid repeating static code analysis unless state changed or a contradiction requires it.
- Continue pending work and preserve all original governance/safety rules.
- Stop with ORCH_STATUS: NEEDS_GO at a human GO gate.

--- CHECKPOINT ---
{cp}
--- ORIGINAL TASK ---
{effective_original}
"""
        return wrapped, original

    def new_job(
        self,
        prompt: str,
        *,
        attachments: list[str] | None = None,
        queue_entry_id: str | None = None,
    ) -> None:
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        d = JOBS_DIR / f"job-{stamp}"
        d.mkdir(parents=True)
        (d / "original-prompt.md").write_text(prompt)
        (d / "checkpoint.md").write_text("")

        scope_mode, max_delegates = self._parse_job_directives(prompt)
        prompt_sha = self._prompt_sha(prompt)
        attachments = list(self.pending_attachments) if attachments is None else list(attachments)
        try:
            pskill = subprocess.run(
                [str(HOME / ".local/bin/orch-skills"), "select", "--text", prompt, "--limit", "2", "--json"],
                capture_output=True, text=True, timeout=10,
                env={**os.environ.copy(), "AI_ORCH_PROJECT_BASE": str(PROJECT_BASE), "AI_ORCH_REPO": str(self.repo)},
            )
            skills = json.loads(pskill.stdout) if pskill.returncode == 0 else []
        except Exception:
            skills = []

        self.job_dir = d
        self.job = {
            "schema_version": 2,
            "id": d.name,
            "title": (prompt.splitlines()[0] if prompt else "task")[:100],
            "repo": str(self.repo),
            "workspace_id": os.getenv("AI_ORCH_WORKSPACE_ID"),
            "project_base": str(PROJECT_BASE),
            "branch_at_start": git_branch(self.repo),
            "head_at_start": git_head(self.repo),
            "created_at": iso(),
            "updated_at": iso(),
            "status": "READY",
            "resume_at": None,
            "attempt": 0,
            "task_status": None,
            "scope_mode": scope_mode,
            "permission_profile": self.permission_profile,
            "max_delegates": max_delegates,
            "prompt_sha256": prompt_sha,
            "prompt_preview": prompt[:240],
            "attachments": attachments,
            "skills": skills,
            "queue_entry_id": queue_entry_id,
        }
        self.save()
        self.raw.clear()
        self.attempts.clear()
        self.current = None
        self.delegate_states.clear()
        self._mount_user_message(prompt)
        self.start(False)
        self.pending_attachments.clear()

    def start(self, resume: bool) -> None:
        if not self.job or not self.job_dir:
            return
        if self.proc and self.proc.poll() is None:
            self.note("A job is already running.", title="NOTICE")
            return

        live = self._live_main_worker()
        if live:
            self.note(
                f"Refusing duplicate MAIN launch: persisted worker is still alive "
                f"(pid {live.get('pid')} · {live.get('status')}).",
                title="RECOVERY GUARD",
                collapsed=False,
            )
            return

        foreign_same_repo = self._foreign_live_same_repo_worker()
        if foreign_same_repo:
            self.note(
                "Refusing duplicate MAIN launch for the same repository: another scope/job "
                f"is still alive (job {foreign_same_repo.get('job_id')} · "
                f"pid {foreign_same_repo.get('pid')} · {foreign_same_repo.get('status')}).",
                title="REPO OWNERSHIP GUARD",
                collapsed=False,
            )
            return

        task, raw_user = self.task_text(resume)
        n = int(self.job.get("attempt", 0)) + 1
        self.job.update(
            {"attempt": n, "status": "RUNNING", "resume_at": None, "task_status": None}
        )
        self.save()

        taskf = self.job_dir / f"run-{n:03d}-task.md"
        rawf = self.job_dir / f"run-{n:03d}-raw.md"
        statef = self.job_dir / f"run-{n:03d}-state.json"
        resultf = self.job_dir / f"run-{n:03d}-result.json"
        leasef = self.job_dir / "main-worker-lease.json"
        taskf.write_text(task)
        rawf.write_text(raw_user)
        atomic_json(statef, self._current_runtime_state())

        argv = [
            str(BASE / "venv/bin/python"),
            str(WORKER),
            "--repo", str(self.repo),
            "--task-file", str(taskf),
            "--raw-file", str(rawf),
            "--state-file", str(statef),
            "--result-file", str(resultf),
            "--lease-file", str(leasef),
            "--job-id", str(self.job["id"]),
            "--run-number", str(n),
            "--prompt-sha", str(self.job.get("prompt_sha256", "")),
        ]

        worker_env = os.environ.copy()
        worker_env["AI_ORCH_JOB_ID"] = str(self.job["id"])
        worker_env["AI_ORCH_REPO"] = str(self.repo)
        worker_env["AI_ORCH_PROJECT_BASE"] = str(PROJECT_BASE)
        worker_env["AI_ORCH_DELEGATION_DEPTH"] = "0"
        worker_env["AI_ORCH_PROMPT_SHA256"] = str(self.job.get("prompt_sha256", ""))
        worker_env["AI_ORCH_MAX_DELEGATES_PER_JOB"] = str(self.job.get("max_delegates", 3))
        worker_env["AI_ORCH_PERMISSION_PROFILE"] = str(self.job.get("permission_profile", self.permission_profile))
        if str(self.job.get("scope_mode", self.scope_mode)) == "strict":
            worker_env["AI_ORCH_STRICT_TASK"] = "1"
        else:
            worker_env.pop("AI_ORCH_STRICT_TASK", None)

        self._last_main_output_at = now()
        self._last_proc_diag_at = 0.0
        self._proc_diag = "starting"
        self._stall_notice_level = 0
        self._recovered_main_result_posted = False

        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=worker_env,
        )
        self.proc = proc
        pgid = process_pgid(proc.pid) or proc.pid
        self.job["worker"] = {
            "pid": proc.pid,
            "pgid": pgid,
            "run_number": n,
            "result_file": str(resultf.resolve()),
            "lease_file": str(leasef.resolve()),
            "state": "RUNNING",
            "started_at": iso(),
        }
        self.save()

        self.raw.clear()
        scope = self.job.get("scope_mode", self.scope_mode)
        psha = str(self.job.get("prompt_sha256", ""))[:8]
        max_del = self.job.get("max_delegates", 3)
        self.event(
            "status",
            (
                ("Resuming" if resume else "Started")
                + f" {self.job['id']} · run {n}"
                + f" · pid {proc.pid}"
                + f" · prompt {psha}"
                + f" · scope {scope}"
                + f" · delegates≤{max_del}"
            ),
        )

        def pump(bound_proc: subprocess.Popen[str] = proc, bound_result: Path = resultf) -> None:
            assert bound_proc.stdout
            for line in bound_proc.stdout:
                self.call_from_thread(self.post_message, Line(clean(line)))
            rc = bound_proc.wait()
            self.call_from_thread(self.post_message, Done(rc, bound_result))

        threading.Thread(target=pump, daemon=True, name=f"orch-main-pump-{n}").start()

    async def on_line(self, msg: Line) -> None:
        line = msg.text
        if not line:
            return

        self.raw.append(line)
        self._last_main_output_at = now()
        self._stall_notice_level = 0
        if self.current and self.current.ended is None:
            self.current.last_event_at = now()

        if self.job_dir:
            append_jsonl(
                self.job_dir / "raw-events.jsonl",
                {"ts": iso(), "line": line},
            )

        if m := ATTEMPT_RE.match(line):
            self.begin_attempt(m.group(1))
            return

        if m := TASK_STATUS_RE.match(line):
            self.task_status = m.group(1).upper()
            self.event("status", f"Task status: {self.task_status}")
            return

        if m := ROUTE_RE.match(line):
            self.route = m.group(1)
            self.update_banner()
            return

        if m := QUOTA_RE.match(line):
            self.quota = m.group(1)
            return

        if m := MODEL_TOOL_RE.match(line):
            self.add_line("tool", f"{m.group(1)} tool · {m.group(2)}")
            return

        if m := SUBAGENT_RE.match(line):
            self.add_line("subagent", f"{m.group(1)} subagent · {m.group(2)}")
            return

        if m := MODEL_PROGRESS_RE.match(line):
            label, text = m.groups()
            kind = (
                "finding"
                if any(
                    x in text
                    for x in (
                        "확정",
                        "발견",
                        "확인 완료",
                        "PASS",
                        "FAIL",
                        "READY_FOR_GO",
                        "CRITICAL",
                    )
                )
                else "progress"
            )
            self.add_line(kind, f"{label} · {text}")
            return

        low = line.lower()
        if any(
            x in low
            for x in (
                "failed:",
                "cooldown",
                "blocked until reset",
                "usage limit",
                "network issue",
                "error:",
            )
        ):
            self.add_line("status", line.replace("[ai-orch] ", "", 1))
        elif self.verbosity == "trace":
            self.add_line("trace", line)

    async def on_done(self, msg: Done) -> None:
        self.finish_attempt()

        result: dict[str, Any] = {}
        if msg.result.exists():
            try:
                result = json.loads(msg.result.read_text())
            except Exception as e:
                self.note(f"Result read error: {e}", title="ERROR")

        response = str(result.get("response") or "")
        reported_status = str(result.get("task_status") or self.task_status or "").upper() or None
        rc = int(result.get("rc", msg.rc))
        status = _validated_task_status(reported_status, rc)
        combined = "\n".join(self.raw[-1200:]) + "\n" + str(result.get("stderr_text") or "")
        if rc != 0 and reported_status == "COMPLETE":
            self.note(
                f"Ignoring reported COMPLETE because ai-orch exited with rc={rc}.",
                title="RUNTIME FAILURE",
                collapsed=False,
            )
        self.proc = None
        self._mark_main_worker_finished(rc)
        self._recovered_main_result_posted = False

        if response:
            self._mount_timeline_message(
                "ASSISTANT",
                response,
                css_class="assistant-message",
            )
            self._copy_messages.append(("assistant", response))

        self._mount_worked(status=status, rc=rc)

        assert self.job
        self.job["branch_at_end"] = git_branch(self.repo)
        self.job["head_at_end"] = git_head(self.repo)
        requested_state = str(self.job.get("status") or "")
        self.job["task_status"] = status
        if requested_state in {"PAUSED_USER", "CANCELLED"}:
            # A user pause/cancel is authoritative even if the terminated worker
            # races to produce a final result while SIGTERM is being delivered.
            self.job["status"] = requested_state
            if requested_state == "PAUSED_USER" and self.job.get("steer_pending"):
                # A steer intentionally terminates the old MAIN. Resume only after
                # its late result has been consumed so it cannot overwrite the steer.
                self.job["steer_pending"] = False
                self.job["status"] = "PAUSED_RETRY"
                self.job["resume_at"] = now() + 0.25
            self.save()
            self.update_banner()
            if self.job["status"] == requested_state:
                self._maybe_notify_terminal(requested_state, result, rc)
            return

        if status in {"COMPLETE", "FAILED", "NEEDS_GO", "NEEDS_USER", "BLOCKED"}:
            self.job["status"] = status
            self.save()
            self._mount_timeline_message(
                "STATUS",
                f"{status}",
                css_class="status-message",
            )
            self.update_banner()
            self._maybe_notify_terminal(status, result, rc)
            return

        if RATE_HINT_RE.search(combined):
            candidates = [parse_reset(x) for x in combined.splitlines()]
            candidates = [x for x in candidates if x and x > now() - 60]
            reset = min(candidates) if candidates else now() + 3600
            self.job.update({"status": "PAUSED_QUOTA", "resume_at": reset + 15})
            self.save()
            self.checkpoint()
            self._mount_timeline_message(
                "STATUS",
                f"PAUSED_QUOTA · auto-resume in {countdown(self.job['resume_at'])}",
                css_class="status-message",
            )
            self.update_banner()
            return

        if NETWORK_HINT_RE.search(combined) or rc != 0:
            self.job.update({"status": "PAUSED_RETRY", "resume_at": now() + 300})
            self.save()
            self.checkpoint()
            self._mount_timeline_message(
                "STATUS",
                "PAUSED_RETRY · temporary failure · retry in 5m",
                css_class="status-message",
            )
            self.update_banner()
            return

        self.job.update({"status": "PAUSED_RETRY", "resume_at": now() + 60})
        self.save()
        self.checkpoint()
        self._mount_timeline_message(
            "STATUS",
            "PAUSED_RETRY · no terminal status · continuation in 1m",
            css_class="status-message",
        )
        self.update_banner()

    def begin_attempt(self, model: str) -> None:
        self.finish_attempt()
        model = pretty_model_name(model)

        log = RichLog(
            wrap=True,
            markup=False,
            highlight=False,
            classes="attempt-log",
        )
        panel = Collapsible(
            log,
            title=f"THINKING · {model} · RUNNING",
            collapsed=True,
        )
        self.query_one("#feed", VerticalScroll).mount(panel)

        self.current = Attempt(model=model, panel=panel, log=log)
        self.attempts.append(self.current)
        self.event("status", f"Trying MAIN: {model}")
        self.update_agentdock()

    def finish_attempt(self) -> None:
        if self.current and self.current.ended is None:
            self.current.ended = now()
            self.current.state = "DONE"
            if self.current.panel:
                self.current.panel.title = (
                    f"THINKING · {self.current.model} · {self.current.elapsed()} "
                    f"· {len(self.current.lines)} events"
                )
                self.current.panel.collapsed = True

            finished = self.current
            self._refresh_attempt_quota_async(finished)
            self.update_agentdock()

    def visible(self, kind: str) -> bool:
        if self.verbosity == "trace":
            return True
        if self.verbosity == "verbose":
            return kind != "trace"
        return kind in {"progress", "finding", "status", "subagent"}

    def add_line(self, kind: str, text: str) -> None:
        self.event(kind, text)
        if not self.current:
            if kind in {"status", "finding"}:
                self.note(text, title="STATUS")
            return

        self.current.lines.append((kind, text))
        self.current.last_event_at = now()
        if self.current.log and self.visible(kind):
            if kind == "tool" and self.verbosity == "verbose":
                text = re.sub(r":\s*\{.*$", "", text) + " …"
            prefix = {
                "finding": "◆ ",
                "progress": "  ",
                "status": "! ",
                "subagent": "↳ ",
                "tool": "· ",
                "trace": "· ",
            }.get(kind, "")
            self.current.log.write(prefix + text)

        self.update_agentdock()

    def note(
        self,
        text: str,
        title: str = "SYSTEM",
        collapsed: bool = True,
    ) -> None:
        detail_titles = {
            "HELP",
            "HISTORY",
            "JOB",
            "SESSION",
            "CURRENT PROMPT",
            "QUOTA",
            "DECISION",
            "AGENTS",
            "WORKERS",
            "DELEGATES",
            "FACT LEDGER",
            "REVIEW DRAFT",
            "PROVIDER HEALTH",
            "LEASE RECOVERY",
            "DIAGNOSTICS",
            "SKILLS",
            "MCP",
            "COMPACT",
            "QUEUE",
            "QUEUE NOTICE",
            "QUEUE ERROR",
            "QUEUE HALTED",
            "QUEUE PAUSED",
            "QUEUE START",
            "DOCTOR",
            "STEER",
            "STEER WARNING",
            "RUNTIME FAILURE",
        }

        if title in detail_titles:
            log = RichLog(
                wrap=True,
                markup=False,
                highlight=False,
                classes="attempt-log",
            )
            panel = Collapsible(log, title=title, collapsed=collapsed)
            self.query_one("#feed", VerticalScroll).mount(panel)
            log.write(text)
            return

        css = "status-message" if title in {
            "STATUS", "PAUSED", "NOTICE", "ERROR", "CANCELLED",
            "PROMPT PRESERVED", "ROUTER", "MODEL", "SCOPE", "DISPLAY",
        } else "system-message"
        self._mount_timeline_message(title, text, css_class=css)

    def rerender(self) -> None:
        for attempt in self.attempts:
            if not attempt.log:
                continue
            attempt.log.clear()
            for kind, text in attempt.lines:
                if self.visible(kind):
                    if kind == "tool" and self.verbosity == "verbose":
                        text = re.sub(r":\s*\{.*$", "", text) + " …"
                    prefix = {
                        "finding": "◆ ",
                        "progress": "  ",
                        "status": "! ",
                        "subagent": "↳ ",
                        "tool": "· ",
                        "trace": "· ",
                    }.get(kind, "")
                    attempt.log.write(prefix + text)

    def update_agentdock(self) -> None:
        dock = self.query_one("#agentdock", Static)

        main = None
        if self.attempts:
            a = self.attempts[-1]
            if a.ended is None:
                quiet = now() - a.last_event_at
                if quiet >= 300:
                    state = f"SUSPECT {self._format_age(quiet)}"
                elif quiet >= 60:
                    state = f"QUIET {self._format_age(quiet)}"
                else:
                    state = "RUN"
            else:
                state = "done"
            main = f"MAIN {a.model} · {state} · {a.elapsed()}"

        current_job = (self.job or {}).get("id")
        delegates = [
            ev
            for ev in self.delegate_states.values()
            if not current_job or ev.get("parent_job_id") == current_job
        ]
        active_states = {"RUNNING", "WORKTREE_READY", "PROVIDER_RUNNING", "VERIFYING"}
        active = sum(
            1
            for ev in delegates
            if str(ev.get("status") or ev.get("event") or "") in active_states
        )
        complete = sum(
            1
            for ev in delegates
            if str(ev.get("status") or ev.get("event") or "") == "COMPLETE"
        )

        parts = []
        if main:
            parts.append(main)
        if delegates:
            parts.append(f"delegates {active} active / {complete} complete / {len(delegates)} total")
        if self.attempts and self.attempts[-1].quota_summary and self.attempts[-1].ended is not None:
            parts.append(f"quota {self.attempts[-1].quota_summary}")

        dock.update("  " + "   ·   ".join(parts) if parts else "  AGENTS idle")

    def refresh_cached_quota(self) -> None:
        if now() - self._last_quota_refresh < 5:
            return
        self._last_quota_refresh = now()
        self._cmd_quota_summary = cached_cmd_quota()

    def tick(self) -> None:
        self.refresh_cached_quota()
        self._poll_delegate_events()
        self._refresh_process_diag()
        self._poll_recovered_main_worker()
        self._queue_tick()

        if self.current and self.current.ended is None and self.proc and self.proc.poll() is None:
            quiet = now() - self.current.last_event_at
            if quiet >= 300 and self._stall_notice_level < 2:
                self._stall_notice_level = 2
                self.note(
                    "No new model stream event for "
                    f"{self._format_age(quiet)}.\n"
                    f"{self._proc_diag}\n"
                    "The watchdog will not kill it automatically. Use /diag before deciding to cancel.",
                    title="STALL WATCH",
                    collapsed=False,
                )
            elif quiet >= 60 and self._stall_notice_level < 1:
                self._stall_notice_level = 1

        if self.job:
            status = self.job.get("status", "READY")
            if status in {"PAUSED_QUOTA", "PAUSED_RETRY"}:
                resume_at = float(self.job.get("resume_at") or 0)
                if resume_at and resume_at <= now():
                    self.resume_job()

        self.update_banner()
        self.update_agentdock()

    def resume_job(self, source: str = "auto") -> None:
        if not self.job:
            if source == "manual":
                self.note("No resumable job is loaded.", title="RESUME", collapsed=False)
            return

        if self.job_dir:
            disk_job = self.job_dir / "job.json"
            if disk_job.exists():
                try:
                    latest = json.loads(disk_job.read_text())
                    if latest.get("id") == self.job.get("id"):
                        self.job.update(latest)
                except Exception as e:
                    if source == "manual":
                        self.note(
                            f"Could not refresh job.json before resume: {e}",
                            title="RESUME WARNING",
                            collapsed=False,
                        )

        if self.proc is not None:
            rc = self.proc.poll()
            if rc is None:
                if source == "manual":
                    self.note(
                        f"Worker is already running (pid {self.proc.pid}). "
                        "Resume was not started twice.",
                        title="RESUME",
                        collapsed=False,
                    )
                return
            self.proc = None

        persisted = self._live_main_worker()
        if persisted:
            if source == "manual":
                self.note(
                    f"Persisted MAIN worker is still alive (pid {persisted.get('pid')} · "
                    f"{persisted.get('status')}); duplicate resume blocked.",
                    title="RESUME",
                    collapsed=False,
                )
            return

        status = str(self.job.get("status") or "")
        if status in {
            "COMPLETE",
            "NEEDS_GO",
            "NEEDS_USER",
            "BLOCKED",
            "CANCELLED",
        }:
            if source == "manual":
                self.note(
                    f"Job is {status}; it is not resumable.",
                    title="RESUME",
                    collapsed=False,
                )
            return

        if source == "manual":
            next_run = int(self.job.get("attempt", 0)) + 1
            self.note(
                f"Manual resume accepted · {self.job.get('id')} · launching run {next_run}",
                title="RESUME",
                collapsed=False,
            )

        try:
            self.start(True)
        except Exception as e:
            self.job.update(
                {
                    "status": "PAUSED_RETRY",
                    "resume_at": now() + 300,
                }
            )
            self.save()
            self.note(
                f"Resume launch failed before worker start: {type(e).__name__}: {e}\n"
                "Job returned to PAUSED_RETRY; retry in 5m.",
                title="RESUME ERROR",
                collapsed=False,
            )
            self.update_banner()

    def _run_json_tool(self, argv: list[str], timeout: int = 30):
        env = os.environ.copy()
        env["AI_ORCH_REPO"] = str(self.repo)
        env["AI_ORCH_PROJECT_BASE"] = str(PROJECT_BASE)
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout).strip() or f"exit={p.returncode}")
        return json.loads(p.stdout)

    def _remember_attachment_items(self, items) -> None:
        added = []
        for item in items or []:
            aid = str(item.get("id") or "")
            if aid and aid not in self.pending_attachments:
                self.pending_attachments.append(aid)
                added.append(item)
        if added:
            body = "\n".join(
                f"{x.get('id')} · {x.get('kind')} · {x.get('name')} · {x.get('size')} bytes"
                for x in added
            )
            self.note(body, title="ATTACHED", collapsed=False)

    def action_attach(self) -> None:
        try:
            items = self._run_json_tool([str(HOME / ".local/bin/orch-attachments"), "pick", "--json"], timeout=300)
            self._remember_attachment_items(items)
        except Exception as e:
            self.note(f"Attach failed: {e}", title="ATTACH ERROR", collapsed=False)

    def _registered_project_completion_names(self) -> list[str]:
        try:
            data = json.loads(WORKSPACE_REGISTRY.read_text())
            rows = data.get("workspaces", {}) if isinstance(data, dict) else {}
        except Exception:
            rows = {}
        values: list[tuple[int, str]] = []
        for item in rows.values() if isinstance(rows, dict) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("display_name") or item.get("name") or "").strip()
            if not name:
                continue
            try:
                idx = int(item.get("window_index", 9999))
            except Exception:
                idx = 9999
            values.append((idx, name))
        out: list[str] = []
        seen: set[str] = set()
        for _idx, name in sorted(values, key=lambda x: (x[0], x[1].casefold())):
            key = name.casefold()
            if key not in seen:
                seen.add(key)
                out.append(name)
        return out

    def _branch_completion_names(self) -> list[str]:
        try:
            q = subprocess.run(
                ["git", "branch", "--format=%(refname:short)"],
                cwd=self.repo,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if q.returncode != 0:
                return []
            return sorted(
                {x.strip() for x in q.stdout.splitlines() if x.strip()},
                key=str.casefold,
            )
        except Exception:
            return []

    @staticmethod
    def _argument_completion_rows(
        base: str, partial: str, values: list[str], desc: str
    ) -> list[tuple[str, str]]:
        if not values:
            return []
        p = partial.strip().casefold()
        exact = any(p == value.casefold() for value in values) if p else False
        chosen = values if (not p or exact) else [
            value for value in values if value.casefold().startswith(p)
        ]
        return [(f"{base} {value}", desc) for value in chosen]

    def _dynamic_slash_candidates(self, stripped: str) -> list[tuple[str, str]]:
        m = re.match(
            r"^/project\s+(open|close|delete|remove|unregister)\s*(.*)$",
            stripped,
            re.I,
        )
        if m:
            sub = m.group(1).lower()
            return self._argument_completion_rows(
                f"/project {sub}",
                m.group(2),
                self._registered_project_completion_names(),
                "프로젝트 선택",
            )

        m = re.match(r"^/branch\s+(switch|next)\s*(.*)$", stripped, re.I)
        if m:
            sub = m.group(1).lower()
            return self._argument_completion_rows(
                f"/branch {sub}",
                m.group(2),
                self._branch_completion_names(),
                "브랜치 선택",
            )
        return []

    def _slash_candidates(self) -> list[tuple[str, str]]:
        box = self.query_one("#prompt", TextArea)
        text = box.text
        if "\n" in text:
            return []
        stripped = text.lstrip()
        if not stripped.startswith("/"):
            return []

        dynamic = self._dynamic_slash_candidates(stripped)
        if dynamic:
            return dynamic

        prefix = stripped.lower()
        matches = [
            (cmd, desc)
            for cmd, desc in SLASH_COMMANDS
            if cmd.lower().startswith(prefix)
            or cmd.split()[0].lower().startswith(prefix)
        ]
        exact = any(cmd.lower() == prefix for cmd, _ in SLASH_COMMANDS)
        if exact and " " in prefix:
            root = prefix.split()[0]
            siblings = [
                (cmd, desc)
                for cmd, desc in SLASH_COMMANDS
                if cmd.lower().startswith(root + " ")
            ]
            if siblings:
                return siblings
        return matches

    def update_commandbar(self) -> None:
        bar = self.query_one("#commandbar", Static)
        candidates = self._slash_candidates()

        if not candidates:
            bar.styles.display = "none"
            bar.update("")
            return

        bar.styles.display = "block"
        shown = candidates[:6]
        text = "  ".join(f"{cmd}  {desc}" for cmd, desc in shown)
        if len(candidates) > len(shown):
            text += f"   +{len(candidates) - len(shown)} more"
        bar.update(text)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self.update_commandbar()

    def action_complete_command(self) -> None:
        if self._command_palette_active():
            return
        candidates = self._slash_candidates()
        if not candidates:
            return
        box = self.query_one("#prompt", TextArea)
        values = [candidate for candidate, _desc in candidates]
        current = box.text.strip()
        exact_idx = next(
            (i for i, value in enumerate(values) if value.casefold() == current.casefold()),
            None,
        )
        if exact_idx is not None and len(values) > 1:
            candidate = values[(exact_idx + 1) % len(values)]
        else:
            candidate = values[0]
        box.load_text(candidate + (" " if candidate in {"/verbose"} else ""))
        box.cursor_location = box.document.end
        self.update_commandbar()

    def action_newline(self) -> None:
        if self._command_palette_active():
            return
        box = self.query_one("#prompt", TextArea)
        box.insert("\n", maintain_selection_offset=False)
        self.update_commandbar()

    def _prepare_draft_payload(self, text: str) -> tuple[str, list[str]]:
        attachments = list(self.pending_attachments)
        cleaned = text
        try:
            detected = self._run_json_tool(
                [str(HOME / ".local/bin/orch-attachments"), "detect", "--text", text, "--json"],
                timeout=30,
            )
            for item in detected.get("attachments", []) or []:
                aid = str(item.get("id") or "")
                if aid and aid not in attachments:
                    attachments.append(aid)
            cleaned = str(detected.get("cleaned_text") or "")
        except Exception as e:
            self.note(f"Attachment path detection skipped: {e}", title="ATTACH WARNING")
        cleaned = cleaned.strip()
        if not cleaned and attachments:
            cleaned = "Analyze the attached file(s) and complete the task implied by their contents."
        return cleaned, attachments

    def action_clear_prompt(self) -> None:
        if self._command_palette_active():
            return
        # Reset only the unsent draft: text + pending attachment selections.
        # Persisted job/transcript state and immutable attachment blobs are untouched.
        box = self.query_one("#prompt", TextArea)
        box.load_text("")
        self.pending_attachments.clear()
        self.update_commandbar()

    def _command_palette_active(self) -> bool:
        try:
            return CommandPalette.is_open(self)
        except Exception:
            return False

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # The composer uses priority Enter/Tab bindings so TextArea can stay multiline.
        # While Textual's Ctrl+P CommandPalette screen is open, those priority
        # bindings must step aside or Enter never reaches the highlighted command.
        if self._command_palette_active() and action in {
            "send", "newline", "complete_command", "clear_prompt", "copy_last"
        }:
            return False
        try:
            return super().check_action(action, parameters)
        except AttributeError:
            return True

    def action_send(self) -> None:
        if self._command_palette_active():
            return
        box = self.query_one("#prompt", TextArea)
        raw_text = box.text
        text, contaminated = sanitize_terminal_input(raw_text)
        if contaminated:
            box.load_text(text)
            box.cursor_location = box.document.end
            self.update_commandbar()
            self.note(
                "터미널 mouse/ANSI 제어 시퀀스가 입력에 섞여 자동 전송을 차단했습니다.\n"
                "제어문자는 제거했습니다. 내용을 확인한 뒤 Enter를 다시 눌러 보내세요.",
                title="INPUT SANITIZED",
                collapsed=False,
            )
            return
        text = text.strip()
        if not text:
            return

        # Slash commands remain immediately available while a job runs.
        if text.startswith("/"):
            box.load_text("")
            self.update_commandbar()
            self.command(text)
            return

        prompt, attachments = self._prepare_draft_payload(text)
        if not prompt and not attachments:
            return

        if self._recent_submit_duplicate(prompt, attachments):
            box.load_text("")
            self.pending_attachments.clear()
            self.update_commandbar()
            self.note(
                f"같은 프롬프트가 {int(SUBMIT_DEDUPE_SECONDS)}초 안에 다시 submit되어 "
                "중복 실행/대기열 등록을 차단했습니다.",
                title="DUPLICATE INPUT BLOCKED",
                collapsed=False,
            )
            return

        busy_gate = self._queue_busy_gate()
        queue_has_items = bool(self.queue_state.get("items"))
        if busy_gate or queue_has_items:
            try:
                entry = self._queue_enqueue(prompt, attachments, gate=busy_gate)
            except Exception as e:
                self.note(
                    f"다음 작업 등록 실패: {e}\n프롬프트는 입력창에 그대로 유지했습니다.",
                    title="QUEUE ERROR",
                    collapsed=False,
                )
                return
            box.load_text("")
            self.pending_attachments.clear()
            self.update_commandbar()
            if entry.get("_duplicate_suppressed"):
                self.note(
                    f"동일한 최근 작업이 이미 대기열에 있어 중복 등록을 막았습니다 · "
                    f"{entry.get('id')}",
                    title="QUEUE DEDUP",
                    collapsed=False,
                )
                return
            self._remember_submit(prompt, attachments)
            self._branch_consume_pending()
            self.note(
                f"다음 작업으로 등록했습니다 · {entry.get('id')}\n"
                f"현재 작업이 COMPLETE가 되면 FIFO 순서로 자동 시작합니다.\n"
                f"대기열: {len(self.queue_state.get('items', []))}개",
                title="QUEUED",
                collapsed=False,
            )
            self._queue_tick()
            return

        try:
            self._branch_apply_plan(self._branch_next_plan())        try:
            self._branch_apply_plan(self._branch_next_plan())
        except Exception as e:
            self.note(f"브랜치 준비 실패: {e}\n프롬프트는 입력창에 그대로 유지했습니다.", title="BRANCH ERROR", collapsed=False)
            return
        self._branch_consume_pending()
        box.load_text("")
        self.pending_attachments.clear()
        self.update_commandbar()
        self.new_job(prompt, attachments=attachments)
        self._remember_submit(prompt, attachments)

    def command(self, text: str) -> None:
        p = text.split()
        cmd = p[0].lower()

        if cmd == "/restart":
            self._schedule_self_restart()

        elif cmd == "/settings":
            helper = HOME / ".local/bin/orch-settings"
            argv = [str(helper), *(p[1:] or ["show"])]
            try:
                q = subprocess.run(argv, cwd=self.repo, capture_output=True, text=True, timeout=30)
                output = q.stdout.strip() or q.stderr.strip() or f"exit={q.returncode}"
                self.note(output, title="SETTINGS", collapsed=False)
            except Exception as e:
                self.note(f"설정 명령 실패: {e}", title="SETTINGS ERROR", collapsed=False)

        elif cmd == "/reload":
            try:
                self.note(
                    self._reload_project_settings(),
                    title="RELOAD",
                    collapsed=False,
                )
            except Exception as e:
                self.note(f"설정 다시 읽기 실패: {e}", title="RELOAD ERROR", collapsed=False)

        elif cmd == "/version":
            self.note(self._version_status_text(), title="VERSION", collapsed=False)

        elif cmd == "/notify":
            sub = p[1].lower() if len(p) >= 2 else "status"
            if sub in {"status", "show"}:
                self.note(self._notify_status_text(), title="NOTIFY", collapsed=False)
            elif sub in {"smart", "all", "important", "off"}:
                self.notify_mode = sub
                self._save_tui_state()
                self.note(self._notify_status_text(), title="NOTIFY", collapsed=False)
            elif sub == "test":
                result = self._notify_test()
                if result.get("ok"):
                    self.note(
                        f"테스트 알림 전송 완료 · backend={result.get('backend')}",
                        title="NOTIFY TEST",
                        collapsed=False,
                    )
                else:
                    self.note(
                        f"테스트 알림 실패 · {result.get('reason') or 'unknown'}",
                        title="NOTIFY ERROR",
                        collapsed=False,
                    )
            else:
                self.note(
                    "사용법: /notify [smart|all|important|off|test]",
                    title="도움말",
                    collapsed=False,
                )

        elif cmd == "/queue":
            sub = p[1].lower() if len(p) >= 2 else "list"
            if sub in {"list", "status"}:
                self.note(self._queue_status_text(), title="QUEUE", collapsed=False)
            elif sub == "add":
                raw = text.split(None, 2)[2] if len(p) >= 3 else ""
                prompt, attachments = self._prepare_draft_payload(raw)
                if not prompt and not attachments:
                    self.note("사용법: /queue add <prompt>", title="도움말", collapsed=False)
                else:
                    try:
                        entry = self._queue_enqueue(
                            prompt,
                            attachments,
                            gate=self._queue_busy_gate(),
                        )
                        self.pending_attachments.clear()
                        if entry.get("_duplicate_suppressed"):
                            self.note(
                                f"동일한 최근 작업이 이미 대기열에 있어 중복 등록을 막았습니다 · "
                                f"{entry.get('id')}",
                                title="QUEUE DEDUP",
                                collapsed=False,
                            )
                        else:
                            self._remember_submit(prompt, attachments)
                            self._branch_consume_pending()
                            self.note(
                                f"대기열에 추가됨 · {entry.get('id')}",
                                title="QUEUE",
                                collapsed=False,
                            )
                            self._queue_tick()
                    except Exception as e:
                        self.note(f"대기열 추가 실패: {e}", title="QUEUE ERROR", collapsed=False)
            elif sub == "remove":
                if len(p) < 3:
                    self.note("사용법: /queue remove <번호|queue-id>", title="도움말", collapsed=False)
                else:
                    ok, message = self._queue_remove(p[2])
                    self.note(message, title="QUEUE" if ok else "QUEUE NOTICE", collapsed=False)
            elif sub == "clear":
                removed = self._queue_clear_pending()
                self.note(
                    f"대기 작업 {removed}개를 삭제했습니다. 실행 중 작업은 건드리지 않았습니다.",
                    title="QUEUE",
                    collapsed=False,
                )
            elif sub == "pause":
                self.queue_state["paused"] = True
                self._save_queue_state()
                self.note(
                    "대기열 자동 실행을 일시정지했습니다. 현재 실행 중인 작업은 계속됩니다.",
                    title="QUEUE PAUSED",
                    collapsed=False,
                )
            elif sub == "resume":
                self._queue_resume_explicit()
                self.note(
                    "대기열 자동 실행을 재개했습니다.",
                    title="QUEUE",
                    collapsed=False,
                )
            else:
                self.note(
                    "사용법:\n"
                    "/queue\n"
                    "/queue add <prompt>\n"
                    "/queue remove <번호|queue-id>\n"
                    "/queue clear\n"
                    "/queue pause\n"
                    "/queue resume",
                    title="도움말",
                    collapsed=False,
                )

        elif cmd == "/verbose":
            if len(p) > 1 and p[1] in {"normal", "verbose", "trace"}:
                self.verbosity = p[1]
                self.rerender()
                self.note(f"Detail · {self.verbosity}", title="DISPLAY")
            else:
                self.note("usage: /verbose normal|verbose|trace", title="HELP")

        elif cmd in {"/details", "/detail"}:
            self.action_details()

        elif cmd == "/doctor":
            self.action_doctor()

        elif cmd == "/steer":
            instruction = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""
            self.steer_job(instruction)

        elif cmd == "/pause":
            self.action_pause()

        elif cmd in {"/resume", "/retry"}:
            self.action_resume()

        elif cmd == "/cancel":
            self.action_cancel()

        elif cmd == "/copy":
            mode = p[1].lower() if len(p) >= 2 else "last"
            aliases = {"assistant": "last", "answer": "last"}
            mode = aliases.get(mode, mode)
            if mode not in {"last", "user", "all", "worked", "prompt"}:
                self.note(
                    "usage: /copy [last|user|all|worked|prompt]",
                    title="HELP",
                    collapsed=False,
                )
            else:
                self._copy_to_clipboard(mode)

        elif cmd == "/quota":
            try:
                q = subprocess.run(
                    [str(HOME / ".local/bin/ai-quota")],
                    capture_output=True,
                    text=True,
                    timeout=75,
                )
                quota_text = q.stdout.strip() or q.stderr.strip() or "quota probe returned no output"
                self.note(
                    quota_text,
                    title="QUOTA",
                    collapsed=False,
                )
                self._last_quota_refresh = 0
            except Exception as e:
                self.note(f"quota probe failed: {e}", title="ERROR", collapsed=False)

        elif cmd in {"/history", "/jobs"}:
            rows = []
            for d in sorted(JOBS_DIR.glob("job-*"), reverse=True)[:20]:
                try:
                    j = json.loads((d / "job.json").read_text())
                    rows.append(
                        f"{j.get('id')} · {j.get('status')} · {j.get('title','')}"
                    )
                except Exception:
                    pass
            self.note("\n".join(rows) or "No jobs.", title="HISTORY", collapsed=False)

        elif cmd == "/job":
            self.note(
                json.dumps(self.job or {}, ensure_ascii=False, indent=2),
                title="JOB",
                collapsed=False,
            )

        elif cmd == "/session":
            if self.job and self.job_dir:
                text_out = (
                    f"project: {os.getenv('AI_ORCH_PROJECT_NAME', self.repo.name)}\n"
                    f"workspace: {os.getenv('AI_ORCH_WORKSPACE_ID', '-')}\n"
                    f"id: {self.job.get('id')}\n"
                    f"path: {self.job_dir}\n"
                    f"status: {self.job.get('status')}\n"
                    f"attempt: {self.job.get('attempt')}\n"
                    f"scope: {self.job.get('scope_mode', self.scope_mode)}\n"
                    f"prompt: {str(self.job.get('prompt_sha256', ''))[:8]}\n"
                    f"max_delegates: {self.job.get('max_delegates', 3)}\n"
                    f"router: {'on' if self.router_enabled else 'off'}\n"
                    f"model: {self.model_override}"
                )
            else:
                text_out = (
                    "No active TUI job.\n"
                    f"router: {'on' if self.router_enabled else 'off'}\n"
                    f"model: {self.model_override}"
                )
            self.note(text_out, title="SESSION", collapsed=False)

        elif cmd == "/project":
            helper = HOME / ".local/bin/orch-project"
            if len(p) == 1:
                argv = [str(helper), "current"]
                title = "PROJECT"
            elif len(p) >= 2 and p[1].lower() == "list":
                argv = [str(helper), "list"]
                title = "PROJECTS"
            elif len(p) >= 3 and p[1].lower() == "open":
                query = text.split(None, 2)[2].strip()
                argv = [str(helper), "open", query]
                title = "PROJECT"
            elif len(p) >= 3 and p[1].lower() == "new":
                name = p[2]
                argv = [str(helper), "new", name]
                if "--no-git" in p[3:]:
                    argv.append("--no-git")
                title = "PROJECT NEW"
            elif len(p) >= 2 and p[1].lower() == "root":
                argv = [str(helper), "root"]
                if len(p) >= 3:
                    argv.append(text.split(None, 2)[2].strip())
                title = "PROJECT ROOT"
            elif len(p) >= 3 and p[1].lower() == "alias":
                alias = text.split(None, 2)[2].strip()
                argv = [str(helper), "alias", alias]
                title = "PROJECT ALIAS"
            elif len(p) >= 2 and p[1].lower() == "close":
                argv = [str(helper), "close"]
                if len(p) >= 3:
                    argv.append(text.split(None, 2)[2].strip())
                title = "PROJECT CLOSE"
            elif len(p) >= 3 and p[1].lower() in {"delete", "remove", "unregister"}:
                query = text.split(None, 2)[2].strip()
                argv = [str(helper), "delete", query]
                title = "PROJECT DELETE"
            else:
                self.note(
                    "usage:\n"
                    "/project\n"
                    "/project list\n"
                    "/project open <name|path>\n"
                    "/project new <name> [--no-git]\n"
                    "/project root [path]\n"
                    "/project alias <short-name|auto>\n"
                    "/project close [name]\n"
                    "/project delete <name>   # 목록 등록만 제거; repo/state 보존",
                    title="HELP",
                    collapsed=False,
                )
                return
            try:
                q = subprocess.run(argv, capture_output=True, text=True, timeout=30)
                output = q.stdout.strip() or q.stderr.strip()
                if output:
                    self.note(output, title=title, collapsed=False)
            except Exception as e:
                self.note(f"project command failed: {e}", title="ERROR", collapsed=False)

        elif cmd == "/current":
            prompt = self._current_prompt_text()
            if not self.job or not prompt:
                self.note("No active job prompt.", title="CURRENT", collapsed=False)
            else:
                self.note(
                    (
                        f"job: {self.job.get('id')}\n"
                        f"prompt_sha256: {self.job.get('prompt_sha256')}\n"
                        f"scope: {self.job.get('scope_mode')}\n"
                        f"max_delegates: {self.job.get('max_delegates')}\n\n"
                        f"{prompt}"
                    ),
                    title="CURRENT PROMPT",
                    collapsed=False,
                )

        elif cmd == "/branch":
            sub = p[1].lower() if len(p) >= 2 else "status"
            if sub in {"status", "show"}:
                branch = git_branch(self.repo); head = git_head(self.repo); dirty = git_dirty_count(self.repo)
                self.note(
                    f"현재 브랜치: {branch}\nHEAD: {head[:12] or '-'}\n"
                    f"변경 파일: {dirty if dirty >= 0 else '?'}\n"
                    f"다음 작업 예약: {self._branch_plan_text(self.pending_branch_plan)}",
                    title="BRANCH", collapsed=False,
                )
            elif sub == "list":
                q = subprocess.run(
                    ["git", "branch", "--format=%(HEAD) %(refname:short)"], cwd=self.repo,
                    capture_output=True, text=True, timeout=10,
                )
                self.note(q.stdout.strip() or q.stderr.strip() or "브랜치가 없습니다.", title="BRANCHES", collapsed=False)
            elif sub == "switch" and len(p) >= 3:
                try:
                    self._branch_switch_existing(p[2])
                    self.update_banner()
                    self.note(f"브랜치 전환 완료 · {git_branch(self.repo)}", title="BRANCH")
                except Exception as e:
                    self.note(str(e), title="BRANCH ERROR", collapsed=False)
            elif sub == "new" and len(p) >= 3:
                from_ref = "HEAD"
                if "--from" in p[3:]:
                    idx = p.index("--from")
                    if idx + 1 >= len(p):
                        self.note("사용법: /branch new <name> [--from <ref>]", title="도움말", collapsed=False); return
                    from_ref = p[idx + 1]
                try:
                    self._branch_create(p[2], from_ref)
                    self.update_banner()
                    self.note(f"새 브랜치 생성/전환 · {git_branch(self.repo)} <- {from_ref}", title="BRANCH")
                except Exception as e:
                    self.note(str(e), title="BRANCH ERROR", collapsed=False)
            elif sub == "next" and len(p) >= 3:
                branch = p[2]
                if not git_valid_branch_name(self.repo, branch) or not (git_local_branch_exists(self.repo, branch) or git_origin_branch_exists(self.repo, branch)):
                    self.note(f"기존 브랜치를 찾을 수 없습니다: {branch}", title="BRANCH ERROR", collapsed=False)
                else:
                    self.pending_branch_plan = {"mode": "switch", "branch": branch}
                    self._save_tui_state()
                    self.note(f"다음 프롬프트는 브랜치 {branch}에서 실행합니다.", title="BRANCH NEXT")
            elif sub == "next-new" and len(p) >= 3:
                branch = p[2]; from_ref = "HEAD"
                if "--from" in p[3:]:
                    idx = p.index("--from")
                    if idx + 1 >= len(p):
                        self.note("사용법: /branch next-new <name> [--from <ref>]", title="도움말", collapsed=False); return
                    from_ref = p[idx + 1]
                if not git_valid_branch_name(self.repo, branch):
                    self.note(f"유효하지 않은 브랜치 이름: {branch}", title="BRANCH ERROR", collapsed=False)
                elif git_local_branch_exists(self.repo, branch):
                    self.note(f"이미 존재하는 로컬 브랜치입니다: {branch}", title="BRANCH ERROR", collapsed=False)
                elif not git_ref_oid(self.repo, from_ref):
                    self.note(f"기준 ref를 찾을 수 없습니다: {from_ref}", title="BRANCH ERROR", collapsed=False)
                else:
                    self.pending_branch_plan = {"mode": "new", "branch": branch, "from": from_ref}
                    self._save_tui_state()
                    self.note(f"다음 프롬프트용 새 브랜치 예약 · {branch} <- {from_ref}", title="BRANCH NEXT")
            elif sub in {"next-clear", "clear-next"}:
                self.pending_branch_plan = None
                self._save_tui_state()
                self.note("다음 작업 브랜치 예약을 취소했습니다.", title="BRANCH NEXT")
            else:
                self.note(
                    "사용법:\n"
                    "/branch\n/branch list\n/branch switch <name>\n"
                    "/branch new <name> [--from <ref>]\n"
                    "/branch next <name>\n/branch next-new <name> [--from <ref>]\n"
                    "/branch next-clear",
                    title="도움말", collapsed=False,
                )

        elif cmd == "/permissions":
            if len(p) < 2:
                self.note(
                    f"permission_profile = {self.permission_profile}\n"
                    "trusted: 로컬 repo 편집/테스트/의존성 설치/안전한 브랜치 생성·전환을 자율 허용\n"
                    "guarded: 필요한 로컬 작업만 보수적으로 수행\n"
                    "항상 차단: destructive Git, force-push, 자격증명/결제, live/private 거래, 고위험 cloud 변경",
                    title="PERMISSIONS", collapsed=False,
                )
            else:
                mode = p[1].lower()
                if mode not in {"trusted", "guarded"}:
                    self.note("사용법: /permissions trusted|guarded", title="도움말", collapsed=False)
                else:
                    self.permission_profile = mode
                    self._save_tui_state()
                    self.note(f"에이전트 로컬 권한 프로필 · {mode}", title="PERMISSIONS")

        elif cmd == "/scope":
            if len(p) < 2:
                self.note(
                    f"scope = {self.scope_mode}\nusage: /scope contextual|strict",
                    title="SCOPE",
                    collapsed=False,
                )
            else:
                mode = p[1].lower()
                if mode not in {"contextual", "strict"}:
                    self.note("usage: /scope contextual|strict", title="HELP")
                else:
                    self.scope_mode = mode
                    self._save_tui_state()
                    self.update_banner()
                    self.note(
                        (
                            f"Default scope · {mode}\n"
                            + (
                                "Strict jobs use current-message-only task discipline "
                                "and default to one delegate."
                                if mode == "strict"
                                else "Contextual jobs use the normal persistent-task mode."
                            )
                        ),
                        title="SCOPE",
                        collapsed=False,
                    )

        elif cmd == "/new":
            live = self._live_main_worker()
            if live:
                self.note(
                    f"Current/recovered MAIN worker is running (pid {live.get('pid')}); "
                    "pause/cancel it before /new.",
                    title="NOTICE",
                    collapsed=False,
                )
            else:
                self._clear_feed_for_new_session()
                self.note("Fresh TUI session ready.", title="SESSION", collapsed=False)

        elif cmd == "/router":
            if len(p) < 2:
                self.note(
                    f"router = {'on' if self.router_enabled else 'off'}\n"
                    "usage: /router on|off|reset",
                    title="ROUTER",
                    collapsed=False,
                )
            elif p[1].lower() == "on":
                self.router_enabled = True
                self._save_tui_state()
                self.update_banner()
                self.note("Adaptive router enabled.", title="ROUTER")
            elif p[1].lower() == "off":
                self.router_enabled = False
                self._save_tui_state()
                self.update_banner()
                self.note("Adaptive router disabled.", title="ROUTER")
            elif p[1].lower() == "reset":
                if self.proc and self.proc.poll() is None:
                    self.note("Cannot reset router state while a job is running.", title="NOTICE")
                else:
                    try:
                        ROUTER_STATE_FILE.unlink(missing_ok=True)
                        self.route = ""
                        self.note(
                            "Router cooldown/failure state cleared. Frontend router setting unchanged.",
                            title="ROUTER",
                            collapsed=False,
                        )
                    except Exception as e:
                        self.note(f"router reset failed: {e}", title="ERROR", collapsed=False)
            else:
                self.note("usage: /router on|off|reset", title="HELP")

        elif cmd == "/model":
            allowed = {
                "auto",
                "cmd",
                "codex",
                "sonnet",
                "opus",
                "claude",
                "claude-opus",
                "gemini-low",
                "gemini-medium",
                "gemini-high",
            }
            if len(p) < 2:
                self.note(
                    f"model = {self.model_override}\n"
                    "usage: /model auto|cmd|codex|sonnet|opus|claude|claude-opus|"
                    "gemini-low|gemini-medium|gemini-high",
                    title="MODEL",
                    collapsed=False,
                )
            else:
                model = p[1].lower()
                if model not in allowed:
                    self.note(
                        "usage: /model auto|cmd|codex|sonnet|opus|"
                        "gemini-low|gemini-medium|gemini-high",
                        title="HELP",
                    )
                else:
                    self.model_override = model
                    self._save_tui_state()
                    self.update_banner()
                    self.note(f"Model override · {model}", title="MODEL")

        elif cmd == "/decision":
            decision, route = self._latest_router_lines()
            text_out = (
                f"decision: {decision or 'not available'}\n"
                f"route: {pretty_route(route) if route else 'not available'}"
            )
            self.note(text_out, title="DECISION", collapsed=False)

        elif cmd == "/status":
            branch = git_branch(self.repo)
            job_status = self.job.get("status") if self.job else "none"
            current = self.current.model if self.current and self.current.ended is None else "none"
            text_out = (
                f"repo: {self.repo}\n"
                f"branch: {branch}\n"
                f"job_branch: {str((self.job or {}).get('branch_at_start') or '-')}\n"
                f"next_branch: {self._branch_plan_text(self.pending_branch_plan)}\n"
                f"workspace: {WORKSPACE_ID or 'legacy-global'}\n"
                f"project_base: {PROJECT_BASE}\n"
                f"context: {WORKSPACE_SOURCE}\n"
                f"job: {job_status}\n"
                f"current: {current}\n"
                f"router: {'on' if self.router_enabled else 'off'}\n"
                f"model: {self.model_override}\n"
                f"scope: {(self.job or {}).get('scope_mode', self.scope_mode)}\n"
                f"prompt: {str((self.job or {}).get('prompt_sha256', ''))[:8] or '-'}\n"
                f"detail: {self.verbosity}\n"
                f"CMD: {self._cmd_quota_summary}"
            )
            self.note(text_out, title="STATUS", collapsed=False)

        elif cmd == "/agents":
            if not self.attempts:
                text_out = (
                    "No model attempts in this TUI session.\n\n"
                    "Available overrides:\n"
                    "  cmd · codex · sonnet · opus · claude · claude-opus · "
                    "gemini-low · gemini-medium · gemini-high"
                )
            else:
                rows = []
                for a in self.attempts:
                    state = "RUNNING" if a.ended is None else "done"
                    rows.append(f"{a.model} · {state} · {a.elapsed()} · {len(a.lines)} events")
                text_out = "\n".join(rows)
            self.note(text_out, title="AGENTS", collapsed=False)

        elif cmd == "/workers":
            running = self.proc is not None and self.proc.poll() is None
            recovered = None if running else self._live_main_worker()
            all_live = self._scan_live_main_workers()
            current_repo = _canonical_repo(self.repo)
            same_repo_other = 0
            other_projects = 0
            current_dir = str(self.job_dir.resolve()) if self.job_dir else None
            for row in all_live:
                try:
                    row_repo = _canonical_repo(Path(str(row.get("repo") or "")))
                except Exception:
                    continue
                if row_repo == current_repo:
                    if not current_dir or str(row.get("job_dir") or "") != current_dir:
                        same_repo_other += 1
                else:
                    other_projects += 1
            active_delegates = sum(
                1
                for ev in self.delegate_states.values()
                if str(ev.get("status") or ev.get("event")) == "RUNNING"
            )
            if running:
                main_state = f"RUNNING_LOCAL pid={self.proc.pid}"
            elif recovered:
                main_state = f"RECOVERED {recovered.get('status')} pid={recovered.get('pid')}"
            else:
                main_state = "idle"
            text_out = (
                f"workspace: {WORKSPACE_ID or 'legacy-global'}\n"
                f"project_base: {PROJECT_BASE}\n"
                f"main_worker: {main_state}\n"
                f"job: {(self.job or {}).get('id', '-')}\n"
                f"attempt: {(self.job or {}).get('attempt', '-')}\n"
                f"current_model: {self.current.model if self.current and self.current.ended is None else '-'}\n"
                f"same_repo_other_live: {same_repo_other}\n"
                f"other_project_main_workers: {other_projects}\n"
                f"delegate_workers_active: {active_delegates}\n"
                f"delegate_workers_seen: {len(self.delegate_states)}"
            )
            self.note(text_out, title="WORKERS", collapsed=False)

        elif cmd == "/delegates":
            self._poll_delegate_events()
            self.note(
                self._delegate_summary_text(),
                title="DELEGATES",
                collapsed=False,
            )

        elif cmd == "/attach":
            try:
                tool = str(HOME / ".local/bin/orch-attachments")
                if len(p) == 1:
                    self.action_attach()
                elif len(p) >= 3 and p[1] == "add":
                    path_arg = text.split(None, 2)[2]
                    items = self._run_json_tool([tool, "add", path_arg])
                    self._remember_attachment_items(items)
                elif len(p) >= 2 and p[1] == "list":
                    items = self._run_json_tool([tool, "list", "--json"])
                    selected = set(self.pending_attachments)
                    lines = [("* " if x.get("id") in selected else "  ") + f"{x.get('id')} · {x.get('kind')} · {x.get('name')} · {x.get('size')} bytes" for x in items]
                    self.note("\n".join(lines) or "No attachments.", title="ATTACHMENTS", collapsed=False)
                elif len(p) >= 2 and p[1] == "clear":
                    self.pending_attachments.clear(); self.note("Pending attachments cleared.", title="ATTACHMENTS", collapsed=False)
                elif len(p) >= 3 and p[1] in {"remove", "rm"}:
                    aid = p[2]; self.pending_attachments = [x for x in self.pending_attachments if x != aid]; self.note(f"Removed pending attachment {aid}", title="ATTACHMENTS", collapsed=False)
                else:
                    self.note("usage: /attach | /attach add <path> | /attach list | /attach remove <id> | /attach clear", title="HELP", collapsed=False)
            except Exception as e:
                self.note(f"Attachment command failed: {e}", title="ATTACH ERROR", collapsed=False)

        elif cmd == "/skills":
            try:
                ps = subprocess.run([str(HOME / ".local/bin/orch-skills"), "available"], capture_output=True, text=True, timeout=10)
                active = self.job.get("skills", []) if self.job else []
                self.note("active: " + (", ".join(active) if active else "none") + "\n\n" + (ps.stdout.strip() or ps.stderr.strip()), title="SKILLS", collapsed=False)
            except Exception as e:
                self.note(f"Skills query failed: {e}", title="ERROR", collapsed=False)

        elif cmd == "/tools":
            try:
                argv = [str(HOME / ".local/bin/orch-tools")]
                argv += (["set", p[1], p[2]] if len(p) >= 3 else ["status"])
                pt = subprocess.run(argv, capture_output=True, text=True, timeout=10)
                self.note(pt.stdout.strip() or pt.stderr.strip(), title="TOOLS", collapsed=False)
            except Exception as e:
                self.note(f"Tools query failed: {e}", title="ERROR", collapsed=False)

        elif cmd == "/mcp":
            try:
                pm = subprocess.run([str(HOME / ".local/bin/orch-mcp"), "list"], capture_output=True, text=True, timeout=10)
                self.note(pm.stdout.strip() or pm.stderr.strip() or "No MCP servers registered.", title="MCP · task-scoped", collapsed=False)
            except Exception as e:
                self.note(f"MCP query failed: {e}", title="ERROR", collapsed=False)

        elif cmd == "/collab":
            try:
                argv = [str(HOME / ".local/bin/orch-collab"), "list", "--limit", "20"]
                if self.job:
                    argv.extend(["--job", str(self.job.get("id"))])
                p3 = subprocess.run(argv, capture_output=True, text=True, timeout=10)
                self.note(
                    p3.stdout.strip() or p3.stderr.strip() or "No Phase 3 collaboration activity.",
                    title="PHASE 3 COLLAB",
                    collapsed=False,
                )
            except Exception as e:
                self.note(f"Phase 3 collaboration query failed: {e}", title="ERROR", collapsed=False)

        elif cmd == "/collect":
            try:
                batch_id = p[1] if len(p) >= 2 else "latest"
                argv = [str(HOME / ".local/bin/orch-collect"), "--batch", batch_id]
                if self.job and batch_id == "latest":
                    argv.extend(["--job", str(self.job.get("id"))])
                p3 = subprocess.run(argv, capture_output=True, text=True, timeout=10)
                self.note(
                    p3.stdout.strip() or p3.stderr.strip() or "No collection packet.",
                    title="PHASE 3 COLLECTION",
                    collapsed=False,
                )
            except Exception as e:
                self.note(f"Phase 3 collection query failed: {e}", title="ERROR", collapsed=False)

        elif cmd == "/draft":
            if not self.job:
                self.note("No current job to draft from.", title="REVIEW DRAFT", collapsed=False)
            else:
                try:
                    py = BASE / "venv/bin/python"
                    p = subprocess.run(
                        [
                            str(py), str(APP_DIR / "phase1_supervisor.py"),
                            "draft", "--job", str(self.job.get("id")),
                            "--repo", str(self.repo),
                        ],
                        capture_output=True, text=True, timeout=15,
                        env={**os.environ.copy(), "AI_ORCH_PROJECT_BASE": str(PROJECT_BASE), "AI_ORCH_REPO": str(self.repo), "AI_ORCH_JOB_ID": str(self.job.get("id"))},
                    )
                    self.note(
                        p.stdout.strip() or p.stderr.strip() or "No draft evidence available.",
                        title="REVIEW DRAFT",
                        collapsed=False,
                    )
                except Exception as e:
                    self.note(f"review draft failed: {e}", title="ERROR", collapsed=False)

        elif cmd == "/facts":
            try:
                argv = [
                    str(HOME / ".local/bin/orch-facts"),
                    "--limit", "30",
                    "--repo", str(self.repo),
                ]
                if self.job:
                    argv.extend(["--job", str(self.job.get("id"))])
                p = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.note(
                    p.stdout.strip() or p.stderr.strip() or "No facts.",
                    title="FACT LEDGER",
                    collapsed=False,
                )
            except Exception as e:
                self.note(f"fact ledger query failed: {e}", title="ERROR", collapsed=False)

        elif cmd == "/health":
            try:
                p = subprocess.run(
                    [str(HOME / ".local/bin/orch-health")],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.note(
                    p.stdout.strip() or p.stderr.strip() or "No provider health state.",
                    title="PROVIDER HEALTH",
                    collapsed=False,
                )
            except Exception as e:
                self.note(f"provider health query failed: {e}", title="ERROR", collapsed=False)

        elif cmd == "/recover":
            try:
                p = subprocess.run(
                    [
                        str(HOME / ".local/bin/orch-delegate-recover"),
                        "--repo", str(self.repo),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.note(
                    p.stdout.strip() or p.stderr.strip() or "No stale delegate leases.",
                    title="LEASE RECOVERY",
                    collapsed=False,
                )
            except Exception as e:
                self.note(f"delegate recovery failed: {e}", title="ERROR", collapsed=False)

        elif cmd == "/update":
            args = p[1:]
            if not args:
                self._run_update_async(["status"], "Update status")
            elif args[0] == "status":
                self._run_update_async(["status"], "Update status")
            elif args[0] == "check":
                provider = args[1] if len(args) > 1 else "all"
                self._run_update_async(["check", provider], f"Check updates · {provider}")
            elif args[0] == "now":
                provider = args[1] if len(args) > 1 else "all"
                self._run_update_async(["now", provider], f"Update now · {provider}")
            elif args[0] == "auto":
                if len(args) >= 2 and args[1] == "on":
                    when = args[2] if len(args) >= 3 else "09:00"
                    self._run_update_async(["auto", "on", when], f"Auto update on · {when}")
                elif len(args) >= 2 and args[1] == "off":
                    self._run_update_async(["auto", "off"], "Auto update off")
                else:
                    self.note(
                        "usage: /update auto on [HH:MM] | /update auto off",
                        title="HELP",
                        collapsed=False,
                    )
            elif args[0] == "log":
                self._run_update_async(["log"], "Update log")
            else:
                self.note(
                    "usage: /update [status|check [all|codex|agy|cmd]|"
                    "now [all|codex|agy|cmd]|auto on [HH:MM]|auto off|log]",
                    title="HELP",
                    collapsed=False,
                )

        elif cmd == "/diag":
            self._refresh_process_diag()
            self.note(
                self._diag_text(),
                title="DIAGNOSTICS",
                collapsed=False,
            )

        elif cmd == "/skills":
            self.note(self._project_skills_text(), title="SKILLS", collapsed=False)

        elif cmd == "/mcp":
            self.note(self._mcp_text(), title="MCP", collapsed=False)

        elif cmd == "/compact":
            if not self.job_dir:
                self.note("No current persistent job to compact.", title="NOTICE")
            else:
                self.checkpoint()
                cp = self.job_dir / "checkpoint.md"
                self.note(
                    f"Resume checkpoint refreshed:\n{cp}",
                    title="COMPACT",
                    collapsed=False,
                )

        elif cmd == "/help":
            help_text = (
                "단축키\n"
                "  Enter        프롬프트 전송\n"
                "               작업 중 Enter는 다음 작업 대기열에 등록\n"
                "  Shift+Enter  줄바꿈\n"
                "  Alt+Enter    줄바꿈 대체 키\n"
                "  Tab          슬래시 명령어 자동완성\n"
                "  F2           상세 표시: normal / verbose / trace 순환\n"
                "  Ctrl+O       파일 / 이미지 첨부\n"
                "  Ctrl+T       최근 WORKED 패널 열기 / 닫기\n"
                "  Ctrl+U       현재 입력 + 대기 중 첨부 전체 초기화\n"
                "  Ctrl+Y       마지막 Assistant 답변 복사\n"
                "  Ctrl+R       일시정지 작업 재시도 / 재개\n"
                "  Ctrl+X       현재 작업 취소\n"
                "  Ctrl+Q       TUI 종료 (업데이트 반영은 /restart 권장)\n\n"
                "명령어\n"
                + "\n".join(f"  {name:<22} {desc}" for name, desc in SLASH_COMMANDS)
            )
            self.note(help_text, title="도움말", collapsed=False)

        elif cmd in {"/quit", "/exit"}:
            self.exit()

        else:
            self.note(
                "알 수 없는 명령어입니다. /help 또는 /를 입력해 사용 가능한 명령어를 확인하세요.",
                title="도움말",
            )

    def action_copy_last(self) -> None:
        if self._command_palette_active():
            return
        self._copy_to_clipboard("last")

    def action_details(self) -> None:
        modes = ["normal", "verbose", "trace"]
        self.verbosity = modes[(modes.index(self.verbosity) + 1) % len(modes)]
        self.rerender()
        self.note(f"Detail · {self.verbosity}", title="DISPLAY")
        self.update_banner()

    def action_toggle_last(self) -> None:
        if not self.attempts:
            return
        panel = self.attempts[-1].panel
        if panel:
            panel.collapsed = not panel.collapsed

    def action_doctor(self) -> None:
        try:
            p = subprocess.run(
                [str(HOME / ".local/bin/orch-doctor")],
                capture_output=True,
                text=True,
                timeout=45,
                env=os.environ.copy(),
            )
            output = (p.stdout or p.stderr or "doctor returned no output").strip()
            self.note(output, title="DOCTOR", collapsed=False)
        except Exception as exc:
            self.note(
                f"doctor failed: {type(exc).__name__}: {exc}",
                title="ERROR",
                collapsed=False,
            )

    def steer_job(self, instruction: str) -> None:
        instruction = instruction.strip()
        if not instruction:
            self.note("사용법: /steer <현재 작업에 반영할 지시>", title="도움말", collapsed=False)
            return
        if not self.job or not self.job_dir:
            self.note("현재 조정할 작업이 없습니다.", title="STEER", collapsed=False)
            return
        status = str(self.job.get("status") or "")
        if status in {"COMPLETE", "FAILED", "NEEDS_GO", "NEEDS_USER", "BLOCKED", "CANCELLED"}:
            self.note(f"현재 작업은 {status} 상태라 steer할 수 없습니다.", title="STEER", collapsed=False)
            return

        local_running = bool(self.proc is not None and self.proc.poll() is None)
        recovered_running = bool(self._live_main_worker())
        was_running = local_running or recovered_running
        self.checkpoint()
        append_jsonl(
            self.job_dir / "steer-history.jsonl",
            {
                "ts": iso(),
                "instruction": instruction,
                "from_status": status,
                "sequence": int(self.job.get("steer_count", 0)) + 1,
                "source": "tui",
            },
        )
        self.job["steer_count"] = int(self.job.get("steer_count", 0)) + 1
        self.job["last_steer"] = instruction
        self.job["last_steer_at"] = iso()
        self.event("status", f"STEER: {instruction}")

        if was_running:
            self.job["status"] = "PAUSED_USER"
            self.job["resume_at"] = None
            self.job["steer_pending"] = True
            self.save()
            ok, detail = self._terminate_main_worker()
            if not ok:
                self.note(
                    "Steer 지시는 저장했지만 현재 MAIN을 안전하게 중단하지 못했습니다.\n" + detail,
                    title="STEER WARNING",
                    collapsed=False,
                )
                return
            if not local_running:
                self.job["steer_pending"] = False
                self.job["status"] = "PAUSED_RETRY"
                self.job["resume_at"] = now() + 5
                self.save()
            self.note(
                f"Steer 적용 · 같은 job으로 안전하게 재개 예정\n{instruction}\n{detail}",
                title="STEER",
                collapsed=False,
            )
            return

        self.save()
        self.note(
            f"Steer 지시를 현재 job에 저장했습니다. 다음 /resume에 반영됩니다.\n{instruction}",
            title="STEER",
            collapsed=False,
        )

    def action_pause(self) -> None:
        live = self._live_main_worker()
        detail = ""
        if live:
            ok, detail = self._terminate_main_worker()
            if not ok:
                self.note(detail, title="PAUSE WARNING", collapsed=False)
                return

        if self.job:
            self.job.update({"status": "PAUSED_USER", "resume_at": None})
            worker = self.job.get("worker") if isinstance(self.job.get("worker"), dict) else {}
            if worker:
                worker = dict(worker)
                worker["state"] = "PAUSED_USER"
                self.job["worker"] = worker
            self.save()
            self.checkpoint()

        self.proc = None
        self.note("Paused by user." + (f"\n{detail}" if detail else ""), title="PAUSED")
        self.update_banner()

    def action_resume(self) -> None:
        if not self.job:
            self.note("No resumable job.", title="RESUME", collapsed=False)
            return

        if self.job_dir:
            disk_job = self.job_dir / "job.json"
            if disk_job.exists():
                try:
                    latest = json.loads(disk_job.read_text())
                    if latest.get("id") == self.job.get("id"):
                        self.job.update(latest)
                except Exception as e:
                    self.note(
                        f"Could not refresh current job: {e}",
                        title="RESUME WARNING",
                        collapsed=False,
                    )

        status = str(self.job.get("status") or "")
        if status in {"COMPLETE", "NEEDS_GO", "NEEDS_USER", "BLOCKED", "CANCELLED"}:
            self.note(
                f"Job is {status}; not resumable.",
                title="RESUME",
                collapsed=False,
            )
            return

        live = self._live_main_worker()
        if live:
            self.note(
                f"Worker is still alive (pid {live.get('pid')} · {live.get('status')}); "
                "not launching a duplicate retry.",
                title="RESUME",
                collapsed=False,
            )
            return

        if self.proc is not None and self.proc.poll() is not None:
            self.proc = None

        self.job.update({"status": "PAUSED_RETRY", "resume_at": now()})
        self.save()
        self.update_banner()
        self.resume_job("manual")

    def action_cancel(self) -> None:
        live = self._live_main_worker()
        detail = ""
        if live:
            ok, detail = self._terminate_main_worker()
            if not ok:
                self.note(detail, title="CANCEL WARNING", collapsed=False)
                return

        if self.job:
            self.job.update({"status": "CANCELLED", "resume_at": None})
            worker = self.job.get("worker") if isinstance(self.job.get("worker"), dict) else {}
            if worker:
                worker = dict(worker)
                worker["state"] = "CANCELLED"
                self.job["worker"] = worker
            self.save()

        self.proc = None
        self.note("Job cancelled." + (f"\n{detail}" if detail else ""), title="CANCELLED")
        if self.queue_state.get("items"):
            self._queue_halt(
                f"{str((self.job or {}).get('id') or 'current job')} 이(가) CANCELLED 상태로 끝나 대기열을 안전하게 중단했습니다."
            )
        self.update_banner()


if __name__ == "__main__":
    OrchBridgeApp().run()

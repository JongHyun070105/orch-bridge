#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout

HOME = Path.home()
DATA_ROOT = HOME / ".local/share/orchbridge"
SESSIONS_ROOT = DATA_ROOT / "sessions"
CACHE = HOME / ".cache/orchbridge/agy-status.json"
ROUTER_STATE = HOME / ".cache/orchbridge/router-state.json"
CONFIG = HOME / ".config/orchbridge/config.json"

COMMANDS = [
    "/help", "/status", "/quota", "/history", "/session", "/new",
    "/router on", "/router off", "/router reset",
    "/model auto", "/model cmd", "/model codex", "/model sonnet",
    "/model opus", "/model claude", "/model claude-opus", "/model gemini-medium", "/model gemini-high",
    "/model gemini-low", "/agents", "/workers", "/skills", "/mcp",
    "/decision", "/compact", "/quit",
]

def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")

def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {} if default is None else default

def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)

def repo_key(repo: Path) -> str:
    return hashlib.sha1(str(repo).encode()).hexdigest()[:12]

def pointer_path(repo: Path) -> Path:
    return DATA_ROOT / f"last-session-{repo_key(repo)}.txt"

def make_session(repo: Path) -> tuple[Path, dict[str, Any]]:
    SESSIONS_ROOT.mkdir(parents=True, exist_ok=True)
    base = f"{repo.name}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    path = SESSIONS_ROOT / base
    i = 2
    while path.exists():
        path = SESSIONS_ROOT / f"{base}-{i}"
        i += 1
    path.mkdir(parents=True)
    state = {
        "session_id": path.name,
        "repo": str(repo),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "router_log": True,
        "model_override": "auto",
    }
    save_json(path / "state.json", state)
    (path / "transcript.jsonl").touch()
    (path / "history").touch()
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    pointer_path(repo).write_text(str(path) + "\n")
    return path, state

def open_session(repo: Path, force_new: bool = False) -> tuple[Path, dict[str, Any]]:
    p = pointer_path(repo)
    if not force_new and p.exists():
        try:
            path = Path(p.read_text().strip())
            state = load_json(path / "state.json", {})
            if path.exists() and state.get("repo") == str(repo):
                state.setdefault("router_log", True)
                state.setdefault("model_override", "auto")
                return path, state
        except Exception:
            pass
    return make_session(repo)

def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    save_json(path / "state.json", state)

def append_message(path: Path, role: str, content: str, **extra: Any) -> None:
    rec = {"ts": now_iso(), "role": role, "content": content, **extra}
    with (path / "transcript.jsonl").open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def read_messages(path: Path) -> list[dict[str, Any]]:
    out = []
    try:
        for line in (path / "transcript.jsonl").read_text().splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return out

def obvious_fresh_message(text: str) -> bool:
    t = text.strip().lower()
    return bool(re.fullmatch(
        r"(안녕+|ㅎㅇ+|하이+|hello|hi|hey|고마워+|감사+|ㅇㅇ+|오케이+|ok|okay)[.!?~ ]*",
        t,
    ))

def format_context(path: Path, current: str, cfg: dict[str, Any]) -> str:
    if obvious_fresh_message(current):
        return "(none — current message is standalone/casual)"

    chat_cfg = cfg.get("chat", {})
    max_messages = int(chat_cfg.get("recent_messages", 10))
    max_chars = int(chat_cfg.get("max_context_chars", 32000))

    parts = []
    summary = path / "summary.md"
    if summary.exists() and summary.read_text().strip():
        parts.append("OLDER CONVERSATION SUMMARY:\n" + summary.read_text().strip())

    messages = read_messages(path)[-max_messages:]
    total = sum(len(x) for x in parts)

    recent = []
    for rec in reversed(messages):
        role = str(rec.get("role", "?")).upper()
        content = str(rec.get("content", "")).strip()
        if not content:
            continue
        block = f"{role}:\n{content}"
        if total + len(block) > max_chars and recent:
            break
        recent.append(block)
        total += len(block)

    if recent:
        parts.append("RECENT TURNS:\n" + "\n\n".join(reversed(recent)))
    return "\n\n".join(parts) if parts else "(none)"

def build_task(repo: Path, session_path: Path, current: str, cfg: dict[str, Any]) -> str:
    context = format_context(session_path, current, cfg)
    return f"""You are continuing a persistent user conversation through ai-chat.

Repository:
{repo}

Conversation history is context only, not authoritative for mutable Git/evidence state.
The CURRENT USER MESSAGE is authoritative for what the user wants now.

Continuity rules:
- answer CURRENT USER MESSAGE first
- only use prior context when it is actually relevant
- do not resume unfinished work merely because it exists
- greetings/casual messages do not imply continuation
- preserve prior constraints only when still relevant
- verify mutable project facts directly

CONVERSATION CONTEXT:
{context}

CURRENT USER MESSAGE:
{current}

Respond naturally to the current message.
""".strip()

def effective_quota() -> tuple[float | None, float | None, int | None]:
    data = load_json(CACHE, {})
    q = data.get("quota", {}) if isinstance(data, dict) else {}

    def score(prefix: str) -> float | None:
        vals = []
        for key, value in q.items():
            if str(key).startswith(prefix) and isinstance(value, dict):
                try:
                    vals.append(float(value["remaining_fraction"]))
                except Exception:
                    pass
        return min(vals) if vals else None

    at = data.get("_quota_captured_at_unix", data.get("_captured_at_unix"))
    try:
        age = int(time.time() - float(at))
    except Exception:
        age = None
    return score("3p-"), score("gemini-"), age

def fmt_pct(x: float | None) -> str:
    return "?" if x is None else f"{x*100:.0f}%"

def toolbar(state: dict[str, Any]) -> HTML:
    third, gemini, _age = effective_quota()
    router = load_json(ROUTER_STATE, {})
    last = router.get("last_success") if isinstance(router.get("last_success"), dict) else {}
    last_name = last.get("model") or last.get("backend") or "-"
    override = state.get("model_override", "auto")

    codex = _usage_cache("codex-usage.json")
    cmd = _usage_cache("cmd-usage.json")

    c5 = _pct(codex.get("five_hour_remaining_pct")) if codex.get("ok") else "-"
    cw = _pct(codex.get("weekly_remaining_pct")) if codex.get("ok") else "-"
    m5 = _pct(cmd.get("five_hour_remaining_pct")) if cmd.get("ok") else "-"
    mw = _pct(cmd.get("weekly_remaining_pct")) if cmd.get("ok") else "-"

    return HTML(
        f" <b>{override.upper()}</b>  "
        f"last:{last_name}  "
        f"C:{c5}/{cw}  "
        f"3P:{fmt_pct(third)}  "
        f"G:{fmt_pct(gemini)}  "
        f"CMD:{m5}/{mw} "
    )

def run_capture(argv: list[str], cwd: Path, timeout: int = 15) -> str:
    try:
        r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        text = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
        return text.strip() or f"(exit {r.returncode}, no output)"
    except Exception as e:
        return f"(unavailable: {e})"

def run_ai_orch(repo: Path, task: str, current_user: str, state: dict[str, Any]) -> tuple[int, str, str, str | None, str | None]:
    env = os.environ.copy()
    env["AI_ORCH_REPO"] = str(repo)
    env["AI_ORCH_USER_MESSAGE"] = current_user
    permission_profile = str(state.get("permission_profile", "trusted")).lower()
    env["AI_ORCH_PERMISSION_PROFILE"] = permission_profile if permission_profile in {"trusted", "guarded"} else "trusted"
    override = str(state.get("model_override", "auto"))
    if override and override != "auto":
        env["AI_ORCH_FORCE_MODEL"] = override
    else:
        env.pop("AI_ORCH_FORCE_MODEL", None)

    proc = subprocess.Popen(
        ["ai-orch"], cwd=repo, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
        start_new_session=True,
    )
    stderr_lines: list[str] = []
    stdout_parts: list[str] = []

    def read_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)
            if state.get("router_log", True):
                print(line.rstrip(), file=sys.stderr, flush=True)

    t = threading.Thread(target=read_stderr, daemon=True)
    t.start()

    try:
        assert proc.stdin is not None
        proc.stdin.write(task)
        proc.stdin.close()

        assert proc.stdout is not None
        for chunk in iter(lambda: proc.stdout.read(1024), ""):
            if not chunk:
                break
            stdout_parts.append(chunk)

        rc = proc.wait()

    except KeyboardInterrupt:
        # ai-orch runs in its own process group. Terminate the whole tree
        # (ai-orch + current backend/micro-judge) without forwarding SIGINT,
        # so Python children do not dump KeyboardInterrupt tracebacks.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=1)
            except Exception:
                pass

        t.join(timeout=1)
        raise

    finally:
        # Avoid leaving a live stdin pipe behind on interrupted turns.
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass

    t.join(timeout=1)
    stderr_text = "".join(stderr_lines)
    response = "".join(stdout_parts)

    main = None
    task_status = None
    for line in stderr_text.splitlines():
        if "[ai-orch] selected MAIN:" in line:
            main = line.split("[ai-orch] selected MAIN:", 1)[1].strip()
        elif "[ai-orch] escalation accepted:" in line:
            main = line.split("[ai-orch] escalation accepted:", 1)[1].strip()
        elif "[ai-orch] FINAL_TASK_STATUS:" in line:
            task_status = line.split("[ai-orch] FINAL_TASK_STATUS:", 1)[1].strip()
        elif "[ai-orch] TASK_STATUS:" in line:
            task_status = line.split("[ai-orch] TASK_STATUS:", 1)[1].strip()
    return rc, response, stderr_text, main, task_status

def compact_session(path: Path, cfg: dict[str, Any], cwd: Path) -> bool:
    msgs = read_messages(path)
    raw = "\n\n".join(
        f"{m.get('role','?').upper()}:\n{m.get('content','')}" for m in msgs
    )
    if len(raw) < 2000:
        print("대화가 아직 짧아서 compact할 필요가 없습니다.")
        return False

    model = cfg.get("decision", {}).get("judge_model", "mimo-v2.5-pro")
    prompt = f"""Summarize this conversation for future AI agents.
Preserve only durable user constraints, decisions, completed work, current state,
unresolved questions, and references needed to continue. Do not invent facts.
Do not treat mutable repository state as permanently true; label it as last observed.

CONVERSATION:
{raw[-100000:]}
"""
    try:
        r = subprocess.run(
            ["cmd", "-p", prompt, "--plan", "--skip-onboarding",
             "-m", model, "--effort", "low"],
            cwd=cwd, capture_output=True, text=True, timeout=180,
        )
    except Exception as e:
        print(f"compact 실패: {e}")
        return False
    if r.returncode != 0 or not r.stdout.strip():
        print(f"compact 실패 (exit {r.returncode})")
        return False
    (path / "summary.md").write_text(r.stdout.strip() + "\n")
    print("summary.md 갱신 완료")
    return True

def maybe_auto_compact(path: Path, cfg: dict[str, Any], cwd: Path) -> None:
    threshold = int(cfg.get("chat", {}).get("auto_compact_chars", 120000))
    try:
        size = (path / "transcript.jsonl").stat().st_size
    except Exception:
        return
    if size < threshold:
        return
    summary = path / "summary.md"
    if summary.exists() and time.time() - summary.stat().st_mtime < 3600:
        return
    compact_session(path, cfg, cwd)


def _usage_cache(name: str) -> dict[str, Any]:
    return load_json(Path.home() / f".cache/orchbridge/{name}", {})

def _probe_usage(binary: str, repo: Path) -> dict[str, Any]:
    try:
        r = subprocess.run(
            [str(Path.home() / ".local/bin" / binary)],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.stdout.strip():
            return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as e:
        return {"ok": False, "reason": str(e)}
    return {"ok": False, "reason": f"{binary} returned no data"}

def _pct(v: Any) -> str:
    try:
        return f"{float(v):.0f}%"
    except Exception:
        return "?"

def _fmt_reset_seconds(seconds: Any) -> str | None:
    try:
        sec = max(0, int(float(seconds)))
    except Exception:
        return None
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, _ = divmod(sec, 60)
    if d:
        return f"in {d}d {h}h"
    if h:
        return f"in {h}h {m}m"
    return f"in {m}m"

def _agy_window(key: str) -> tuple[float | None, str | None]:
    data = load_json(CACHE, {})
    q = data.get("quota", {}) if isinstance(data, dict) else {}
    item = q.get(key)
    if not isinstance(item, dict):
        return None, None
    try:
        remaining = float(item.get("remaining_fraction"))
    except Exception:
        remaining = None

    reset = None
    if item.get("reset_in_seconds") is not None:
        reset = _fmt_reset_seconds(item.get("reset_in_seconds"))
    if not reset and item.get("reset_time"):
        reset = str(item.get("reset_time"))
    return remaining, reset

def _line(label: str, remaining: Any, reset: Any) -> str:
    left = _pct(remaining)
    suffix = f" · resets {reset}" if reset else ""
    return f"  {label:<8} {left:>4} left{suffix}"

def _cooldown(router: dict[str, Any], key: str) -> str | None:
    entry = router.get("blocked_until", {}).get(key)
    if not isinstance(entry, dict):
        return None
    try:
        sec = int(float(entry.get("until", 0)) - time.time())
    except Exception:
        return None
    if sec <= 0:
        return None
    human = _fmt_reset_seconds(sec) or f"in {sec}s"
    return f"{human.removeprefix('in ')} cooldown"

def print_quota_dashboard(repo: Path) -> None:
    codex = _probe_usage("codex-usage-snapshot", repo)
    cmd = _probe_usage("cmd-usage-snapshot", repo)
    router = load_json(ROUTER_STATE, {})

    a3_5, a3_5r = _agy_window("3p-5h")
    a3_w, a3_wr = _agy_window("3p-weekly")
    ag_5, ag_5r = _agy_window("gemini-5h")
    ag_w, ag_wr = _agy_window("gemini-weekly")
    _, _, age = effective_quota()

    print("Quota / reset")
    print("-------------")

    print("Codex")
    if codex.get("ok"):
        print(_line("5h", codex.get("five_hour_remaining_pct"), codex.get("five_hour_reset")))
        print(_line("weekly", codex.get("weekly_remaining_pct"), codex.get("weekly_reset")))
        if codex.get("credits"):
            print(f"  credits  {codex['credits']}")
    else:
        cd = _cooldown(router, "codex")
        print(f"  exact quota unavailable" + (f" · {cd}" if cd else ""))
        if codex.get("reason"):
            print(f"  reason   {codex['reason']}")

    print("AGY Claude/GPT")
    print(_line("5h", None if a3_5 is None else a3_5 * 100, a3_5r))
    print(_line("weekly", None if a3_w is None else a3_w * 100, a3_wr))

    print("AGY Gemini")
    print(_line("5h", None if ag_5 is None else ag_5 * 100, ag_5r))
    print(_line("weekly", None if ag_w is None else ag_w * 100, ag_wr))

    print("Command Code")
    if cmd.get("ok"):
        print(_line("5h", cmd.get("five_hour_remaining_pct"), cmd.get("five_hour_reset")))
        print(_line("weekly", cmd.get("weekly_remaining_pct"), cmd.get("weekly_reset")))
    else:
        cd = _cooldown(router, "commandcode")
        print(f"  exact quota unavailable" + (f" · {cd}" if cd else ""))
        if cmd.get("reason"):
            print(f"  reason   {cmd['reason']}")

    if age is not None:
        print(f"AGY snapshot age: {age}s")

def print_help() -> None:
    print("""Commands
/help                 도움말
/status               repo/session/router 상태
/quota                AGY quota
/history              최근 대화
/session              세션 경로
/new                  새 대화
/model auto|cmd|codex|sonnet|opus|claude|claude-opus|gemini-low|gemini-medium|gemini-high
/router on|off         라우팅 로그
/router reset          cooldown/last-success 초기화
/decision              최근 routing 판단
/agents                provider health / cooldown state
/workers               recent orchestrator delegates
/skills                backend별 project skills
/mcp                   backend별 MCP 상태
/compact               오래된 대화 요약
/quit                  종료

입력:
Enter      전송
Esc+Enter  줄바꿈
↑/↓        입력 history
""")

def main() -> None:
    cfg = load_json(CONFIG, {})
    repo = Path(os.environ.get(
        "AI_ORCH_REPO",
        cfg.get("default_repo", os.getcwd()),
    )).expanduser().resolve()
    session_path, state = open_session(repo)

    kb = KeyBindings()

    @kb.add("escape", "enter")
    def _(event):
        event.current_buffer.insert_text("\n")

    @kb.add("enter")
    def _(event):
        event.current_buffer.validate_and_handle()

    completer = WordCompleter(COMMANDS, sentence=True, ignore_case=True)
    prompt_session = PromptSession(
        history=FileHistory(str(session_path / "history")),
        completer=completer,
        complete_while_typing=True,
        multiline=True,
        key_bindings=kb,
        prompt_continuation=lambda width, line_number, wrap_count: "... ",
    )

    print("OrchBridge CLI")
    print(f"repo: {repo}")
    print(f"session: {session_path.name}")
    print("/help 로 명령어 확인")

    while True:
        try:
            with patch_stdout(raw=True):
                raw = prompt_session.prompt(
                    HTML("<b>You &gt;</b> "),
                    bottom_toolbar=lambda: toolbar(state),
                ).strip()
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            continue

        if not raw:
            continue

        if raw in ("/quit", "/exit"):
            break
        if raw == "/help":
            print_help()
            continue
        if raw == "/session":
            print(session_path)
            continue
        if raw == "/quota":
            print_quota_dashboard(repo)
            continue
        if raw == "/history":
            for rec in read_messages(session_path)[-10:]:
                print(f"\n[{rec.get('role','?')}]")
                print(str(rec.get("content", "")).strip())
            print()
            continue
        if raw == "/new":
            session_path, state = make_session(repo)
            prompt_session = PromptSession(
                history=FileHistory(str(session_path / "history")),
                completer=completer, complete_while_typing=True,
                multiline=True, key_bindings=kb,
                prompt_continuation=lambda width, line_number, wrap_count: "... ",
            )
            print(f"new session: {session_path.name}")
            continue
        if raw.startswith("/router "):
            arg = raw.split(None, 1)[1].strip().lower()
            if arg == "reset":
                try:
                    ROUTER_STATE.unlink()
                except FileNotFoundError:
                    pass
                print("router state reset")
            elif arg in ("on", "off"):
                state["router_log"] = arg == "on"
                save_state(session_path, state)
                print(f"router log {arg}")
            else:
                print("usage: /router on|off|reset")
            continue
        if raw.startswith("/model "):
            name = raw.split(None, 1)[1].strip().lower()
            allowed = {
                "auto", "cmd", "codex", "sonnet", "opus", "claude", "claude-opus",
                "gemini-low", "gemini-medium", "gemini-high",
            }
            if name not in allowed:
                print("unknown model override")
            else:
                state["model_override"] = name
                save_state(session_path, state)
                print(f"model override: {name}")
            continue
        if raw == "/decision":
            router = load_json(ROUTER_STATE, {})
            print(json.dumps(router.get("last_decision"), ensure_ascii=False, indent=2))
            continue
        if raw == "/agents":
            print(run_capture([str(HOME / ".local/bin/orch-health")], repo))
            continue
        if raw == "/workers":
            print(run_capture([str(HOME / ".local/bin/orch-delegations"), "--limit", "20"], repo))
            continue
        if raw == "/skills":
            roots = [
                (".agents", repo / ".agents/skills"),
                (".codex", repo / ".codex/skills"),
                (".commandcode", repo / ".commandcode/skills"),
            ]
            for label, root in roots:
                names = sorted(
                    p.name for p in root.glob("*")
                    if p.is_dir() and (p / "SKILL.md").exists()
                ) if root.exists() else []
                print(f"{label}: {', '.join(names) if names else '(none)'}")
            continue
        if raw == "/mcp":
            print("=== Codex ===")
            print(run_capture(["codex", "mcp", "list"], repo))
            print("\n=== Antigravity ===")
            print(run_capture(["agy", "mcp", "list"], repo))
            print("\n=== Command Code ===")
            print(run_capture(["cmd", "mcp", "list"], repo))
            continue
        if raw == "/compact":
            compact_session(session_path, cfg, repo)
            continue
        if raw == "/status":
            print(f"repo: {repo}")
            print(f"session: {session_path.name}")
            print(f"model override: {state.get('model_override','auto')}")
            print(run_capture(["git", "status", "--short", "--branch"], repo))
            print(json.dumps(load_json(ROUTER_STATE, {}), ensure_ascii=False, indent=2))
            continue

        previous = read_messages(session_path)
        append_message(session_path, "user", raw)
        task = build_task(repo, session_path, raw, cfg)

        print("\nAssistant >")
        with patch_stdout(raw=True):
            try:
                rc, response, stderr_text, main_name, task_status = run_ai_orch(repo, task, raw, state)
            except KeyboardInterrupt:
                print("\nInterrupted.")
                continue

        if response:
            print(response.rstrip())
            append_message(
                session_path, "assistant", response,
                main=main_name, exit_code=rc, task_status=task_status,
            )
        else:
            print(f"(no response; ai-orch exit={rc})")
            append_message(
                session_path, "assistant", f"(no response; exit={rc})",
                main=main_name, exit_code=rc, task_status=task_status,
                router_error=stderr_text[-4000:],
            )

        shown_task = task_status or ("FAILED" if rc != 0 else "UNKNOWN")
        print(
            f"\n[main: {main_name or 'unknown'} | "
            f"process: {rc} | task: {shown_task}]\n"
        )
        save_state(session_path, state)
        maybe_auto_compact(session_path, cfg, repo)

if __name__ == "__main__":
    main()

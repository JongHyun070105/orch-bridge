#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

from platform_support import login_shell_command

HOME = Path.home()
BASE = HOME / ".local/share/orchbridge"
GLOBAL_DIR = BASE / "global"
PROJECTS_DIR = BASE / "projects"
REGISTRY = GLOBAL_DIR / "workspaces.json"
PROJECT_CONFIG = GLOBAL_DIR / "project-config.json"

MASTER_SESSION = os.environ.get("AI_ORCH_MASTER_SESSION", "orch")
SHELL_WINDOW = 0
OPS_WINDOW = 9
PRIMARY_PROJECT_SLOTS = list(range(1, 9))
GLOBAL_DIR.mkdir(parents=True, exist_ok=True)
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)


def load_registry() -> dict[str, Any]:
    try:
        data = json.loads(REGISTRY.read_text())
        if isinstance(data, dict) and isinstance(data.get("workspaces"), dict):
            return data
    except Exception:
        pass
    return {"version": 2, "master_session": MASTER_SESSION, "workspaces": {}}


def save_registry(data: dict[str, Any]) -> None:
    data["version"] = 2
    data["master_session"] = MASTER_SESSION
    atomic_json(REGISTRY, data)


def run(argv: list[str], *, cwd: Path | None = None, timeout: int = 10, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


def tmux_has_session(session: str = MASTER_SESSION) -> bool:
    if not tmux_available():
        return False
    return subprocess.run(
        ["tmux", "has-session", "-t", session],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def git_root(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.is_file():
        path = path.parent
    try:
        p = run(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
        if p.returncode == 0 and p.stdout.strip():
            return Path(p.stdout.strip()).resolve()
    except Exception:
        pass
    return path


def slugify(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_text = normalized.encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return slug[:36] or "project"


def workspace_id(repo: Path) -> str:
    digest = hashlib.sha1(str(repo).encode()).hexdigest()[:8]
    return f"{slugify(repo.name)}-{digest}"


def project_base(wid: str) -> Path:
    return PROJECTS_DIR / wid


def ensure_project_dirs(wid: str) -> Path:
    base = project_base(wid)
    for name in ("jobs", "delegations", "worktrees"):
        (base / name).mkdir(parents=True, exist_ok=True)
    return base



def load_project_config() -> dict[str, Any]:
    default_root = HOME / "Projects"
    try:
        data = json.loads(PROJECT_CONFIG.read_text())
        if isinstance(data, dict):
            root = Path(str(data.get("project_root") or default_root)).expanduser()
            return {"project_root": str(root.resolve())}
    except Exception:
        pass
    return {"project_root": str(default_root.resolve())}


def save_project_config(data: dict[str, Any]) -> None:
    root = Path(str(data["project_root"])).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    atomic_json(PROJECT_CONFIG, {"project_root": str(root)})


def project_root() -> Path:
    root = Path(load_project_config()["project_root"]).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def auto_alias(name: str) -> str:
    """Compact tmux label; repository/workspace names are unchanged."""
    original = name.strip()
    text = re.sub(r"[\s_]+", "-", original).strip("-")
    parts = [p for p in text.split("-") if p]
    common_suffixes = {
        "flutter", "android", "ios", "app", "application",
        "project", "repo", "repository", "frontend", "backend",
    }
    while len(parts) > 1 and parts[-1].casefold() in common_suffixes:
        parts.pop()
    candidate = "-".join(parts) or original
    if len(candidate) <= 11:
        return candidate
    if len(parts) >= 2 and len(parts[0]) >= 5:
        return parts[0][:11]
    return candidate[:11].rstrip("-_")


def unique_alias(candidate: str, data: dict[str, Any], wid: str) -> str:
    used = {
        str(w.get("display_name", "")).casefold()
        for key, w in data["workspaces"].items()
        if key != wid
    }
    if candidate.casefold() not in used:
        return candidate
    base = candidate[:8] or "project"
    for n in range(2, 100):
        alias = f"{base}-{n}"
        if alias.casefold() not in used:
            return alias
    return f"{base}-{wid[-2:]}"


def display_name(repo: Path, wid: str) -> str:
    data = load_registry()
    return unique_alias(auto_alias(repo.name), data, wid)


def used_preferred_indices(data: dict[str, Any], exclude: str | None = None) -> set[int]:
    """Reserve only explicitly pinned slots plus currently live tmux windows."""
    out: set[int] = set()
    for wid, w in data["workspaces"].items():
        if wid == exclude or not bool(w.get("slot_pinned", False)):
            continue
        try:
            idx = int(w.get("window_index"))
        except Exception:
            continue
        if idx not in {SHELL_WINDOW, OPS_WINDOW}:
            out.add(idx)
    try:
        for row in tmux_windows():
            idx = int(row.get("index", -1))
            if idx not in {SHELL_WINDOW, OPS_WINDOW}:
                out.add(idx)
    except Exception:
        pass
    return out

def allocate_index(data: dict[str, Any], exclude: str | None = None) -> int:
    used = used_preferred_indices(data, exclude=exclude)
    for idx in PRIMARY_PROJECT_SLOTS:
        if idx not in used:
            return idx
    idx = 10
    while idx in used or idx == OPS_WINDOW:
        idx += 1
    return idx


def register(repo_input: str | Path, *, preferred_index: int | None = None) -> dict[str, Any]:
    repo = git_root(Path(repo_input))
    if not repo.exists() or not repo.is_dir():
        raise SystemExit(f"project path does not exist: {repo}")

    data = load_registry()
    wid = workspace_id(repo)
    old = data["workspaces"].get(wid, {})

    if preferred_index is None:
        try:
            idx = int(old.get("window_index"))
        except Exception:
            idx = allocate_index(data, exclude=wid)
    else:
        idx = int(preferred_index)

    if idx in {SHELL_WINDOW, OPS_WINDOW}:
        raise SystemExit(f"window {idx} is reserved")

    owner = [
        k for k, v in data["workspaces"].items()
        if k != wid and int(v.get("window_index", -1)) == idx
    ]
    if owner:
        idx = allocate_index(data, exclude=wid)

    entry = {
        "id": wid,
        "name": repo.name,
        "display_name": (
            old.get("display_name")
            if old.get("display_name_custom")
            else display_name(repo, wid)
        ),
        "display_name_custom": bool(old.get("display_name_custom", False)),
        "repo": str(repo),
        "project_base": str(ensure_project_dirs(wid)),
        "window_index": idx,
        "slot_pinned": bool(old.get("slot_pinned", False)),
        "created_at": old.get("created_at") or time.time(),
        "last_opened_at": old.get("last_opened_at"),
    }
    data["workspaces"][wid] = entry
    save_registry(data)
    return entry


def resolve_registered_query(query: str) -> dict[str, Any]:
    """Resolve only an already-registered workspace; never register paths as a side effect."""
    data = load_registry()
    rows = list(data["workspaces"].values())
    q = str(query or "").strip().casefold()
    if not q:
        raise SystemExit("project query is required")

    exact = [
        w for w in rows
        if q in {
            str(w.get("id", "")).casefold(),
            str(w.get("name", "")).casefold(),
            str(w.get("display_name", "")).casefold(),
            str(w.get("repo", "")).casefold(),
            str(w.get("window_index", "")).casefold(),
        }
    ]
    if len(exact) == 1:
        return exact[0]

    partial = [
        w for w in rows
        if q in str(w.get("id", "")).casefold()
        or q in str(w.get("name", "")).casefold()
        or q in str(w.get("display_name", "")).casefold()
        or q in str(w.get("repo", "")).casefold()
    ]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise SystemExit(
            f"unknown registered project {query!r}. Use 'orch-project list'."
        )
    raise SystemExit(
        f"ambiguous project {query!r}: "
        + ", ".join(str(x.get("name")) for x in partial[:8])
    )


def resolve_query(query: str) -> dict[str, Any]:
    candidate = Path(query).expanduser()
    if candidate.exists():
        return register(candidate)
    return resolve_registered_query(query)


def tmux_windows() -> list[dict[str, Any]]:
    if not tmux_has_session():
        return []
    fmt = (
        "#{window_index}\t#{window_name}\t"
        "#{@ai_orch_workspace_id}\t#{@ai_orch_repo}\t#{pane_current_command}"
    )
    p = run(["tmux", "list-windows", "-t", MASTER_SESSION, "-F", fmt])
    if p.returncode != 0:
        return []
    rows = []
    for line in p.stdout.splitlines():
        parts = line.split("\t")
        while len(parts) < 5:
            parts.append("")
        try:
            idx = int(parts[0])
        except Exception:
            continue
        rows.append({
            "index": idx,
            "name": parts[1],
            "workspace_id": parts[2],
            "repo": parts[3],
            "command": parts[4],
        })
    return rows


def window_for_workspace(wid: str) -> dict[str, Any] | None:
    for row in tmux_windows():
        if row.get("workspace_id") == wid:
            return row
    return None


def update_registry_entry(entry: dict[str, Any]) -> None:
    data = load_registry()
    data["workspaces"][entry["id"]] = entry
    save_registry(data)


def shell_env(entry: dict[str, Any]) -> dict[str, str]:
    return {
        "AI_ORCH_REPO": str(entry["repo"]),
        "AI_ORCH_WORKSPACE_ID": str(entry["id"]),
        "AI_ORCH_PROJECT_BASE": str(entry["project_base"]),
        "AI_ORCH_PROJECT_NAME": str(entry["name"]),
        "AI_ORCH_MASTER_SESSION": MASTER_SESSION,
    }


def command_with_env(entry: dict[str, Any], command: str) -> str:
    import shlex
    prefix = " ".join(
        f"{key}={shlex.quote(value)}" for key, value in shell_env(entry).items()
    )
    return f"env {prefix} {command}"


def force_window_zero(session: str) -> None:
    p = run(["tmux", "list-windows", "-t", session, "-F", "#I"])
    rows = [x.strip() for x in p.stdout.splitlines() if x.strip()]
    if not rows:
        return
    first = rows[0]
    if first != "0":
        run(["tmux", "move-window", "-s", f"{session}:{first}", "-t", f"{session}:0"], check=True)



def configure_tmux_modified_keys() -> None:
    # Best-effort tmux passthrough for modified Enter / CSI-u keys.
    if not tmux_available():
        return

    attempts = [
        ["tmux", "set-option", "-s", "extended-keys", "always"],
        ["tmux", "set-option", "-s", "extended-keys-format", "csi-u"],
        ["tmux", "set-option", "-s", "xterm-keys", "on"],
    ]
    for argv in attempts:
        try:
            p = run(argv)
            if p.returncode != 0 and argv[-2:] == ["extended-keys", "always"]:
                run(["tmux", "set-option", "-s", "extended-keys", "on"])
        except Exception:
            pass

    try:
        p = run(["tmux", "show-option", "-sqv", "terminal-features"])
        current = p.stdout if p.returncode == 0 else ""
        for feature in (
            "xterm*:extkeys",
            "screen*:extkeys",
            "tmux*:extkeys",
            "rxvt*:extkeys",
            "alacritty*:extkeys",
            "wezterm*:extkeys",
            "foot*:extkeys",
        ):
            if feature not in current:
                run(["tmux", "set-option", "-as", "terminal-features", "," + feature])
                current += "," + feature
    except Exception:
        pass

def ensure_master_session(cwd: Path | None = None) -> None:
    if not tmux_available():
        raise SystemExit("tmux is not installed or not on PATH")
    if tmux_has_session():
        configure_tmux_modified_keys()
        return

    start_dir = str((cwd or HOME).expanduser().resolve())
    run([
        "tmux", "new-session", "-d", "-s", MASTER_SESSION,
        "-n", "shell", "-c", start_dir, login_shell_command(),
    ], check=True)
    force_window_zero(MASTER_SESSION)
    configure_tmux_modified_keys()
    run(["tmux", "set-option", "-t", MASTER_SESSION, "base-index", "0"], check=True)
    run(["tmux", "set-window-option", "-t", f"{MASTER_SESSION}:0", "pane-base-index", "0"], check=True)

    run([
        "tmux", "new-window", "-d", "-t", f"{MASTER_SESSION}:{OPS_WINDOW}",
        "-n", "ops", "-c", str(HOME), f"ai-orch-dashboard; {login_shell_command()}",
    ], check=True)


def choose_runtime_index(entry: dict[str, Any]) -> int:
    preferred = int(entry.get("window_index", 1))
    occupied = {int(x["index"]): x for x in tmux_windows()}
    if str(occupied.get(preferred, {}).get("workspace_id") or "") == entry["id"]:
        return preferred
    if bool(entry.get("slot_pinned", False)):
        if preferred in occupied:
            raise SystemExit(
                f"pinned window {preferred} is occupied; "
                "use `orch-project slot <project> <other|auto>`"
            )
        return preferred

    used = set(occupied)
    data = load_registry()
    for wid, other in data["workspaces"].items():
        if wid == entry.get("id") or not bool(other.get("slot_pinned", False)):
            continue
        try:
            used.add(int(other.get("window_index")))
        except Exception:
            pass
    for idx in PRIMARY_PROJECT_SLOTS:
        if idx not in used:
            return idx
    idx = 10
    while idx in used or idx == OPS_WINDOW:
        idx += 1
    return idx


PROJECT_SHELL_COMMANDS = {"zsh", "bash", "sh", "fish", "dash", "ksh", "nu"}


def project_window_needs_tui_restart(row: dict[str, Any]) -> bool:
    command = str(row.get("command") or "").strip().casefold()
    return command in PROJECT_SHELL_COMMANDS


def restart_project_tui(entry: dict[str, Any], row: dict[str, Any]) -> None:
    idx = int(row["index"])
    run([
        "tmux", "respawn-pane", "-k", "-t", f"{MASTER_SESSION}:{idx}",
        command_with_env(entry, f"ai-chat-tui; {login_shell_command()}"),
    ], check=True)
    run([
        "tmux", "set-option", "-w", "-t", f"{MASTER_SESSION}:{idx}",
        "@ai_orch_workspace_id", str(entry["id"]),
    ], check=True)
    run([
        "tmux", "set-option", "-w", "-t", f"{MASTER_SESSION}:{idx}",
        "@ai_orch_repo", str(entry["repo"]),
    ], check=True)


def ensure_project_window(entry: dict[str, Any]) -> int:
    ensure_master_session(Path(entry["repo"]))
    existing = window_for_workspace(str(entry["id"]))
    if existing:
        if project_window_needs_tui_restart(existing):
            restart_project_tui(entry, existing)
        return int(existing["index"])

    idx = choose_runtime_index(entry)
    repo = str(entry["repo"])
    name = str(entry.get("display_name") or entry["name"])

    run([
        "tmux", "new-window", "-d", "-t", f"{MASTER_SESSION}:{idx}",
        "-n", name, "-c", repo,
        command_with_env(entry, f"ai-chat-tui; {login_shell_command()}"),
    ], check=True)
    run([
        "tmux", "set-option", "-w", "-t", f"{MASTER_SESSION}:{idx}",
        "@ai_orch_workspace_id", str(entry["id"]),
    ], check=True)
    run([
        "tmux", "set-option", "-w", "-t", f"{MASTER_SESSION}:{idx}",
        "@ai_orch_repo", repo,
    ], check=True)

    if int(entry.get("window_index", idx)) != idx:
        entry = dict(entry)
        entry["window_index"] = idx
        update_registry_entry(entry)
    return idx


def touch_opened(entry: dict[str, Any]) -> None:
    data = load_registry()
    wid = str(entry["id"])
    if wid in data["workspaces"]:
        data["workspaces"][wid]["last_opened_at"] = time.time()
        save_registry(data)


def select_project_window(idx: int, *, attach: bool = True) -> None:
    run(["tmux", "select-window", "-t", f"{MASTER_SESSION}:{idx}"], check=True)
    if not attach:
        return

    if os.environ.get("TMUX"):
        current = run(["tmux", "display-message", "-p", "#S"]).stdout.strip()
        if current != MASTER_SESSION:
            run(["tmux", "switch-client", "-t", MASTER_SESSION], check=True)
    else:
        os.execvp("tmux", ["tmux", "attach", "-t", MASTER_SESSION])


def open_project(query: str | None, *, preferred_index: int | None = None, attach: bool = True) -> dict[str, Any]:
    if query is None:
        entry = register(Path.cwd(), preferred_index=preferred_index)
    else:
        candidate = Path(query).expanduser()
        if candidate.exists():
            entry = register(candidate, preferred_index=preferred_index)
        else:
            entry = resolve_query(query)
            if preferred_index is not None:
                entry = register(entry["repo"], preferred_index=preferred_index)

    idx = ensure_project_window(entry)
    touch_opened(entry)
    print(
        f"project {entry['name']} · window {idx}\n"
        f"repo    {entry['repo']}\n"
        f"state   {entry['project_base']}"
    )
    select_project_window(idx, attach=attach)
    return entry


def list_projects(as_json: bool = False) -> int:
    data = load_registry()
    live = {x.get("workspace_id"): x for x in tmux_windows() if x.get("workspace_id")}
    current_wid = os.environ.get("AI_ORCH_WORKSPACE_ID", "")

    rows = sorted(
        data["workspaces"].values(),
        key=lambda x: (int(x.get("window_index", 9999)), str(x.get("name", "")).casefold()),
    )
    enriched = []
    for w in rows:
        item = dict(w)
        lw = live.get(w.get("id"))
        item["live_window"] = int(lw["index"]) if lw else None
        enriched.append(item)

    if as_json:
        print(json.dumps(enriched, ensure_ascii=False, indent=2))
        return 0
    if not enriched:
        print("No registered projects. Run `cd <project> && orch`.")
        return 0

    print(f"PROJECTS · tmux session {MASTER_SESSION}")
    print("    WIN   PIN  STATE  PROJECT                   REPO")
    for w in enriched:
        mark = "*" if w.get("id") == current_wid else " "
        win = w.get("live_window")
        if win is not None:
            win_text = str(win)
        elif bool(w.get("slot_pinned", False)):
            win_text = str(w.get("window_index", "-"))
        else:
            win_text = "auto"
        state = "LIVE" if win is not None else "-"
        pin = "yes" if bool(w.get("slot_pinned", False)) else "-"
        print(f"{mark}   {win_text:<5} {pin:<4} {state:<5} {str(w.get('name')):<25} {w.get('repo')}")
    print("\nreserved: 0=shell · 9=ops")
    print("remove stale registration: orch-project delete <project>  (repo/state are preserved)")
    return 0


def current_project() -> int:
    wid = os.environ.get("AI_ORCH_WORKSPACE_ID")
    if not wid:
        print(f"No project workspace in this pane. session={MASTER_SESSION}; 0=shell · 9=ops.")
        return 1
    data = load_registry()
    entry = dict(data["workspaces"].get(wid, {}))
    if not entry:
        entry = {
            "id": wid,
            "name": os.environ.get("AI_ORCH_PROJECT_NAME"),
            "repo": os.environ.get("AI_ORCH_REPO"),
            "project_base": os.environ.get("AI_ORCH_PROJECT_BASE"),
        }
    live = window_for_workspace(wid)
    entry["live_window"] = live.get("index") if live else None
    print(json.dumps(entry, ensure_ascii=False, indent=2))
    return 0


def _copy_tree(src: Path, dst: Path) -> None:
    if src.exists():
        shutil.copytree(src, dst, dirs_exist_ok=True)


def migrate_legacy(repo_input: str | Path) -> int:
    entry = register(repo_input, preferred_index=1)
    pbase = Path(entry["project_base"])
    marker = pbase / ".legacy-v35-migrated"
    if marker.exists() or (pbase / ".legacy-v34-migrated").exists():
        print(f"legacy migration already completed: {entry['name']}")
        return 0

    _copy_tree(BASE / "jobs", pbase / "jobs")
    _copy_tree(BASE / "delegations", pbase / "delegations")
    project_health = pbase / "delegations/provider-health.json"
    if project_health.exists():
        project_health.unlink()

    legacy_tui = BASE / "tui-state.json"
    if legacy_tui.exists() and not (pbase / "tui-state.json").exists():
        shutil.copy2(legacy_tui, pbase / "tui-state.json")

    marker.write_text(json.dumps({
        "migrated_at": time.time(),
        "legacy_base": str(BASE),
        "repo": entry["repo"],
    }, ensure_ascii=False, indent=2) + "\n")
    print(f"legacy state copied into workspace: {pbase}")
    print("legacy files were left in place for rollback.")
    return 0



def set_project_root(path_input: str | None) -> int:
    if path_input is None:
        print(project_root())
        return 0
    root = Path(path_input).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    save_project_config({"project_root": str(root)})
    print(f"project root: {root}")
    return 0


def validate_new_name(name: str) -> str:
    name = name.strip()
    if not name or name in {".", ".."}:
        raise SystemExit("project name is empty")
    if "/" in name or "\\" in name:
        raise SystemExit("project name must be one folder name; use /project root to change the parent")
    if name.startswith("."):
        raise SystemExit("hidden project names are not allowed by /project new")
    return name



def initialize_git_repo(repo: Path) -> None:
    """Initialize a new orchestrator project with `main` as its unborn HEAD."""
    branch = "main"

    # Prefer Git's explicit initial-branch option so behavior is independent of
    # the user's global init.defaultBranch setting. Older Git versions may not
    # support this option, so retain a compatibility fallback.
    first = run(["git", "init", f"--initial-branch={branch}", str(repo)], timeout=15)
    if first.returncode != 0:
        fallback = run(["git", "init", str(repo)], timeout=15)
        if fallback.returncode != 0:
            detail = fallback.stderr.strip() or first.stderr.strip() or "git init failed"
            raise SystemExit(detail)
        repoint = run(
            ["git", "-C", str(repo), "symbolic-ref", "HEAD", f"refs/heads/{branch}"],
            timeout=15,
        )
        if repoint.returncode != 0:
            raise SystemExit(repoint.stderr.strip() or "failed to set initial Git branch to main")

    verify = run(["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"], timeout=15)
    if verify.returncode != 0 or verify.stdout.strip() != branch:
        detail = verify.stderr.strip() or verify.stdout.strip() or "unknown HEAD"
        raise SystemExit(f"Git initialized but initial branch is not {branch}: {detail}")

def create_project(
    name: str,
    *,
    no_git: bool = False,
    preferred_index: int | None = None,
    attach: bool = True,
) -> dict[str, Any]:
    name = validate_new_name(name)
    root = project_root()
    repo = root / name
    if repo.exists():
        raise SystemExit(
            f"project already exists: {repo}\n"
            f"use `/project open {name}` for an existing project"
        )
    repo.mkdir(parents=True)
    if not no_git:
        initialize_git_repo(repo)
    entry = register(repo, preferred_index=preferred_index)
    idx = ensure_project_window(entry)
    touch_opened(entry)
    print(
        f"created {entry['name']} · window {idx}\n"
        f"repo    {entry['repo']}\n"
        f"alias   {entry['display_name']}\n"
        f"git     {'off' if no_git else 'initialized · branch main'}"
    )
    select_project_window(idx, attach=attach)
    return entry


def set_alias(query: str | None, alias: str) -> int:
    if query:
        entry = resolve_query(query)
    else:
        wid = os.environ.get("AI_ORCH_WORKSPACE_ID")
        if not wid:
            raise SystemExit("no current project")
        data = load_registry()
        entry = data["workspaces"].get(wid)
        if not entry:
            raise SystemExit("current workspace is not registered")
    data = load_registry()
    wid = str(entry["id"])
    item = dict(data["workspaces"][wid])
    if alias.casefold() == "auto":
        value = unique_alias(auto_alias(str(item["name"])), data, wid)
        custom = False
    else:
        value = re.sub(r"\s+", "-", alias.strip())
        value = re.sub(r"[^A-Za-z0-9._-]+", "", value)[:14].strip("-_.")
        if not value:
            raise SystemExit("alias became empty after sanitization")
        value = unique_alias(value, data, wid)
        custom = True
    item["display_name"] = value
    item["display_name_custom"] = custom
    data["workspaces"][wid] = item
    save_registry(data)
    live = window_for_workspace(wid)
    if live:
        run(["tmux", "rename-window", "-t", f"{MASTER_SESSION}:{live['index']}", value], check=True)
    print(f"{item['name']} alias -> {value}")
    return 0



def _entry_for_restart(query: str | None) -> dict[str, Any]:
    if query:
        return resolve_query(query)
    wid = os.environ.get("AI_ORCH_WORKSPACE_ID")
    if wid:
        data = load_registry()
        entry = data["workspaces"].get(wid)
        if entry:
            return entry
    return register(Path.cwd())


def _restart_now(query: str, delay: float) -> int:
    if delay > 0:
        time.sleep(delay)
    entry = resolve_query(query)
    ensure_master_session(Path(entry["repo"]))
    row = window_for_workspace(str(entry["id"]))
    if row:
        restart_project_tui(entry, row)
        idx = int(row["index"])
    else:
        idx = ensure_project_window(entry)
    touch_opened(entry)
    return idx


def _spawn_restart(entry: dict[str, Any], delay: float) -> None:
    subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "_restart-now",
            str(entry["id"]),
            "--delay",
            str(max(0.0, delay)),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def restart_project(
    query: str | None = None,
    *,
    all_projects: bool = False,
    attach: bool = True,
    delay: float = 0.6,
) -> int:
    if all_projects:
        if not tmux_has_session():
            print("no live project TUI windows to restart")
            return 0
        data = load_registry()
        entries: list[dict[str, Any]] = []
        for entry in data["workspaces"].values():
            if not isinstance(entry, dict):
                continue
            if window_for_workspace(str(entry.get("id") or "")):
                entries.append(entry)
        if not entries:
            print("no live project TUI windows to restart")
            return 0
        for entry in entries:
            _spawn_restart(entry, delay)
        print(f"scheduled TUI restart for {len(entries)} live project window(s)")
        return 0

    ensure_master_session(Path.cwd())
    entry = _entry_for_restart(query)
    row = window_for_workspace(str(entry["id"]))
    if not row:
        idx = ensure_project_window(entry)
        touch_opened(entry)
        print(
            f"project {entry['name']} was closed; opened fresh TUI · window {idx}"
        )
        if attach:
            select_project_window(idx, attach=True)
        return 0

    idx = int(row["index"])
    _spawn_restart(entry, delay)
    print(
        f"scheduled TUI restart · {entry['name']} · window {idx} · "
        f"delay {max(0.0, delay):.1f}s"
    )
    if attach:
        select_project_window(idx, attach=True)
    return 0


def close_project(query: str | None = None) -> int:
    if query:
        entry = resolve_query(query)
    else:
        wid = os.environ.get("AI_ORCH_WORKSPACE_ID")
        if not wid:
            raise SystemExit("no current project window")
        data = load_registry()
        entry = data["workspaces"].get(wid)
        if not entry:
            raise SystemExit("current project is not registered")
    live = window_for_workspace(str(entry["id"]))
    if not live:
        print(f"{entry['name']}: already closed")
        return 0
    idx = int(live["index"])
    run(["tmux", "select-window", "-t", f"{MASTER_SESSION}:0"])
    run(["tmux", "kill-window", "-t", f"{MASTER_SESSION}:{idx}"], check=True)
    moves = compact_project_indices()
    print(f"closed project window: {entry['name']}")
    if moves:
        print("compacted live slots: " + ", ".join(f"{wid}:{old}->{new}" for wid, old, new in moves))
    return 0


def _project_slot_sequence(count: int, blocked: set[int] | None = None) -> list[int]:
    blocked = set(blocked or set()) | {SHELL_WINDOW, OPS_WINDOW}
    out: list[int] = []
    idx = 1
    while len(out) < count:
        if idx not in blocked:
            out.append(idx)
        idx += 1
    return out


def compact_project_indices() -> list[tuple[str, int, int]]:
    """Compact LIVE unpinned project windows while respecting pinned/manual slots."""
    data = load_registry()
    live_rows = tmux_windows()
    live_by_wid = {str(x.get("workspace_id")): x for x in live_rows if x.get("workspace_id")}
    manual_blocked = {
        int(x["index"]) for x in live_rows
        if not x.get("workspace_id") and int(x.get("index", -1)) not in {SHELL_WINDOW, OPS_WINDOW}
    }
    pinned_slots: dict[int, str] = {}
    for wid, entry in data["workspaces"].items():
        if not bool(entry.get("slot_pinned", False)):
            continue
        try:
            idx = int(entry.get("window_index"))
        except Exception:
            continue
        if idx in {SHELL_WINDOW, OPS_WINDOW}:
            continue
        if idx in pinned_slots and pinned_slots[idx] != wid:
            raise RuntimeError(f"duplicate pinned project slot {idx}")
        pinned_slots[idx] = wid
    conflict = manual_blocked & set(pinned_slots)
    if conflict:
        raise RuntimeError(f"pinned project slot {sorted(conflict)[0]} is occupied by a manual tmux window")

    live_registered: list[tuple[str, dict[str, Any], int]] = []
    for wid, row in live_by_wid.items():
        entry = data["workspaces"].get(wid)
        if isinstance(entry, dict):
            live_registered.append((wid, entry, int(row["index"])))

    desired: dict[str, int] = {}
    used = set(manual_blocked) | set(pinned_slots) | {SHELL_WINDOW, OPS_WINDOW}
    for wid, entry, _old in live_registered:
        if bool(entry.get("slot_pinned", False)):
            desired[wid] = int(entry["window_index"])

    unpinned = sorted(
        [(wid, entry, old) for wid, entry, old in live_registered if not bool(entry.get("slot_pinned", False))],
        key=lambda row: (row[2], str(row[1].get("name", "")).casefold()),
    )
    next_idx = 1
    for wid, _entry, _old in unpinned:
        while next_idx in used:
            next_idx += 1
        desired[wid] = next_idx
        used.add(next_idx)
        next_idx += 1

    moves = [
        (wid, old, desired[wid])
        for wid, _entry, old in live_registered
        if desired.get(wid, old) != old
    ]
    if moves and tmux_has_session():
        occupied = {int(x["index"]) for x in live_rows}
        temp = max(occupied | {20}) + 20
        staged: list[tuple[str, int, int]] = []
        for wid, old, target in moves:
            while temp in occupied or temp in {SHELL_WINDOW, OPS_WINDOW}:
                temp += 1
            run(["tmux", "move-window", "-s", f"{MASTER_SESSION}:{old}", "-t", f"{MASTER_SESSION}:{temp}"], check=True)
            occupied.discard(old)
            occupied.add(temp)
            staged.append((wid, temp, target))
            temp += 1
        for wid, current, target in staged:
            run(["tmux", "move-window", "-s", f"{MASTER_SESSION}:{current}", "-t", f"{MASTER_SESSION}:{target}"], check=True)

    for wid, target in desired.items():
        if wid in data["workspaces"]:
            data["workspaces"][wid]["window_index"] = target
    save_registry(data)
    return moves


def set_project_slot(query: str, value: str) -> int:
    entry = resolve_registered_query(query)
    wid = str(entry["id"])
    data = load_registry()
    current = data["workspaces"].get(wid)
    if not isinstance(current, dict):
        raise SystemExit(f"project disappeared from registry: {query!r}")
    raw = str(value).strip().lower()
    if raw == "auto":
        current["slot_pinned"] = False
        data["workspaces"][wid] = current
        save_registry(data)
        moves = compact_project_indices()
        latest = load_registry()["workspaces"][wid]
        print(f"project slot auto: {latest['name']} · window {latest.get('window_index')}")
        if moves:
            print("compacted live slots: " + ", ".join(f"{w}:{a}->{b}" for w, a, b in moves))
        return 0
    try:
        target = int(raw)
    except Exception:
        raise SystemExit("slot must be a number or 'auto'")
    if target <= 0 or target in {SHELL_WINDOW, OPS_WINDOW}:
        raise SystemExit(f"window {target} is reserved/invalid")
    for other_wid, other in data["workspaces"].items():
        if other_wid == wid or not bool(other.get("slot_pinned", False)):
            continue
        try:
            if int(other.get("window_index")) == target:
                raise SystemExit(f"window {target} is pinned by project {other.get('name')}")
        except (TypeError, ValueError):
            pass
    for row in tmux_windows():
        if int(row.get("index", -1)) == target and not row.get("workspace_id"):
            raise SystemExit(f"window {target} is occupied by a manual tmux window")
    current["window_index"] = target
    current["slot_pinned"] = True
    data["workspaces"][wid] = current
    save_registry(data)
    moves = compact_project_indices()
    print(f"project slot pinned: {current['name']} -> {target}")
    if moves:
        print("compacted live slots: " + ", ".join(f"{w}:{a}->{b}" for w, a, b in moves))
    return 0


def delete_project(query: str) -> int:
    """Delete a workspace registration only; never delete the repository or state."""
    entry = resolve_registered_query(query)
    wid = str(entry["id"])
    live = window_for_workspace(wid)
    if live:
        idx = int(live["index"])
        run(["tmux", "select-window", "-t", f"{MASTER_SESSION}:{SHELL_WINDOW}"])
        run(["tmux", "kill-window", "-t", f"{MASTER_SESSION}:{idx}"], check=True)

    data = load_registry()
    removed = data["workspaces"].pop(wid, None)
    if removed is None:
        raise SystemExit(f"project disappeared from registry: {query!r}")
    save_registry(data)

    try:
        moves = compact_project_indices()
    except Exception as exc:
        moves = []
        print(
            f"warning: project removed but window-number compaction failed: {exc}",
            file=sys.stderr,
        )

    print(f"deleted project registration: {entry['name']}")
    print(f"repo preserved:  {entry['repo']}")
    print(f"state preserved: {entry['project_base']}")
    if moves:
        summary = ", ".join(f"{wid}:{old}->{new}" for wid, old, new in moves)
        print(f"compacted project slots: {summary}")
    return 0


def master_layout() -> int:
    if not tmux_has_session():
        print(f"{MASTER_SESSION}: not running")
        return 0
    for row in tmux_windows():
        tag = row.get("workspace_id") or (
            "shell" if row["index"] == SHELL_WINDOW
            else "ops" if row["index"] == OPS_WINDOW
            else "manual"
        )
        print(f"{row['index']:>2}  {row['name']:<24} {tag}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="OrchBridge project/window manager")
    sub = ap.add_subparsers(dest="cmd")

    op = sub.add_parser("open")
    op.add_argument("query", nargs="?")
    op.add_argument("--window", type=int)
    op.add_argument("--no-attach", action="store_true")

    reg = sub.add_parser("register")
    reg.add_argument("path")
    reg.add_argument("--window", type=int)

    ls = sub.add_parser("list")
    ls.add_argument("--json", action="store_true")

    sub.add_parser("current")
    sub.add_parser("layout")

    newp = sub.add_parser("new")
    newp.add_argument("name")
    newp.add_argument("--no-git", action="store_true")
    newp.add_argument("--window", type=int)
    newp.add_argument("--no-attach", action="store_true")

    rootp = sub.add_parser("root")
    rootp.add_argument("path", nargs="?")

    aliasp = sub.add_parser("alias")
    aliasp.add_argument("alias")
    aliasp.add_argument("--project")

    closep = sub.add_parser("close")
    closep.add_argument("query", nargs="?")

    deletep = sub.add_parser("delete", aliases=["remove", "unregister"])
    deletep.add_argument("query")

    slotp = sub.add_parser("slot")
    slotp.add_argument("query")
    slotp.add_argument("value")

    restartp = sub.add_parser("restart")
    restartp.add_argument("query", nargs="?")
    restartp.add_argument("--all", action="store_true")
    restartp.add_argument("--no-attach", action="store_true")
    restartp.add_argument("--delay", type=float, default=0.6)

    restart_now = sub.add_parser("_restart-now", help=argparse.SUPPRESS)
    restart_now.add_argument("query")
    restart_now.add_argument("--delay", type=float, default=0.0)

    mig = sub.add_parser("migrate-legacy")
    mig.add_argument("path")

    ns = ap.parse_args()
    if ns.cmd in {None, "open"}:
        open_project(
            getattr(ns, "query", None),
            preferred_index=getattr(ns, "window", None),
            attach=not getattr(ns, "no_attach", False),
        )
        return 0
    if ns.cmd == "register":
        print(json.dumps(register(ns.path, preferred_index=ns.window), ensure_ascii=False, indent=2))
        return 0
    if ns.cmd == "list":
        return list_projects(ns.json)
    if ns.cmd == "current":
        return current_project()
    if ns.cmd == "layout":
        return master_layout()
    if ns.cmd == "new":
        create_project(ns.name, no_git=ns.no_git, preferred_index=ns.window, attach=not ns.no_attach)
        return 0
    if ns.cmd == "root":
        return set_project_root(ns.path)
    if ns.cmd == "alias":
        return set_alias(ns.project, ns.alias)
    if ns.cmd == "close":
        return close_project(ns.query)
    if ns.cmd in {"delete", "remove", "unregister"}:
        return delete_project(ns.query)
    if ns.cmd == "slot":
        return set_project_slot(ns.query, ns.value)
    if ns.cmd == "restart":
        return restart_project(
            ns.query,
            all_projects=ns.all,
            attach=not ns.no_attach,
            delay=ns.delay,
        )
    if ns.cmd == "_restart-now":
        _restart_now(ns.query, ns.delay)
        return 0
    if ns.cmd == "migrate-legacy":
        return migrate_legacy(ns.path)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_v12_metadata():
    data = json.loads((ROOT / "VERSION.json").read_text())
    assert data["components"]["ai_chat_tui"] == "1.2.0"
    assert data["components"]["ai_workspace"] == "1.2.0"
    assert data["input_safety_schema"] == 1
    assert data["queue_dedupe_schema"] == 1
    assert data["project_registry_delete_schema"] == 1
    assert data["smart_tab_completion_schema"] == 1


def test_terminal_escape_sanitizer_and_duplicate_queue(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    repo.mkdir()
    state.mkdir()
    monkeypatch.setenv("AI_ORCH_REPO", str(repo))
    monkeypatch.setenv("AI_ORCH_PROJECT_BASE", str(state))
    monkeypatch.setenv("AI_ORCH_WORKSPACE_ID", "v12-test")
    monkeypatch.setenv("AI_ORCH_PROJECT_NAME", "v12-test")

    tui = _load("tui_v12_safety", APP / "ai_chat_tui.py")

    raw = "when stop " + "\x1b[<43;33;54M" + "spending time" + "\x1b[<43;34;54m" + " now"
    cleaned, contaminated = tui.sanitize_terminal_input(raw)
    assert contaminated
    assert cleaned == "when stop spending time now"

    cleaned, contaminated = tui.sanitize_terminal_input(
        "before ^[[<43;33;54Mmiddle^[[<43;34;54M after"
    )
    assert contaminated
    assert cleaned == "before middle after"

    ordinary = "한글 prompt\nsecond line\tvalue"
    assert tui.sanitize_terminal_input(ordinary) == (ordinary, False)

    ui = object.__new__(tui.OrchBridgeApp)
    ui.queue_state = {
        "version": 1,
        "paused": False,
        "items": [],
        "active_entry_id": None,
        "gate": None,
        "halted_reason": None,
    }
    ui._queue_workspace_ready = lambda: True
    ui._current_runtime_state = lambda: {"model_override": "auto"}
    ui._branch_next_plan = lambda: None
    ui._save_queue_state = lambda: None

    first = tui.OrchBridgeApp._queue_enqueue(ui, "same task", [], gate=None)
    second = tui.OrchBridgeApp._queue_enqueue(ui, "same task", [], gate=None)
    assert not first.get("_duplicate_suppressed")
    assert second.get("_duplicate_suppressed")
    assert len(ui.queue_state["items"]) == 1

    try:
        tui.OrchBridgeApp._queue_enqueue(ui, "bad\x1b[<0;1;1Mtask", [], gate=None)
    except ValueError as exc:
        assert "terminal control sequence" in str(exc)
    else:
        raise AssertionError("contaminated queue input was accepted")

    ui._last_submit_fingerprint = ""
    ui._last_submit_at = 0.0
    tui.OrchBridgeApp._remember_submit(ui, "fresh task", [])
    assert tui.OrchBridgeApp._recent_submit_duplicate(ui, "fresh task", [])
    assert not tui.OrchBridgeApp._recent_submit_duplicate(ui, "different task", [])


def test_project_delete_preserves_repo_state_and_compacts_slots(tmp_path):
    workspace = _load("workspace_v12_delete", APP / "ai_workspace.py")
    reg = tmp_path / "global" / "workspaces.json"
    reg.parent.mkdir(parents=True)

    rows = {}
    paths = {}
    for idx, name in [(1, "alpha"), (5, "stale-v6"), (7, "village-coverage")]:
        repo = tmp_path / "repos" / name
        state = tmp_path / "state" / name
        repo.mkdir(parents=True)
        state.mkdir(parents=True)
        wid = f"{name}-id"
        rows[wid] = {
            "id": wid,
            "name": name,
            "display_name": name,
            "display_name_custom": False,
            "repo": str(repo),
            "project_base": str(state),
            "window_index": idx,
        }
        paths[name] = (repo, state)

    reg.write_text(json.dumps({
        "version": 2,
        "master_session": "orch",
        "workspaces": rows,
    }, indent=2) + "\n")

    workspace.REGISTRY = reg
    workspace.PROJECTS_DIR = tmp_path / "state"
    workspace.tmux_windows = lambda: []
    workspace.tmux_has_session = lambda *args, **kwargs: False

    assert workspace.delete_project("5") == 0
    data = json.loads(reg.read_text())
    assert "stale-v6-id" not in data["workspaces"]
    assert paths["stale-v6"][0].is_dir()
    assert paths["stale-v6"][1].is_dir()

    remaining = sorted(
        data["workspaces"].values(),
        key=lambda item: item["window_index"],
    )
    assert [
        (item["name"], item["window_index"]) for item in remaining
    ] == [("alpha", 1), ("village-coverage", 2)]

    unknown = tmp_path / "repos" / "not-registered"
    unknown.mkdir()
    try:
        workspace.resolve_registered_query(str(unknown))
    except SystemExit:
        pass
    else:
        raise AssertionError("unregistered path unexpectedly resolved")


def test_list_backed_tab_completion(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    repo.mkdir()
    state.mkdir()
    monkeypatch.setenv("AI_ORCH_REPO", str(repo))
    monkeypatch.setenv("AI_ORCH_PROJECT_BASE", str(state))
    monkeypatch.setenv("AI_ORCH_WORKSPACE_ID", "alpha-id")
    monkeypatch.setenv("AI_ORCH_PROJECT_NAME", "alpha")

    tui = _load("tui_v12_completion", APP / "ai_chat_tui.py")
    reg = tmp_path / "workspaces.json"
    reg.write_text(json.dumps({
        "version": 2,
        "workspaces": {
            "alpha-id": {
                "id": "alpha-id", "name": "alpha", "display_name": "alpha",
                "window_index": 1, "repo": str(repo), "project_base": str(state),
            },
            "village-id": {
                "id": "village-id", "name": "village-coverage",
                "display_name": "village-coverage", "window_index": 2,
                "repo": str(tmp_path / "village"), "project_base": str(tmp_path / "vstate"),
            },
        },
    }, indent=2) + "\n")
    tui.WORKSPACE_REGISTRY = reg

    names = tui.OrchBridgeApp._registered_project_completion_names(SimpleNamespace())
    assert names == ["alpha", "village-coverage"]

    dummy = SimpleNamespace()
    dummy._registered_project_completion_names = lambda: names
    dummy._branch_completion_names = lambda: []
    dummy._argument_completion_rows = (
        lambda base, partial, values, desc:
        tui.OrchBridgeApp._argument_completion_rows(base, partial, values, desc)
    )
    candidates = tui.OrchBridgeApp._dynamic_slash_candidates(
        dummy, "/project delete "
    )
    assert [row[0] for row in candidates] == [
        "/project delete alpha",
        "/project delete village-coverage",
    ]

    class Doc:
        end = (0, 0)

    class Box:
        def __init__(self):
            self.text = "/project delete alpha"
            self.document = Doc()
            self.cursor_location = None
        def load_text(self, text: str):
            self.text = text
            self.document.end = (0, len(text))

    box = Box()
    complete = SimpleNamespace(
        _command_palette_active=lambda: False,
        _slash_candidates=lambda: candidates,
        query_one=lambda *args, **kwargs: box,
        update_commandbar=lambda: None,
    )
    tui.OrchBridgeApp.action_complete_command(complete)
    assert box.text == "/project delete village-coverage"

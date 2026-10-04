from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_project_queue_survives_tui_reload(tui_module, tmp_path: Path, monkeypatch) -> None:
    queue_file = tmp_path / "projects/workspace-a/task-queue.json"
    queue_file.parent.mkdir(parents=True)
    monkeypatch.setattr(tui_module, "QUEUE_FILE", queue_file)

    first = object.__new__(tui_module.OrchBridgeApp)
    first.queue_state = first._queue_default_state()
    first.queue_state.update(
        {
            "active_entry_id": "queue-1",
            "gate": {"job_id": "job-4", "status": "RUNNING"},
            "items": [
                {"id": "queue-1", "prompt": "first", "workspace_id": "workspace-a"},
                {"id": "queue-2", "prompt": "second", "workspace_id": "workspace-a"},
            ],
        }
    )
    first._save_queue_state()

    restarted = object.__new__(tui_module.OrchBridgeApp)
    loaded = restarted._load_queue_state()
    assert [item["id"] for item in loaded["items"]] == ["queue-1", "queue-2"]
    assert loaded["active_entry_id"] == "queue-1"
    assert loaded["gate"]["job_id"] == "job-4"


def test_malformed_queue_is_preserved_and_paused(tui_module, tmp_path: Path, monkeypatch) -> None:
    queue_file = tmp_path / "projects/workspace-b/task-queue.json"
    queue_file.parent.mkdir(parents=True)
    original = "{not valid queue data"
    queue_file.write_text(original, encoding="utf-8")
    monkeypatch.setattr(tui_module, "QUEUE_FILE", queue_file)

    app = object.__new__(tui_module.OrchBridgeApp)
    loaded = app._load_queue_state()
    assert loaded["paused"] is True
    assert loaded["halted_reason"].startswith("QUEUE_FILE_INVALID:")
    assert queue_file.read_text(encoding="utf-8") == original


def test_jobs_are_visible_only_to_their_registered_project(tui_module, tmp_path: Path) -> None:
    repo = tmp_path / "same-repo"
    project_a = tmp_path / "projects/workspace-a"
    project_b = tmp_path / "projects/workspace-b"
    job = {"repo": str(repo), "workspace_id": "workspace-a", "project_base": str(project_a)}

    assert tui_module.job_belongs_to_context(
        job,
        current_repo=repo,
        workspace_id="workspace-a",
        project_base=project_a,
        global_base=tui_module.BASE,
    )
    assert not tui_module.job_belongs_to_context(
        job,
        current_repo=repo,
        workspace_id="workspace-b",
        project_base=project_b,
        global_base=tui_module.BASE,
    )
    assert not tui_module.job_belongs_to_context(
        job,
        current_repo=repo,
        workspace_id=None,
        project_base=tui_module.BASE,
        global_base=tui_module.BASE,
    )


def test_tui_restart_persists_state_and_restarts_project_without_killing_main(
    tui_module, tmp_path: Path, monkeypatch
) -> None:
    helper = tui_module.HOME / ".local/bin/orch-project"
    helper.parent.mkdir(parents=True)
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setattr(tui_module, "WORKSPACE_ID", "workspace-a")
    saved: list[str] = []
    spawned: list[tuple[list[str], dict[str, object]]] = []
    exited: list[bool] = []
    monkeypatch.setattr(
        tui_module.subprocess,
        "Popen",
        lambda args, **kwargs: spawned.append((list(args), kwargs)),
    )
    fake = SimpleNamespace(
        repo=tmp_path,
        job_dir=None,
        _save_tui_state=lambda: saved.append("tui"),
        _save_queue_state=lambda: saved.append("queue"),
        exit=lambda: exited.append(True),
        note=lambda *args, **kwargs: None,
    )

    tui_module.OrchBridgeApp._schedule_self_restart(fake)

    assert saved == ["tui", "queue"]
    assert len(spawned) == 1
    command, options = spawned[0]
    assert command[1:] == ["restart", "workspace-a", "--no-attach", "--delay", "0.8"]
    assert options["start_new_session"] is True
    assert exited == [True]


def test_reload_refreshes_settings_and_queue_without_replacing_worker(tui_module) -> None:
    snapshots = iter(
        [
            {"model_override": "auto", "router_enabled": True, "scope_mode": "normal", "permission_profile": "guarded", "notify_mode": "smart"},
            {"model_override": "codex", "router_enabled": True, "scope_mode": "normal", "permission_profile": "guarded", "notify_mode": "smart"},
        ]
    )
    queue = {"items": [{"id": "queue-2", "prompt": "keep me"}]}
    schedule = {"items": [{"id": "schedule-1", "prompt": "later"}]}
    worker = object()
    fake = SimpleNamespace(
        _current_runtime_state=lambda: next(snapshots),
        _load_tui_state=lambda: None,
        _load_queue_state=lambda: queue,
        _load_schedule_state=lambda: schedule,
        update_banner=lambda: None,
        update_commandbar=lambda: None,
        proc=worker,
    )

    message = tui_module.OrchBridgeApp._reload_project_settings(fake)
    assert "changed: model_override" in message
    assert fake.queue_state == queue
    assert fake.schedule_state == schedule
    assert fake.proc is worker


def test_failed_main_result_is_persisted_and_notified(tui_module, tmp_path: Path, monkeypatch) -> None:
    result_file = tmp_path / "result.json"
    result_file.write_text(
        json.dumps({"task_status": "FAILED", "rc": 1, "response": "provider failed"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(tui_module, "git_branch", lambda _repo: "main")
    monkeypatch.setattr(tui_module, "git_head", lambda _repo: "deadbeef")
    notifications: list[tuple[str, dict, int]] = []
    saved: list[bool] = []
    fake = SimpleNamespace(
        finish_attempt=lambda: None,
        raw=[],
        job={"id": "job-1", "status": "RUNNING", "title": "unit failure"},
        task_status=None,
        proc=object(),
        _mark_main_worker_finished=lambda _rc: None,
        _recovered_main_result_posted=True,
        _copy_messages=[],
        _mount_timeline_message=lambda *args, **kwargs: None,
        _mount_worked=lambda **kwargs: None,
        _maybe_notify_terminal=lambda status, result, rc: notifications.append((status, result, rc)),
        save=lambda: saved.append(True),
        update_banner=lambda: None,
        repo=tmp_path,
    )

    asyncio.run(tui_module.OrchBridgeApp.on_done(fake, SimpleNamespace(rc=1, result=result_file)))
    assert fake.job["status"] == "FAILED"
    assert saved == [True]
    assert notifications and notifications[0][0] == "FAILED"


def test_safe_branch_manager_creates_and_guards_branches(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    (repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    subprocess.run(
        [
            "git", "-C", str(repo), "-c", "user.name=OrchBridge Test",
            "-c", "user.email=tests@invalid", "commit", "--quiet", "-m", "initial",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    import sys

    app_dir = str(Path(__file__).resolve().parents[1] / "app")
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    import branch_manager

    created = branch_manager.new(repo, "agent/verified", "HEAD")
    assert created["branch"] == "agent/verified"
    assert _git(repo, "branch", "--show-current") == "agent/verified"

    _git(repo, "branch", "existing")
    _git(repo, "switch", "--quiet", "main")
    (repo / "tracked.txt").write_text("uncommitted user work\n", encoding="utf-8")
    try:
        branch_manager.switch(repo, "existing")
    except RuntimeError as error:
        assert "not clean" in str(error)
    else:
        raise AssertionError("dirty worktree must block branch switching")
    assert _git(repo, "branch", "--show-current") == "main"
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "uncommitted user work\n"

    monkeypatch.setenv("AI_ORCH_DELEGATION_DEPTH", "1")
    try:
        branch_manager.new(repo, "agent/delegated", "HEAD")
    except SystemExit as error:
        assert "MAIN only" in str(error)
    else:
        raise AssertionError("delegate branch mutation must be blocked")
    assert _git(repo, "branch", "--show-current") == "main"

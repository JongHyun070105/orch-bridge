from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def test_smart_notifications_cover_single_failure_and_queue_boundaries(tui_module, tmp_path: Path) -> None:
    fake = SimpleNamespace(
        notify_mode="smart",
        queue_state={"items": [], "active_entry_id": None, "gate": None, "paused": False},
        attempts=[SimpleNamespace(model="codex")],
        model_override=None,
        _job_elapsed_text=lambda: "42s",
        job={"id": "job-1", "title": "Improve Unicode handling", "branch_at_end": "feature/unicode"},
        repo=tmp_path / "demo-project",
    )

    single = tui_module.OrchBridgeApp._notification_payload(fake, "COMPLETE", {"main_name": "Codex"})
    assert single is not None
    assert "demo-project" in single["title"]
    assert "COMPLETE" in single["subtitle"]
    assert "Codex" in single["subtitle"]
    assert "42s" in single["subtitle"]
    assert "feature/unicode" in single["message"]

    failed = tui_module.OrchBridgeApp._notification_payload(fake, "FAILED", {"main_name": "Codex"}, 1)
    assert failed is not None and "FAILED" in failed["subtitle"]

    fake.queue_state = {
        "items": [{"id": "queue-1"}, {"id": "queue-2"}],
        "active_entry_id": "queue-1",
        "gate": {"job_id": "job-1"},
        "paused": False,
    }
    assert tui_module.OrchBridgeApp._notification_payload(fake, "COMPLETE", {"main_name": "Codex"}) is None

    fake.queue_state["items"] = [{"id": "queue-1"}]
    final = tui_module.OrchBridgeApp._notification_payload(fake, "COMPLETE", {"main_name": "Codex"})
    assert final is not None and "QUEUE COMPLETE" in final["subtitle"]


def test_missing_linux_notification_backend_fails_gracefully() -> None:
    from app import desktop_notify

    with patch.object(desktop_notify.platform, "system", return_value="Linux"), patch.object(
        desktop_notify.shutil, "which", return_value=None
    ):
        assert "unavailable" in desktop_notify.backend_status()
        result = desktop_notify.send_notification(title="test", message="test")
    assert result["ok"] is False
    assert result["backend"] == "none"
    assert "notify-send" in result["reason"]


def test_headless_notification_command_error_never_raises() -> None:
    from app import desktop_notify

    with patch.object(desktop_notify.platform, "system", return_value="Linux"), patch.object(
        desktop_notify.shutil, "which", return_value="/usr/bin/notify-send"
    ), patch.object(desktop_notify.subprocess, "run", side_effect=OSError("no desktop session")):
        result = desktop_notify.send_notification(title="test", message="test")
    assert result["ok"] is False
    assert result["backend"] == "notify-send"


def test_notification_test_mode_needs_no_desktop_session(monkeypatch) -> None:
    from app import desktop_notify

    monkeypatch.setenv("AI_ORCH_NOTIFY_DRY_RUN", "1")
    with patch.object(desktop_notify.platform, "system", side_effect=AssertionError("dry run must not probe OS")):
        result = desktop_notify.send_notification(title="test", message="test")
    assert result["ok"] is True
    assert result["backend"] == "dry-run"

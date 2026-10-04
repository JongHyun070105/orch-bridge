from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_v13_metadata_and_public_defaults():
    data = json.loads((ROOT / "VERSION.json").read_text())
    manifest = json.loads((ROOT / ".release-please-manifest.json").read_text())
    assert data["release"] == manifest["."]
    assert data["release"] in {"1.2.0", "1.3.0"}
    for key in (
        "scheduled_prompt_schema",
        "global_provider_health_schema",
        "transcript_follow_schema",
        "live_slot_compaction_schema",
        "project_slot_pin_schema",
        "proactive_delegation_schema",
        "claude_runtime_schema",
        "agy_model_breaker_schema",
        "router_visibility_schema",
    ):
        assert data[key] == 1
    assert data["platforms"] == ["macOS", "Linux"]
    assert data["components"]["ai_chat_tui"] == "1.3.0"
    assert data["components"]["ai_orch"] == "1.3.0"
    tui = (APP / "ai_chat_tui.py").read_text()
    assert 'permission_profile", "guarded"' in tui
    assert ".local/share/orchbridge" in tui
    assert "/Users/" not in tui


def test_scheduled_prompt_parser_and_queue_contract(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    state = tmp_path / "state"
    repo.mkdir()
    state.mkdir()
    monkeypatch.setenv("AI_ORCH_REPO", str(repo))
    monkeypatch.setenv("AI_ORCH_PROJECT_BASE", str(state))
    monkeypatch.setenv("AI_ORCH_WORKSPACE_ID", "v13-test")
    monkeypatch.setenv("AI_ORCH_PROJECT_NAME", "v13-test")
    tui = _load("tui_v13_schedule", APP / "ai_chat_tui.py")

    ref = datetime(2026, 10, 4, 13, 0, tzinfo=timezone.utc)
    due, prompt = tui.parse_schedule_request("in 1h30m verify CI", ref)
    assert int((due - ref).total_seconds()) == 5400
    assert prompt == "verify CI"
    due, prompt = tui.parse_schedule_request("tomorrow 09:00 review evidence", ref)
    assert due.day == 5 and due.hour == 9 and due.minute == 0
    assert prompt == "review evidence"
    due, prompt = tui.parse_schedule_request("2026-10-05 18:15 prepare release", ref)
    assert due.day == 5 and due.hour == 18 and due.minute == 15
    src = (APP / "ai_chat_tui.py").read_text()
    assert 'SCHEDULE_FILE = PROJECT_BASE / "scheduled-prompts.json"' in src
    assert "self._queue_enqueue(" in src
    assert "self._schedule_tick()" in src
    assert "they never interrupt the active job" in src


def test_cmd_global_quota_state_persists_and_clears(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    sys.modules.pop("provider_health_state", None)
    ph = _load("provider_health_state", APP / "provider_health_state.py")
    item = ph.mark_quota_exhausted(
        "commandcode",
        reason="credit/quota exhausted",
        source="test",
        retry_after_seconds=3600,
    )
    assert item["status"] == "QUOTA_EXHAUSTED"
    assert ph.provider_unavailable("commandcode")
    assert not ph.probe_due("commandcode")
    assert "QUOTA_EXHAUSTED" in ph.summary("commandcode")
    ph.clear_provider("commandcode", source="test")
    assert not ph.provider_unavailable("commandcode")


def test_claude_55_adaptive_effort_and_safe_stream_visibility():
    runtime = _load("claude_runtime_v13", APP / "claude_runtime.py")
    assert runtime.pretty_model("claude-sonnet-5-5") == "Claude Sonnet 5.5"
    assert runtime.pretty_model("claude-opus-5-5") == "Claude Opus 5.5"
    low, source = runtime.adaptive_effort(
        need=.20, reasoning=.20, uncertainty=.10, scope=.10, risk=.10, crosscheck=.10, env={}
    )
    assert (low, source) == ("low", "adaptive")
    high, source = runtime.adaptive_effort(
        need=.91, reasoning=.91, uncertainty=.90, scope=.75, risk=.90, crosscheck=.92, env={}
    )
    assert (high, source) == ("xhigh", "adaptive")
    forced, source = runtime.adaptive_effort(
        need=.10, reasoning=.10, uncertainty=.10, scope=.10, risk=.10, crosscheck=.10,
        env={"AI_ORCH_CLAUDE_EFFORT_LEVEL": "max"},
    )
    assert (forced, source) == ("max", "env")

    actions = runtime.stream_actions({
        "type": "assistant",
        "message": {"content": [
            {"type": "thinking", "thinking": "PRIVATE_CHAIN_OF_THOUGHT"},
            {"type": "text", "text": "Checking tests."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}, "id": "t1"},
        ]},
    })
    assert {"kind": "reasoning"} in actions
    assert any(x.get("kind") == "text" and x.get("text") == "Checking tests." for x in actions)
    assert any(x.get("kind") == "tool" and x.get("name") == "Bash" for x in actions)
    assert "PRIVATE_CHAIN_OF_THOUGHT" not in json.dumps(actions)


def test_exact_agy_thinking_models_and_proactive_delegate_budget():
    p1 = (APP / "phase1_supervisor.py").read_text()
    p3 = (APP / "phase3_orchestrator.py").read_text()
    assert '"model": "claude-sonnet-4-6-thinking"' in p1
    assert '"model": "claude-opus-4-6-thinking"' in p1
    assert '"model": "claude-sonnet-4-6-thinking"' in p3
    assert '"model": "claude-opus-4-6-thinking"' in p3
    assert "status NOT IN ('BLOCKED', 'CANCELLED')" in p1
    assert "status NOT IN ('BLOCKED', 'CANCELLED')" in p3
    assert "[delegate] auto-select role=" in p1
    assert "def _collab_task_role" in p3
    assert p1.index("if args.task_file:") < p1.index("target = _auto_target(caller, task)")


def test_router_visibility_and_cmd_exclusion_surfaces():
    orch = (APP / "ai_orch.py").read_text()
    tui = (APP / "ai_chat_tui.py").read_text()
    assert 'JUDGE_HEALTH_KEY = "commandcode:judge"' in orch
    assert 'global_provider_unavailable("commandcode")' in orch
    assert "route utility (not raw model quality):" in orch
    assert "Command Code availability probe only (not MAIN routing)" in orch
    assert "def migrate_legacy_judge_cooldown" in orch
    assert 'CLAUDE_CODE_PINNED_MODELS = {' in orch
    assert '"sonnet": "claude-sonnet-5-5"' in orch
    assert '"opus": "claude-opus-5-5"' in orch

    mod = _load("tui_v13_router", APP / "ai_chat_tui.py")
    m = mod.ROUTE_RE.match(
        "[ai-orch] route utility (not raw model quality): "
        "claude-sonnet(1.68) > "
        "gemini-high(1.43)"
    )
    assert m
    panel = mod.pretty_router_panel(m.group(1))
    assert "Claude Code Sonnet 5.5 (1.68)" in panel
    assert "Gemini 3.8 Flash · high (1.43)" in panel
    assert "subscription" not in panel.lower()
    assert "GLOBAL_PROVIDER_HEALTH_FILE" in tui
    assert "elif m := ROUTE_RE.match(line):" in tui
    assert 'if not provider_config_enabled(config, "commandcode"):' in orch
    assert orch.index('global_probe_due("commandcode")') < orch.index('provider_config_enabled(config, "commandcode")')


def test_transcript_follow_and_project_slot_surfaces():
    tui = (APP / "ai_chat_tui.py").read_text()
    ws = (APP / "ai_workspace.py").read_text()
    assert "def _follow_feed_bottom" in tui
    assert "scroll_end(animate=False)" in tui
    assert tui.count("self._follow_feed_bottom()") >= 4
    assert "/project slot <name> <number|auto>" in tui
    assert "def set_project_slot" in ws
    assert 'slotp = sub.add_parser("slot")' in ws
    assert "slot_pinned" in ws
    assert "Compact live windows and keep the registry collision-free" in ws

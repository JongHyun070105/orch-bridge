from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))


def test_v11_metadata_and_public_defaults():
    data = json.loads((ROOT / "VERSION.json").read_text())
    assert data["release"] in {"1.0.0", "1.1.0"}
    assert data["components"]["provider_runtime"] == "1.1.0"
    assert data["components"]["provider_doctor"] == "1.1.0"
    assert data["platforms"] == ["macOS", "Linux"]
    assert data["steering_schema"] == 1
    assert data["provider_health_schema"] == 1
    tui = (APP / "ai_chat_tui.py").read_text()
    assert 'permission_profile", "guarded"' in tui
    assert ".local/share/orchbridge" in tui


def test_palette_guard_is_retained():
    tui = (APP / "ai_chat_tui.py").read_text()
    assert "CommandPalette.is_open(self)" in tui
    assert "def check_action" in tui
    assert "self._command_palette_active()" in tui


def test_steer_and_doctor_surface_is_present():
    tui = (APP / "ai_chat_tui.py").read_text()
    assert '("/doctor",' in tui
    assert '("/steer <지시>",' in tui
    assert "def steer_job" in tui
    assert "steer-history.jsonl" in tui
    assert "--- STEERING INSTRUCTIONS ---" in tui
    assert "steer_pending" in tui
    assert (ROOT / "bin/orch-doctor").is_file()


def test_nonzero_complete_is_not_authoritative():
    tree = ast.parse((APP / "ai_chat_tui.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_validated_task_status")
    module = ast.Module(body=[node], type_ignores=[])
    ns: dict[str, object] = {}
    exec(compile(ast.fix_missing_locations(module), "<status-test>", "exec"), ns)
    fn = ns["_validated_task_status"]
    assert fn("COMPLETE", 0) == "COMPLETE"
    assert fn("COMPLETE", 1) is None
    assert fn("FAILED", 1) == "FAILED"


def test_provider_resolver_rechecks_after_missing(tmp_path, monkeypatch):
    import provider_runtime as runtime
    runtime.clear_positive_cache()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("SHELL", raising=False)
    cfg = {"providers": {"codex": {"enabled": "auto", "binary": "codex"}}}
    assert runtime.resolve_provider_cli("codex", cfg, home=tmp_path, use_cache=True) is None
    exe = tmp_path / ".local/bin/codex"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\necho codex-test\n")
    exe.chmod(0o755)
    assert runtime.resolve_provider_cli("codex", cfg, home=tmp_path, use_cache=True) == str(exe.resolve())


def test_provider_resolver_finds_nvm_bin(tmp_path, monkeypatch):
    import provider_runtime as runtime
    runtime.clear_positive_cache()
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("SHELL", raising=False)
    exe = tmp_path / ".nvm/versions/node/v99/bin/codex"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\necho codex-test\n")
    exe.chmod(0o755)
    cfg = {"providers": {"codex": {"enabled": "auto", "binary": "codex"}}}
    assert runtime.resolve_provider_cli("codex", cfg, home=tmp_path, use_cache=False) == str(exe.resolve())


def _doctor(tmp_path: Path) -> dict:
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["PATH"] = os.pathsep.join((str(tmp_path / ".local/bin"), "/usr/bin", "/bin"))
    proc = subprocess.run(
        [sys.executable, str(APP / "provider_doctor.py"), "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_doctor_is_nonfatal_when_optional_providers_missing(tmp_path):
    data = _doctor(tmp_path)
    assert set(data["providers"]) == {"codex", "claude", "commandcode", "agy"}
    allowed = {
        "MISSING", "HEALTHY", "AUTH_REQUIRED", "QUOTA_EXHAUSTED",
        "FOUND_BUT_COMMAND_FAILS", "BROKEN_INSTALL", "FOUND_BUT_NOT_EXECUTABLE", "UNKNOWN",
    }
    assert all(row["status"] in allowed for row in data["providers"].values())


def test_doctor_detects_broken_codex_npm_and_stale_dir(tmp_path):
    root = tmp_path / ".nvm/versions/node/v99"
    package = root / "lib/node_modules/@openai/codex"
    package.mkdir(parents=True)
    scope = package.parent
    (scope / ".codex-stale-test").mkdir()
    link = root / "bin/codex"
    link.parent.mkdir(parents=True)
    link.symlink_to(root / "missing-codex")
    data = _doctor(tmp_path)
    assert data["providers"]["codex"]["status"] == "BROKEN_INSTALL"
    assert data["codex_npm"]["stale_dirs"]
    assert any("ENOTEMPTY" in warning for warning in data["warnings"])


def test_missing_binary_has_no_long_cooldown_path():
    orch = (APP / "ai_orch.py").read_text()
    assert 'outcome.kind == "missing_binary"' in orch
    assert "no cooldown; run /doctor" in orch
    assert 'resolve_provider_cli("codex", config)' in orch
    assert 'resolve_provider_cli("commandcode", config)' in orch

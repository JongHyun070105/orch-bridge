from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


@pytest.fixture
def tui_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app_path = str(APP_DIR)
    if app_path not in sys.path:
        sys.path.insert(0, app_path)
    for name in ("ai_chat_tui", "desktop_notify", "platform_support", "orch_runtime"):
        sys.modules.pop(name, None)
    module = importlib.import_module("ai_chat_tui")
    yield module
    for name in ("ai_chat_tui", "desktop_notify", "platform_support", "orch_runtime"):
        sys.modules.pop(name, None)

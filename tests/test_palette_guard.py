from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

from textual.app import ComposeResult
from textual.command import CommandPalette, Hit, Provider
from textual.widgets import Static, TextArea


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))


class PaletteActions(Provider):
    async def search(self, query: str):
        if not query:
            return
        for label in ("Alpha palette action", "Beta palette action"):
            if query.casefold() in label.casefold():
                yield Hit(
                    1.0,
                    label,
                    lambda label=label: self.app.selected_commands.append(label),
                    text=label,
                )


def test_palette_enter_selects_highlighted_action_without_sending(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    sys.modules.pop("ai_chat_tui", None)
    tui = importlib.import_module("ai_chat_tui")

    class PaletteHarness(tui.OrchBridgeApp):
        CSS = ""
        COMMANDS = {PaletteActions}

        def __init__(self) -> None:
            super().__init__()
            self.sent = 0
            self.selected_commands: list[str] = []
            self.copy_pressed = 0

        def compose(self) -> ComposeResult:
            yield Static(id="brand")
            yield TextArea(id="prompt")

        def update_banner(self) -> None:
            pass

        def on_mount(self) -> None:
            pass

        def recover(self) -> None:
            pass

        def tick(self) -> None:
            pass

        def on_text_area_changed(self, _event) -> None:
            pass

        def update_commandbar(self) -> None:
            pass

        def action_send(self) -> None:
            self.sent += 1

        def action_copy_last(self) -> None:
            self.copy_pressed += 1

    async def scenario() -> None:
        app = PaletteHarness()
        async with app.run_test() as pilot:
            await pilot.press("ctrl+p")
            await pilot.pause(0.1)
            assert CommandPalette.is_open(app)

            await pilot.press(*tuple("action"))
            await pilot.pause(0.2)
            command_list = app.screen.query_one("CommandList")
            before = command_list.highlighted
            await pilot.press("down")
            after = command_list.highlighted
            assert before != after

            await pilot.press("enter")
            await pilot.pause(0.1)
            assert not CommandPalette.is_open(app)
            assert app.sent == 0
            assert len(app.selected_commands) == 1

            prompt = app.query_one("#prompt", TextArea)
            prompt.focus()
            prompt.load_text("normal prompt")
            await pilot.pause(0.05)
            await pilot.press("enter")
            assert app.sent == 1

            prompt.load_text("newline")
            prompt.cursor_location = prompt.document.end
            before_text = prompt.text
            await pilot.press("shift+enter")
            assert prompt.text == before_text + "\n"

            prompt.load_text("clear me")
            await pilot.press("ctrl+u")
            assert prompt.text == ""
            await pilot.press("ctrl+y")
            assert app.copy_pressed == 1

            await pilot.press("ctrl+p")
            await pilot.pause(0.1)
            assert CommandPalette.is_open(app)
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert not CommandPalette.is_open(app)

    asyncio.run(scenario())
    sys.modules.pop("ai_chat_tui", None)

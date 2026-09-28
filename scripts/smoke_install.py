#!/usr/bin/env python3
"""Install a release artifact into a temporary HOME and verify its CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


PROVIDERS = ("codex", "claude", "agy", "commandcode")


def run(command: list[str], *, env: dict[str, str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, env=env, text=True, capture_output=True, timeout=timeout)


def smoke(installer: Path) -> None:
    installer = installer.resolve()
    if not installer.is_file():
        raise FileNotFoundError(installer)
    expected_version = installer.name.removeprefix("orchbridge-v").removesuffix(".sh")

    with tempfile.TemporaryDirectory(prefix="orchbridge-install-smoke-") as temporary:
        home = Path(temporary)
        env = dict(os.environ)
        env["HOME"] = str(home)
        env["PATH"] = os.pathsep.join((str(home / ".local/bin"), env.get("PATH", "")))

        config_path = home / ".config/orchbridge/config.json"
        config_path.parent.mkdir(parents=True)
        original_config = {
            "schema_version": 1,
            "default_repo": str(home / "Projects"),
            "providers": {
                name: {"enabled": "auto", "binary": name}
                for name in PROVIDERS
            },
            "models": {"commandcode": "custom-model-preserved"},
            "ui": {"notify_mode": "smart"},
        }
        config_path.write_text(json.dumps(original_config, indent=2) + "\n", encoding="utf-8")

        install = run(["bash", str(installer), "--yes"], env=env)
        if install.returncode:
            raise RuntimeError(f"installer failed:\n{install.stdout}\n{install.stderr}")

        commands = home / ".local/bin"
        cli = commands / "orch"
        if not cli.is_file():
            raise RuntimeError("installer did not create the orch command")
        version = run([str(cli), "version"], env=env)
        if version.returncode or version.stdout.strip() != expected_version:
            raise RuntimeError(f"orch version failed:\n{version.stdout}\n{version.stderr}")

        settings = run([str(cli), "settings", "show", "--json"], env=env)
        if settings.returncode:
            raise RuntimeError(f"orch settings show failed:\n{settings.stdout}\n{settings.stderr}")
        loaded_config = json.loads(settings.stdout)
        if loaded_config.get("models", {}).get("commandcode") != "custom-model-preserved":
            raise RuntimeError("installation overwrote the existing user model configuration")

        detected = run([str(cli), "settings", "detect", "--json"], env=env)
        if detected.returncode:
            raise RuntimeError(f"provider detection failed:\n{detected.stdout}\n{detected.stderr}")
        providers = json.loads(detected.stdout)
        if set(providers) != set(PROVIDERS):
            raise RuntimeError(f"provider detection returned an unexpected set: {sorted(providers)}")

        doctor = run([str(cli), "doctor"], env=env)
        if doctor.returncode:
            raise RuntimeError(f"orch doctor failed:\n{doctor.stdout}\n{doctor.stderr}")

        state_file = home / ".local/share/orchbridge/projects/smoke/task-queue.json"
        state_file.parent.mkdir(parents=True)
        state_file.write_text('{"items": []}\n', encoding="utf-8")
        uninstall = run([str(cli), "uninstall"], env=env)
        if uninstall.returncode:
            raise RuntimeError(f"orch uninstall failed:\n{uninstall.stdout}\n{uninstall.stderr}")
        if state_file.read_text(encoding="utf-8") != '{"items": []}\n':
            raise RuntimeError("default uninstall changed persistent queue state")
        if not config_path.is_file():
            raise RuntimeError("uninstall removed user configuration")
        if (home / ".local/share/orchbridge/app").exists():
            raise RuntimeError("uninstall left the runtime application installed")
        if cli.exists():
            raise RuntimeError("uninstall left the orch command installed")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: smoke_install.py /path/to/orchbridge-vX.Y.Z.sh", file=sys.stderr)
        return 2
    try:
        smoke(Path(args[0]))
    except Exception as error:
        print(f"installer smoke failed: {error}", file=sys.stderr)
        return 1
    print("standalone install, provider detection, config preservation, and uninstall smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

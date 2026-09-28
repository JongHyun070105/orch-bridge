from __future__ import annotations

import re
from pathlib import Path

from scripts.build_release import release_files


SENSITIVE_PATTERNS = (
    ("home-directory path", re.compile(r"(?<![A-Za-z0-9_])/(?:Users|home)/[^/\\s\"'<>]+")),
    (
        "provider credential",
        re.compile(
            r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
            r"github_pat_[A-Za-z0-9_]{20,}|AIza[A-Za-z0-9_-]{30,}|"
            r"AKIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{16,})\b"
        ),
    ),
    ("private key material", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("cloud account id", re.compile(r"(?<!\d)\d{12}(?!\d)")),
    (
        "email address",
        re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    ),
)


def sensitive_categories(text: str) -> list[str]:
    return [label for label, pattern in SENSITIVE_PATTERNS if pattern.search(text)]


def test_public_release_source_has_no_machine_paths_or_secrets() -> None:
    root = Path(__file__).resolve().parents[1]
    hits: list[str] = []
    for path in release_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for category in sensitive_categories(text):
            hits.append(f"{path.relative_to(root)}: {category}")
    assert not hits, "sensitive-looking data in public source:\n" + "\n".join(hits)


def test_privacy_patterns_catch_common_leak_shapes() -> None:
    fake_home = "/" + "Users" + "/" + "example-user" + "/Projects/demo"
    fake_token = "sk-" + "A" * 24
    fake_account = "123456" + "789012"
    fake_email = "person" + "@example.invalid"
    assert "home-directory path" in sensitive_categories(fake_home)
    assert "provider credential" in sensitive_categories(fake_token)
    assert "cloud account id" in sensitive_categories(fake_account)
    assert "email address" in sensitive_categories(fake_email)


def test_build_boundary_excludes_runtime_state_and_caches() -> None:
    root = Path(__file__).resolve().parents[1]
    names = {path.relative_to(root).as_posix() for path in release_files(root)}
    assert not any(name.startswith((".omx/", ".venv/", "dist/")) for name in names)
    assert not any(name.endswith((".pyc", ".pyo")) or name.endswith(".DS_Store") for name in names)

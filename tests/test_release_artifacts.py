from __future__ import annotations

import hashlib
import json
import tarfile
import zipfile
from pathlib import Path

from scripts.build_release import build_release
from scripts.validate_release import validate


ROOT = Path(__file__).resolve().parents[1]


def test_version_sources_and_release_tag_agree() -> None:
    version = json.loads((ROOT / "VERSION.json").read_text(encoding="utf-8"))["release"]
    assert validate(f"v{version}", ROOT) == version
    try:
        validate("not-a-semver-tag", ROOT)
    except ValueError as error:
        assert "vMAJOR.MINOR.PATCH" in str(error)
    else:
        raise AssertionError("release tag mismatch must fail closed")


def test_release_builder_is_reproducible_and_excludes_local_state(tmp_path: Path) -> None:
    first = build_release(ROOT, tmp_path / "first")
    second = build_release(ROOT, tmp_path / "second")

    for left, right in zip(first, second, strict=True):
        assert left.read_bytes() == right.read_bytes()

    installer, archive, zip_archive, checksums = first
    version = json.loads((ROOT / "VERSION.json").read_text(encoding="utf-8"))["release"]
    release_root = f"orchbridge-v{version}"
    assert installer.name == f"{release_root}.sh"
    assert archive.name == f"{release_root}.tar.gz"
    assert zip_archive.name == f"{release_root}.zip"
    assert installer.stat().st_mode & 0o111

    with tarfile.open(archive, "r:gz") as source:
        names = source.getnames()
    assert f"{release_root}/app/ai_chat_tui.py" in names
    assert f"{release_root}/install.sh" in names
    assert not any(name.startswith((f"{release_root}/.omx/", f"{release_root}/.venv/")) for name in names)
    assert not any(".DS_Store" in name or "/__pycache__/" in name for name in names)

    with zipfile.ZipFile(zip_archive) as source:
        zip_names = source.namelist()
    assert f"{release_root}/app/ai_chat_tui.py" in zip_names
    assert f"{release_root}/install.sh" in zip_names
    assert not any(name.startswith((f"{release_root}/.omx/", f"{release_root}/.venv/")) for name in zip_names)

    for line in checksums.read_text(encoding="utf-8").splitlines():
        digest, name = line.split(maxsplit=1)
        artifact = checksums.parent / name.strip()
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == digest

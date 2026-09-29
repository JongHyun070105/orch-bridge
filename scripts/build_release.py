#!/usr/bin/env python3
"""Build deterministic OrchBridge source and standalone installer artifacts."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import shutil
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FILES = (
    ".env.example",
    ".gitignore",
    ".release-please-manifest.json",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "VERSION.json",
    "install.sh",
    "release-please-config.json",
    "requirements.txt",
    "uninstall.sh",
)
PUBLIC_DIRS = (".github", "app", "bin", "scripts", "tests", "workflows")
IGNORED_PARTS = {"__pycache__", ".pytest_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def release_files(root: Path = ROOT) -> list[Path]:
    """Return only intended public source files, in stable path order."""
    root = root.resolve()
    paths = [root / name for name in PUBLIC_FILES if (root / name).is_file()]
    for directory in PUBLIC_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root)
            if any(part in IGNORED_PARTS for part in relative.parts):
                continue
            if path.name == ".DS_Store" or path.suffix in IGNORED_SUFFIXES:
                continue
            paths.append(path)
    return sorted(set(paths), key=lambda path: path.relative_to(root).as_posix())


def _write_deterministic_tar(root: Path, files: Iterable[Path]) -> bytes:
    version = json.loads((root / "VERSION.json").read_text(encoding="utf-8"))["release"]
    name = f"orchbridge-v{version}"
    tar_bytes = io.BytesIO()
    with gzip.GzipFile(fileobj=tar_bytes, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for path in files:
                relative = path.relative_to(root).as_posix()
                data = path.read_bytes()
                info = tarfile.TarInfo(f"{name}/{relative}")
                info.size = len(data)
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mode = 0o755 if path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH) else 0o644
                archive.addfile(info, io.BytesIO(data))
    return tar_bytes.getvalue()


def _write_deterministic_zip(root: Path, files: Iterable[Path]) -> bytes:
    version = json.loads((root / "VERSION.json").read_text(encoding="utf-8"))["release"]
    name = f"orchbridge-v{version}"
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo(f"{name}/{relative}", date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            mode = 0o755 if path.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH) else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return output.getvalue()


def _standalone_installer(name: str, archive_sha256: str, raw_archive: bytes) -> str:
    marker = "__ORCHBRIDGE_PAYLOAD_START__"
    end_marker = "__ORCHBRIDGE_PAYLOAD_END__"
    payload = base64.b64encode(raw_archive).decode("ascii")
    payload_lines = "\n".join(payload[i : i + 100] for i in range(0, len(payload), 100))
    return f'''#!/usr/bin/env bash
set -euo pipefail
TMP="$(mktemp -d)"
cleanup() {{ rm -rf "$TMP"; }}
trap cleanup EXIT INT TERM
PYTHON="$(command -v python3 || true)"
[[ -n "$PYTHON" ]] || {{ echo "ERROR: python3 is required" >&2; exit 2; }}

"$PYTHON" - "$0" "$TMP/release.tar.gz" "{archive_sha256}" <<'PYEXTRACT'
from pathlib import Path
import base64, hashlib, sys
source = Path(sys.argv[1]).read_text(encoding="utf-8")
marker = "{marker}"
end_marker = "{end_marker}"
try:
    payload = source.split(marker + "\\n", 1)[1].split("\\n" + end_marker, 1)[0]
except IndexError as error:
    raise SystemExit("installer payload is incomplete") from error
raw = base64.b64decode("".join(payload.split()), validate=True)
if hashlib.sha256(raw).hexdigest() != sys.argv[3]:
    raise SystemExit("installer payload checksum mismatch")
Path(sys.argv[2]).write_bytes(raw)
PYEXTRACT

"$PYTHON" - "$TMP/release.tar.gz" "$TMP" <<'PYUNTAR'
from pathlib import Path, PurePosixPath
import shutil, sys, tarfile
archive_path = Path(sys.argv[1])
destination = Path(sys.argv[2]).resolve()
with tarfile.open(archive_path, "r:gz") as archive:
    for member in archive.getmembers():
        relative = PurePosixPath(member.name)
        if relative.is_absolute() or ".." in relative.parts:
            raise SystemExit(f"unsafe archive member: {{member.name}}")
        target = (destination / Path(*relative.parts)).resolve()
        if target != destination and destination not in target.parents:
            raise SystemExit(f"unsafe archive member: {{member.name}}")
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not member.isfile():
            raise SystemExit(f"unsupported archive member: {{member.name}}")
        target.parent.mkdir(parents=True, exist_ok=True)
        extracted = archive.extractfile(member)
        if extracted is None:
            raise SystemExit(f"could not read archive member: {{member.name}}")
        with extracted, target.open("wb") as output:
            shutil.copyfileobj(extracted, output)
        target.chmod(member.mode & 0o777)
PYUNTAR

bash "$TMP/{name}/install.sh" "$@"
exit $?
: <<'__ORCHBRIDGE_PAYLOAD_SHELL__'
{marker}
{payload_lines}
{end_marker}
__ORCHBRIDGE_PAYLOAD_SHELL__
'''


def build_release(root: Path = ROOT, dist_dir: Path | None = None) -> tuple[Path, Path, Path, Path]:
    root = root.resolve()
    version = str(json.loads((root / "VERSION.json").read_text(encoding="utf-8"))["release"])
    name = f"orchbridge-v{version}"
    dist = (dist_dir or root / "dist").resolve()
    dist.mkdir(parents=True, exist_ok=True)

    raw_archive = _write_deterministic_tar(root, release_files(root))
    archive_path = dist / f"{name}.tar.gz"
    archive_path.write_bytes(raw_archive)

    raw_zip = _write_deterministic_zip(root, release_files(root))
    zip_path = dist / f"{name}.zip"
    zip_path.write_bytes(raw_zip)

    archive_sha256 = hashlib.sha256(raw_archive).hexdigest()
    installer_path = dist / f"{name}.sh"
    installer_path.write_text(_standalone_installer(name, archive_sha256, raw_archive), encoding="utf-8")
    installer_path.chmod(0o755)

    checksum_path = dist / "SHA256SUMS.txt"
    checksums = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
        for path in (installer_path, archive_path, zip_path)
    ]
    checksum_path.write_text("\n".join(checksums) + "\n", encoding="utf-8")
    return installer_path, archive_path, zip_path, checksum_path


def main() -> int:
    for path in build_release():
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

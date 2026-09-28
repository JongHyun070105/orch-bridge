#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import shlex
import threading
import time
from pathlib import Path
from typing import Any

MAIN_LEASE_SECONDS = int(os.environ.get("AI_ORCH_MAIN_LEASE_SECONDS", "90"))
MAIN_HEARTBEAT_SECONDS = int(os.environ.get("AI_ORCH_MAIN_HEARTBEAT_SECONDS", "15"))


def now() -> float:
    return time.time()


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def process_command(pid: int) -> str | None:
    if pid <= 0:
        return None
    try:
        p = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return None
    if p.returncode != 0:
        return None
    text = p.stdout.strip()
    return text or None


def process_pgid(pid: int) -> int | None:
    if pid <= 0:
        return None
    try:
        return int(os.getpgid(pid))
    except Exception:
        return None


def _canonical_path(value: str | Path) -> str:
    """Return a stable filesystem identity across macOS /var -> /private/var aliases."""
    return os.path.realpath(os.path.abspath(os.path.expanduser(str(value))))


def _argv_value(argv: list[str], flag: str) -> str | None:
    try:
        idx = argv.index(flag)
    except ValueError:
        return None
    return argv[idx + 1] if idx + 1 < len(argv) else None


def command_matches_worker(command: str, *, job_id: str, repo: str, result_file: str) -> bool:
    """Prove ownership from argv semantics, not raw path spelling.

    macOS commonly exposes the same temporary directory as both /var/... and
    /private/var/.... A raw substring comparison therefore produces false
    LIVE_UNKNOWN classifications even though the worker is the correct process.
    """
    command = command or ""
    try:
        argv = shlex.split(command)
    except Exception:
        argv = command.split()
    if not argv or not any(Path(token).name == "ai_job_worker.py" for token in argv):
        return False

    cmd_job = _argv_value(argv, "--job-id")
    cmd_repo = _argv_value(argv, "--repo")
    cmd_result = _argv_value(argv, "--result-file")
    if cmd_job != job_id or not cmd_repo or not cmd_result:
        return False

    return (
        _canonical_path(cmd_repo) == _canonical_path(repo)
        and _canonical_path(cmd_result) == _canonical_path(result_file)
    )


def classify_main_worker(
    lease: dict[str, Any],
    *,
    expected_job_id: str,
    expected_repo: str,
) -> dict[str, Any]:
    """Classify a persisted MAIN worker without trusting PID existence alone.

    status values:
      LIVE_OWNED   pid exists and command line matches this exact job/repo/result
      LIVE_UNKNOWN pid exists but process command cannot be inspected safely
      STALE_PID    pid exists but is not this worker (PID reuse/foreign process)
      DEAD         pid does not exist or lease has no usable pid
    """
    pid = int(lease.get("pid") or 0)
    pgid = int(lease.get("pgid") or 0)
    result_file = str(lease.get("result_file") or "")
    job_id = str(lease.get("job_id") or "")
    repo = str(lease.get("repo") or "")

    base = {
        "pid": pid,
        "pgid": pgid,
        "job_id": job_id,
        "repo": repo,
        "result_file": result_file,
        "run_number": int(lease.get("run_number") or 0),
        "lease_state": str(lease.get("state") or ""),
        "heartbeat_at": float(lease.get("heartbeat_at") or 0),
        "lease_until": float(lease.get("lease_until") or 0),
    }

    if pid <= 0 or not pid_alive(pid):
        return {**base, "status": "DEAD", "command": None}

    command = process_command(pid)
    if command is None:
        # Conservative: do not launch a second worker merely because ps failed.
        return {**base, "status": "LIVE_UNKNOWN", "command": None}

    expected_repo_resolved = _canonical_path(expected_repo)
    metadata_matches = (
        job_id == expected_job_id
        and _canonical_path(repo) == expected_repo_resolved
        and bool(result_file)
    )
    if not metadata_matches:
        return {**base, "status": "STALE_PID", "command": command}

    if "ai_job_worker.py" not in command:
        # A definitely different process now owns this PID.
        return {**base, "status": "STALE_PID", "command": command}

    if command_matches_worker(
        command,
        job_id=expected_job_id,
        repo=expected_repo_resolved,
        result_file=result_file,
    ):
        return {**base, "status": "LIVE_OWNED", "command": command}

    # It still looks like an orchestrator worker, but ps output may be truncated or
    # we may be observing another run during a transition. Fail closed: never
    # launch a duplicate merely because full ownership cannot be proven.
    return {**base, "status": "LIVE_UNKNOWN", "command": command}


def make_main_lease(
    *,
    job_id: str,
    repo: str,
    pid: int,
    pgid: int | None,
    run_number: int,
    prompt_sha256: str,
    result_file: str,
    state: str = "RUNNING",
    exit_code: int | None = None,
) -> dict[str, Any]:
    ts = now()
    data: dict[str, Any] = {
        "schema_version": 1,
        "job_id": job_id,
        "repo": _canonical_path(repo),
        "pid": int(pid),
        "pgid": int(pgid or pid),
        "run_number": int(run_number),
        "prompt_sha256": prompt_sha256,
        "result_file": _canonical_path(result_file),
        "state": state,
        "heartbeat_at": ts,
        "lease_until": ts + MAIN_LEASE_SECONDS,
        "updated_at": ts,
    }
    if exit_code is not None:
        data["exit_code"] = int(exit_code)
    return data


def write_main_lease(path: Path, **kwargs: Any) -> dict[str, Any]:
    data = make_main_lease(**kwargs)
    atomic_json(path, data)
    return data


def start_main_heartbeat(
    lease_path: Path,
    *,
    job_id: str,
    repo: str,
    pid: int,
    pgid: int | None,
    run_number: int,
    prompt_sha256: str,
    result_file: str,
) -> tuple[threading.Event, threading.Thread]:
    stop = threading.Event()

    def beat() -> None:
        while not stop.is_set():
            try:
                write_main_lease(
                    lease_path,
                    job_id=job_id,
                    repo=repo,
                    pid=pid,
                    pgid=pgid,
                    run_number=run_number,
                    prompt_sha256=prompt_sha256,
                    result_file=result_file,
                    state="RUNNING",
                )
            except Exception:
                pass
            stop.wait(max(3, MAIN_HEARTBEAT_SECONDS))

    thread = threading.Thread(
        target=beat,
        name=f"orch-main-lease-{job_id}",
        daemon=True,
    )
    thread.start()
    return stop, thread


def finish_main_heartbeat(
    lease_path: Path,
    stop: threading.Event | None,
    thread: threading.Thread | None,
    *,
    job_id: str,
    repo: str,
    pid: int,
    pgid: int | None,
    run_number: int,
    prompt_sha256: str,
    result_file: str,
    exit_code: int,
) -> None:
    if stop is not None:
        stop.set()
    if thread is not None:
        thread.join(timeout=max(1, MAIN_HEARTBEAT_SECONDS + 1))
    try:
        write_main_lease(
            lease_path,
            job_id=job_id,
            repo=repo,
            pid=pid,
            pgid=pgid,
            run_number=run_number,
            prompt_sha256=prompt_sha256,
            result_file=result_file,
            state="EXITED",
            exit_code=exit_code,
        )
    except Exception:
        pass


def discover_legacy_main_worker(job_dir: Path, repo: Path) -> dict[str, Any] | None:
    """Find a pre-v5 ai_job_worker whose argv references this exact job directory.

    This exists only to make the v4 -> v5 upgrade safe while an old worker is still
    running. It never matches on repo name alone.
    """
    try:
        p = subprocess.run(
            ["ps", "-axo", "pid=,pgid=,command="],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return None
    if p.returncode != 0:
        return None

    job_token = _canonical_path(job_dir)
    repo_token = _canonical_path(repo)
    for line in p.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            pgid = int(parts[1])
        except Exception:
            continue
        command = parts[2]
        try:
            argv = shlex.split(command)
        except Exception:
            argv = command.split()
        if not any(Path(token).name == "ai_job_worker.py" for token in argv):
            continue
        if not pid_alive(pid):
            continue
        cmd_repo = _argv_value(argv, "--repo")
        task_file = _argv_value(argv, "--task-file")
        result_file = _argv_value(argv, "--result-file") or ""
        if not cmd_repo or _canonical_path(cmd_repo) != repo_token:
            continue
        # An exact job-directory match prevents adopting a worker from another job.
        candidate_job_paths = [x for x in (task_file, result_file) if x]
        if not any(_canonical_path(Path(x).parent) == job_token for x in candidate_job_paths):
            continue
        if not result_file:
            candidates = sorted(job_dir.glob("run-*-result.json"), reverse=True)
            result_file = str(candidates[0]) if candidates else str(job_dir / "run-unknown-result.json")
        return {
            "pid": pid,
            "pgid": pgid,
            "command": command,
            "result_file": result_file,
        }
    return None

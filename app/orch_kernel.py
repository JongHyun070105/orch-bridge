#!/usr/bin/env python3
"""OrchBridge durable orchestration control plane.

This module deliberately contains no provider/model calls. It owns deterministic
runtime state that must survive crashes and must be testable without network access.
Supported platforms are macOS and Linux.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

KERNEL_VERSION = "1.4.0"
EVENT_SCHEMA = 1
BINDING_SCHEMA = 1
GOAL_SCHEMA = 1
WORKTREE_SCHEMA = 1
WORKFLOW_SCHEMA = 1

_SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|secret|password|passwd|authorization|bearer|access[_-]?token|"
    r"refresh[_-]?token|private[_-]?key|aws[_-]?(?:access|secret))",
    re.I,
)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_KEYLIKE_RE = re.compile(
    r"\b(?:sk|ghp|github_pat|xox[baprs])-?[A-Za-z0-9_-]{16,}\b", re.I
)


def now() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts if ts is not None else now()).astimezone().isoformat(timespec="seconds")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def atomic_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}-{time.time_ns()}")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


@contextmanager
def file_lock(path: Path, *, exclusive: bool = True) -> Iterator[None]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def redact(value: Any, *, key: str = "") -> Any:
    """Best-effort credential redaction for durable/exported runtime records."""
    if _SECRET_KEY_RE.search(key or ""):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        text = _BEARER_RE.sub("Bearer [REDACTED]", value)
        text = _JWT_RE.sub("[REDACTED_JWT]", text)
        return _KEYLIKE_RE.sub("[REDACTED_KEY]", text)
    return value


def _run_git(repo: Path, *args: str, timeout: int = 8) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.rstrip()


def canonical_repo(path: Path) -> Path:
    root = _run_git(Path(path).expanduser().resolve(), "rev-parse", "--show-toplevel")
    return Path(root).expanduser().resolve()


def normalize_remote(raw: str) -> str:
    raw = str(raw or "").strip()
    if not raw:
        return ""
    if raw.startswith("git@") and ":" in raw:
        host_path = raw[4:]
        host, path = host_path.split(":", 1)
        raw = f"{host.lower()}/{path}"
    elif "://" in raw:
        try:
            from urllib.parse import urlsplit
            url = urlsplit(raw)
            raw = f"{(url.hostname or '').lower()}/{url.path.lstrip('/')}"
        except Exception:
            pass
    if raw.endswith(".git"):
        raw = raw[:-4]
    return raw.rstrip("/")


@dataclass(frozen=True)
class RepoIdentity:
    root: str
    remote_origin: str
    head: str
    branch: str
    dirty_count: int

    @classmethod
    def capture(cls, repo: Path) -> "RepoIdentity":
        root = canonical_repo(repo)
        try:
            remote = normalize_remote(_run_git(root, "remote", "get-url", "origin"))
        except Exception:
            remote = ""
        head = _run_git(root, "rev-parse", "HEAD")
        branch = _run_git(root, "branch", "--show-current") or "detached"
        status = _run_git(root, "status", "--porcelain=v1", "--untracked-files=all")
        dirty = len([line for line in status.splitlines() if line.strip()])
        return cls(str(root), remote, head, branch, dirty)

    def matches_binding(self, binding: dict[str, Any]) -> tuple[bool, str]:
        expected_root = str(binding.get("repo_root") or binding.get("repo") or "")
        if expected_root:
            try:
                if Path(expected_root).expanduser().resolve() != Path(self.root).resolve():
                    return False, f"repo root mismatch: {self.root} != {expected_root}"
            except Exception:
                return False, "repo root could not be normalized"
        expected_remote = normalize_remote(str(binding.get("remote_origin") or ""))
        if expected_remote and self.remote_origin and expected_remote != self.remote_origin:
            return False, f"remote mismatch: {self.remote_origin} != {expected_remote}"
        return True, "ok"


def build_project_binding(
    *,
    repo: Path,
    workspace_id: str | None,
    project_base: Path,
    project_name: str | None,
    prompt_sha256: str,
) -> dict[str, Any]:
    identity = RepoIdentity.capture(repo)
    return {
        "schema": BINDING_SCHEMA,
        "project_id": str(workspace_id or ""),
        "workspace_id": str(workspace_id or ""),
        "project_name": str(project_name or Path(identity.root).name),
        "project_base": str(Path(project_base).expanduser().resolve()),
        "repo_root": identity.root,
        "remote_origin": identity.remote_origin,
        "prompt_sha256": str(prompt_sha256),
    }


def binding_digest(binding: dict[str, Any]) -> str:
    material = {
        key: binding.get(key)
        for key in (
            "schema", "project_id", "workspace_id", "project_name",
            "project_base", "repo_root", "remote_origin", "prompt_sha256",
        )
    }
    return hashlib.sha256(_canonical_json(material)).hexdigest()


def validate_project_binding(
    *,
    binding: dict[str, Any],
    digest: str | None,
    repo: Path,
    workspace_id: str | None,
    project_base: Path,
    prompt_sha256: str,
) -> tuple[bool, str]:
    if not isinstance(binding, dict) or int(binding.get("schema") or 0) != BINDING_SCHEMA:
        return False, "PROJECT_BINDING_MISMATCH: missing/unsupported binding"
    actual_digest = binding_digest(binding)
    if digest and str(digest) != actual_digest:
        return False, "PROJECT_BINDING_MISMATCH: binding digest differs"
    if str(binding.get("prompt_sha256") or "") != str(prompt_sha256):
        return False, "PROJECT_BINDING_MISMATCH: prompt hash differs"
    if str(binding.get("workspace_id") or "") != str(workspace_id or ""):
        return False, "PROJECT_BINDING_MISMATCH: workspace differs"
    try:
        expected_base = Path(str(binding.get("project_base") or "")).expanduser().resolve()
        if expected_base != Path(project_base).expanduser().resolve():
            return False, "PROJECT_BINDING_MISMATCH: project state root differs"
    except Exception:
        return False, "PROJECT_BINDING_MISMATCH: project state root invalid"
    identity = RepoIdentity.capture(repo)
    ok, reason = identity.matches_binding(binding)
    if not ok:
        return False, "PROJECT_BINDING_MISMATCH: " + reason
    return True, "ok"


def pid_alive(pid: int) -> bool:
    if pid <= 1:
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


class MainCheckoutLeaseManager:
    """One durable writer owner per canonical Git checkout."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for_repo(self, repo: Path) -> Path:
        root = canonical_repo(repo)
        key = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:32]
        return self.root / f"{key}.json"

    def _active(self, data: dict[str, Any]) -> bool:
        job_dir = Path(str(data.get("job_dir") or ""))
        if job_dir:
            job = load_json(job_dir / "job.json", {})
            if isinstance(job, dict):
                status = str(job.get("status") or "").upper()
                if status and status not in {
                    "COMPLETE", "FAILED", "CANCELLED", "BLOCKED", "NEEDS_USER", "NEEDS_GO"
                }:
                    return True
        worker_pid = int(data.get("worker_pid") or 0)
        if pid_alive(worker_pid):
            return True
        launcher_pid = int(data.get("launcher_pid") or 0)
        try:
            age = max(0.0, now() - float(data.get("created_at_epoch") or 0))
        except Exception:
            age = 999.0
        return pid_alive(launcher_pid) and age < 30.0

    def acquire(self, *, repo: Path, job_id: str, job_dir: Path) -> str:
        path = self.path_for_repo(repo)
        token = uuid.uuid4().hex
        payload = {
            "schema": 1,
            "token": token,
            "job_id": str(job_id),
            "job_dir": str(Path(job_dir).expanduser().resolve()),
            "repo_root": str(canonical_repo(repo)),
            "launcher_pid": os.getpid(),
            "worker_pid": 0,
            "created_at": iso(),
            "created_at_epoch": now(),
        }
        for _ in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(fd, (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
                return token
            except FileExistsError:
                old = load_json(path, {})
                if isinstance(old, dict) and self._active(old):
                    raise RuntimeError(
                        "CHECKOUT_LEASE_BUSY: "
                        f"repo={payload['repo_root']} owner={old.get('job_id')}"
                    )
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        raise RuntimeError("CHECKOUT_LEASE_BUSY: could not acquire checkout lease")

    def refresh_worker(self, *, repo: Path, token: str, worker_pid: int) -> None:
        path = self.path_for_repo(repo)
        with file_lock(path.with_suffix(".lock")):
            data = load_json(path, {})
            if not isinstance(data, dict) or str(data.get("token") or "") != str(token):
                raise RuntimeError("CHECKOUT_LEASE_LOST")
            data["worker_pid"] = int(worker_pid)
            data["updated_at"] = iso()
            atomic_json(path, data)

    def release(self, *, repo: Path, token: str | None) -> bool:
        path = self.path_for_repo(repo)
        if not path.exists():
            return True
        with file_lock(path.with_suffix(".lock")):
            data = load_json(path, {})
            if token and isinstance(data, dict) and str(data.get("token") or "") != str(token):
                return False
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return True


def find_live_prompt_collision(jobs_dir: Path, prompt_sha256: str) -> dict[str, Any] | None:
    for job_dir in sorted(Path(jobs_dir).glob("job-*"), reverse=True):
        job = load_json(job_dir / "job.json", {})
        if not isinstance(job, dict) or str(job.get("prompt_sha256") or "") != str(prompt_sha256):
            continue
        status = str(job.get("status") or "").upper()
        if status in {"COMPLETE", "FAILED", "CANCELLED", "BLOCKED", "NEEDS_USER", "NEEDS_GO"}:
            continue
        lease = load_json(job_dir / "main-worker-lease.json", {})
        worker_pid = int(lease.get("pid") or 0) if isinstance(lease, dict) else 0
        if pid_alive(worker_pid) or status in {
            "READY", "RUNNING", "PAUSED_QUOTA", "PAUSED_RETRY", "PAUSED_USER", "PREPARING"
        }:
            return {
                "job_id": job.get("id") or job_dir.name,
                "repo": job.get("repo"),
                "status": status,
            }
    return None


class EventJournal:
    """Append-only, hash-chained JSONL event journal with a durable tail anchor."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.anchor_path = self.path.with_name(self.path.name + ".anchor.json")
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    @staticmethod
    def _record_hash(record_without_hash: dict[str, Any]) -> str:
        return hashlib.sha256(_canonical_json(record_without_hash)).hexdigest()

    def _read_unlocked(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for idx, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception as exc:
                raise RuntimeError(f"invalid journal JSON at line {idx}: {exc}") from exc
            if not isinstance(row, dict):
                raise RuntimeError(f"invalid journal record at line {idx}")
            rows.append(row)
        return rows

    def verify(self) -> tuple[bool, str]:
        with file_lock(self.lock_path, exclusive=False):
            rows = self._read_unlocked()
            prev_hash = "GENESIS"
            expected_seq = 1
            for idx, row in enumerate(rows, 1):
                if int(row.get("seq") or 0) != expected_seq:
                    return False, f"sequence mismatch at line {idx}"
                if str(row.get("prev_hash") or "") != prev_hash:
                    return False, f"prev_hash mismatch at line {idx}"
                stored = str(row.get("hash") or "")
                material = dict(row)
                material.pop("hash", None)
                if stored != self._record_hash(material):
                    return False, f"hash mismatch at line {idx}"
                prev_hash = stored
                expected_seq += 1
            anchor = load_json(self.anchor_path, {})
            if rows and isinstance(anchor, dict) and anchor:
                last = rows[-1]
                if int(anchor.get("seq") or 0) != int(last.get("seq") or 0):
                    return False, "tail anchor sequence mismatch"
                if str(anchor.get("hash") or "") != str(last.get("hash") or ""):
                    return False, "tail anchor hash mismatch"
            return True, "ok"

    def read(self) -> list[dict[str, Any]]:
        ok, reason = self.verify()
        if not ok:
            raise RuntimeError("V14_JOURNAL_INTEGRITY_FAILURE: " + reason)
        with file_lock(self.lock_path, exclusive=False):
            return self._read_unlocked()

    def append(self, event: str, **fields: Any) -> dict[str, Any]:
        event = str(event or "").strip()
        if not event:
            raise ValueError("event is required")
        with file_lock(self.lock_path):
            rows = self._read_unlocked()
            prev_hash = "GENESIS"
            expected_seq = 1
            for idx, row in enumerate(rows, 1):
                if int(row.get("seq") or 0) != expected_seq:
                    raise RuntimeError(f"journal sequence mismatch before append at line {idx}")
                if str(row.get("prev_hash") or "") != prev_hash:
                    raise RuntimeError(f"journal prev_hash mismatch before append at line {idx}")
                stored = str(row.get("hash") or "")
                material = dict(row)
                material.pop("hash", None)
                if stored != self._record_hash(material):
                    raise RuntimeError(f"journal hash mismatch before append at line {idx}")
                prev_hash = stored
                expected_seq += 1

            anchor = load_json(self.anchor_path, {})
            if rows and isinstance(anchor, dict) and anchor:
                last = rows[-1]
                if int(anchor.get("seq") or 0) != int(last.get("seq") or 0) or str(anchor.get("hash") or "") != str(last.get("hash") or ""):
                    raise RuntimeError("journal tail anchor mismatch before append")

            record = {
                "schema": EVENT_SCHEMA,
                "seq": len(rows) + 1,
                "event": event,
                "timestamp": iso(),
                "timestamp_epoch": now(),
                "prev_hash": prev_hash,
                **redact(fields),
            }
            record["hash"] = self._record_hash(record)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            atomic_json(
                self.anchor_path,
                {"schema": EVENT_SCHEMA, "seq": record["seq"], "hash": record["hash"], "updated_at": iso()},
            )
            return record

    def intent(self, operation: str, *, idempotency_key: str, **fields: Any) -> dict[str, Any]:
        intent_id = uuid.uuid4().hex
        return self.append(
            "side_effect.intent",
            intent_id=intent_id,
            operation=operation,
            idempotency_key=idempotency_key,
            **fields,
        )

    def terminal(self, intent_id: str, status: str, **fields: Any) -> dict[str, Any]:
        return self.append(
            "side_effect.terminal",
            intent_id=str(intent_id),
            status=str(status),
            **fields,
        )

    def unresolved_intents(self) -> list[dict[str, Any]]:
        rows = self.read()
        terminal = {
            str(row.get("intent_id") or "")
            for row in rows
            if row.get("event") == "side_effect.terminal"
        }
        return [
            row for row in rows
            if row.get("event") == "side_effect.intent"
            and str(row.get("intent_id") or "") not in terminal
        ]


class GoalStore:
    def __init__(self, job_dir: Path):
        self.job_dir = Path(job_dir)
        self.goal_path = self.job_dir / "goal.json"
        self.todo_path = self.job_dir / "todo.json"
        self.lock_path = self.job_dir / ".goal.lock"

    def initialize(self, objective: str) -> None:
        with file_lock(self.lock_path):
            if not self.goal_path.exists():
                atomic_json(self.goal_path, {
                    "schema": GOAL_SCHEMA,
                    "objective": str(objective),
                    "acceptance": [],
                    "progress": [],
                    "completion_audit": None,
                    "created_at": iso(),
                    "updated_at": iso(),
                })
            if not self.todo_path.exists():
                atomic_json(self.todo_path, {"schema": GOAL_SCHEMA, "items": [], "updated_at": iso()})

    def read_goal(self) -> dict[str, Any]:
        data = load_json(self.goal_path, {})
        return data if isinstance(data, dict) else {}

    def read_todo(self) -> dict[str, Any]:
        data = load_json(self.todo_path, {})
        return data if isinstance(data, dict) else {}

    def add_todo(self, text: str, *, acceptance: str = "") -> dict[str, Any]:
        with file_lock(self.lock_path):
            data = self.read_todo() or {"schema": GOAL_SCHEMA, "items": []}
            item = {
                "todo_id": uuid.uuid4().hex[:12],
                "text": str(text),
                "acceptance": str(acceptance),
                "status": "todo",
                "created_at": iso(),
            }
            data.setdefault("items", []).append(item)
            data["updated_at"] = iso()
            atomic_json(self.todo_path, data)
            return item

    def set_completion_audit(self, audit: dict[str, Any]) -> None:
        with file_lock(self.lock_path):
            data = self.read_goal()
            data["completion_audit"] = redact(audit)
            data["updated_at"] = iso()
            atomic_json(self.goal_path, data)


class WorktreeOwnershipRegistry:
    def __init__(self, state_root: Path):
        self.path = Path(state_root) / "worktree-ownership.json"
        self.lock_path = self.path.with_suffix(".lock")

    def register(self, record: dict[str, Any]) -> dict[str, Any]:
        worktree_id = str(record.get("worktree_id") or "")
        if not worktree_id:
            raise ValueError("worktree_id is required")
        with file_lock(self.lock_path):
            data = load_json(self.path, {"schema": WORKTREE_SCHEMA, "worktrees": {}})
            worktrees = data.setdefault("worktrees", {})
            old = worktrees.get(worktree_id)
            if isinstance(old, dict):
                same_owner = (
                    old.get("owner_job_id") == record.get("owner_job_id")
                    and old.get("owner_agent_id") == record.get("owner_agent_id")
                )
                if not same_owner:
                    raise RuntimeError("WORKTREE_OWNERSHIP_EXISTS")
            merged = {
                "schema": WORKTREE_SCHEMA,
                "created_at": old.get("created_at") if isinstance(old, dict) else iso(),
                **(old if isinstance(old, dict) else {}),
                **record,
                "updated_at": iso(),
            }
            worktrees[worktree_id] = merged
            data["updated_at"] = iso()
            atomic_json(self.path, data)
            return merged

    @staticmethod
    def cleanup_decision(
        *, ownership_proven: bool, dirty: bool, unique_commits: int, remote_confirmed: bool
    ) -> str:
        if not ownership_proven:
            return "QUARANTINE"
        if dirty or unique_commits > 0 and not remote_confirmed:
            return "PRESERVE"
        return "DELETE_ELIGIBLE"


class WorkflowStore:
    def __init__(self, job_dir: Path):
        self.path = Path(job_dir) / "workflow.json"
        self.lock_path = self.path.with_suffix(".lock")

    def initialize(self, *, capacity: int = 4) -> dict[str, Any]:
        with file_lock(self.lock_path):
            data = load_json(self.path, None)
            if isinstance(data, dict):
                return data
            data = {
                "schema": WORKFLOW_SCHEMA,
                "capacity": max(1, int(capacity)),
                "owner": None,
                "nodes": {},
                "updated_at": iso(),
            }
            atomic_json(self.path, data)
            return data

    def set_owner(self, owner: str) -> None:
        with file_lock(self.lock_path):
            data = load_json(self.path, {"schema": WORKFLOW_SCHEMA, "capacity": 4, "nodes": {}})
            existing = data.get("owner")
            if existing and existing != owner:
                raise RuntimeError("WORKFLOW_OWNER_BUSY")
            data["owner"] = str(owner)
            data["updated_at"] = iso()
            atomic_json(self.path, data)

    def transition(self, node_id: str, status: str, **fields: Any) -> dict[str, Any]:
        with file_lock(self.lock_path):
            data = load_json(self.path, {"schema": WORKFLOW_SCHEMA, "capacity": 4, "nodes": {}})
            nodes = data.setdefault("nodes", {})
            node = dict(nodes.get(node_id) or {})
            node.update({"node_id": str(node_id), "status": str(status), "updated_at": iso(), **redact(fields)})
            nodes[str(node_id)] = node
            data["updated_at"] = iso()
            atomic_json(self.path, data)
            return node


@dataclass
class CompletionVerification:
    accepted: bool
    checks: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_completion(
    *,
    job: dict[str, Any],
    job_dir: Path,
    repo: Path,
    reported_status: str | None,
    rc: int,
    journal: EventJournal,
) -> CompletionVerification:
    checks: dict[str, bool] = {}
    errors: list[str] = []
    checks["process_success"] = int(rc) == 0
    checks["explicit_complete"] = str(reported_status or "").upper() == "COMPLETE"

    prompt_path = Path(job_dir) / "original-prompt.md"
    try:
        prompt_sha = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
    except Exception:
        prompt_sha = ""
    checks["prompt_hash"] = bool(prompt_sha) and prompt_sha == str(job.get("prompt_sha256") or "")

    ok_journal, journal_reason = journal.verify()
    checks["journal_integrity"] = ok_journal
    if not ok_journal:
        errors.append("journal: " + journal_reason)

    unresolved = journal.unresolved_intents() if ok_journal else []
    checks["side_effects_resolved"] = not unresolved
    if unresolved:
        errors.append(f"{len(unresolved)} side effect intent(s) have no terminal record")

    try:
        identity = RepoIdentity.capture(repo)
        binding = job.get("project_binding") if isinstance(job.get("project_binding"), dict) else {}
        ok_binding, reason = identity.matches_binding(binding)
        checks["repo_identity"] = ok_binding
        if not ok_binding:
            errors.append(reason)
    except Exception as exc:
        checks["repo_identity"] = False
        errors.append(f"repo identity check failed: {exc}")

    for name, passed in checks.items():
        if not passed and name not in {"journal_integrity", "side_effects_resolved", "repo_identity"}:
            errors.append(name + " failed")

    return CompletionVerification(accepted=all(checks.values()), checks=checks, errors=errors)


class ObserverEngine:
    @staticmethod
    def repo_identity_proposal(*, repo: Path, binding: dict[str, Any]) -> dict[str, Any]:
        try:
            identity = RepoIdentity.capture(repo)
            ok, reason = identity.matches_binding(binding)
        except Exception as exc:
            return {"action": "BLOCK", "reason": f"REPO_IDENTITY_MISMATCH: {exc}"}
        if not ok:
            return {"action": "BLOCK", "reason": "REPO_IDENTITY_MISMATCH: " + reason}
        return {"action": "ALLOW", "reason": "repo identity matches immutable binding"}


def export_redacted(job_dir: Path) -> dict[str, Any]:
    job_dir = Path(job_dir)
    out: dict[str, Any] = {}
    for name in ("job.json", "goal.json", "todo.json", "workflow.json"):
        data = load_json(job_dir / name, None)
        if data is not None:
            out[name] = redact(data)
    journal = EventJournal(job_dir / "journal-v14.jsonl")
    if journal.path.exists():
        out["journal"] = redact(journal.read())
    return out

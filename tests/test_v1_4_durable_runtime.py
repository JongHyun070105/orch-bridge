from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from orch_kernel import (  # noqa: E402
    EventJournal,
    GoalStore,
    MainCheckoutLeaseManager,
    WorktreeOwnershipRegistry,
    WorkflowStore,
    binding_digest,
    build_project_binding,
    validate_project_binding,
    verify_completion,
)


def _git(repo: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return p.stdout.strip()


def _repo(path: Path, name: str) -> Path:
    repo = path / name
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "OrchBridge Test")
    _git(repo, "config", "user.email", "orchbridge-test.invalid")
    (repo / "README.md").write_text("# test\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "remote", "add", "origin", f"https://example.invalid/acme/{name}.git")
    return repo


def test_v14_metadata_and_public_component_inventory() -> None:
    version = json.loads((ROOT / "VERSION.json").read_text())
    manifest = json.loads((ROOT / ".release-please-manifest.json").read_text())
    assert version["release"] == manifest["."]
    for component in (
        "ai_chat_tui",
        "ai_orch",
        "settings_cli",
        "phase1_supervisor",
        "phase3_orchestrator",
        "orch_kernel",
    ):
        assert version["components"][component] == "1.4.0"
    for key in (
        "project_binding_schema",
        "main_checkout_lease_schema",
        "event_journal_schema",
        "goal_schema",
        "worktree_ownership_schema",
        "workflow_state_schema",
        "completion_verification_schema",
        "compact_ui_schema",
    ):
        assert version[key] == 1
    assert (APP / "orch_kernel.py").is_file()


def test_agy_claude_55_effort_is_consistent_across_runtime() -> None:
    p1 = (APP / "phase1_supervisor.py").read_text()
    p3 = (APP / "phase3_orchestrator.py").read_text()
    orch = (APP / "ai_orch.py").read_text()
    settings = (APP / "settings_cli.py").read_text()
    tui = (APP / "ai_chat_tui.py").read_text()

    for source in (p1, p3, orch, settings, tui):
        assert "claude-sonnet-4-6-thinking" not in source or source is settings
        assert "claude-opus-4-6-thinking" not in source or source is settings

    for source in (p1, p3, orch):
        assert "claude-sonnet-5-5" in source
        assert "claude-opus-5-5" in source
        assert "--effort" in source

    assert '"sonnet": "claude-sonnet-5-5"' in settings
    assert '"opus": "claude-opus-5-5"' in settings
    assert '"sonnet": {"sonnet", "claude-sonnet-4-6-thinking"}' in settings
    assert '"opus": {"opus", "claude-opus-4-6-thinking"}' in settings


def test_immutable_project_binding_rejects_cross_repo_context(tmp_path: Path) -> None:
    repo_a = _repo(tmp_path, "alpha")
    repo_b = _repo(tmp_path, "beta")
    state = tmp_path / "state"
    state.mkdir()
    prompt_sha = hashlib.sha256(b"village task").hexdigest()

    binding = build_project_binding(
        repo=repo_a,
        workspace_id="alpha-ws",
        project_base=state,
        project_name="alpha",
        prompt_sha256=prompt_sha,
    )
    digest = binding_digest(binding)

    ok, reason = validate_project_binding(
        binding=binding,
        digest=digest,
        repo=repo_a,
        workspace_id="alpha-ws",
        project_base=state,
        prompt_sha256=prompt_sha,
    )
    assert ok, reason

    ok, reason = validate_project_binding(
        binding=binding,
        digest=digest,
        repo=repo_b,
        workspace_id="beta-ws",
        project_base=state,
        prompt_sha256=prompt_sha,
    )
    assert not ok
    assert "PROJECT_BINDING_MISMATCH" in reason


def test_non_git_registered_workspace_has_stable_binding(tmp_path: Path) -> None:
    repo = tmp_path / "plain-workspace"
    repo.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    prompt_sha = hashlib.sha256(b"plain task").hexdigest()

    binding = build_project_binding(
        repo=repo,
        workspace_id="plain-ws",
        project_base=state,
        project_name="plain",
        prompt_sha256=prompt_sha,
    )
    digest = binding_digest(binding)
    assert binding["repo_root"] == str(repo.resolve())
    assert binding["remote_origin"] == ""

    ok, reason = validate_project_binding(
        binding=binding,
        digest=digest,
        repo=repo,
        workspace_id="plain-ws",
        project_base=state,
        prompt_sha256=prompt_sha,
    )
    assert ok, reason


def test_atomic_checkout_lease_blocks_second_main_before_worker(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "repo")
    manager = MainCheckoutLeaseManager(tmp_path / "leases")
    job_a = tmp_path / "job-a"
    job_b = tmp_path / "job-b"
    job_a.mkdir()
    job_b.mkdir()
    (job_a / "job.json").write_text(json.dumps({"status": "READY"}))
    (job_b / "job.json").write_text(json.dumps({"status": "READY"}))

    token = manager.acquire(repo=repo, job_id="job-a", job_dir=job_a)
    with pytest.raises(RuntimeError, match="CHECKOUT_LEASE_BUSY"):
        manager.acquire(repo=repo, job_id="job-b", job_dir=job_b)
    assert manager.owns(repo=repo, token=token)
    assert manager.release(repo=repo, token=token)


def test_stale_pre_spawn_checkout_lease_is_reclaimable(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "stale-repo")
    manager = MainCheckoutLeaseManager(tmp_path / "leases")
    job_a = tmp_path / "job-a"
    job_b = tmp_path / "job-b"
    job_a.mkdir()
    job_b.mkdir()
    (job_a / "job.json").write_text(json.dumps({"status": "READY"}))
    (job_b / "job.json").write_text(json.dumps({"status": "READY"}))

    token = manager.acquire(repo=repo, job_id="job-a", job_dir=job_a)
    lease_path = manager.path_for_repo(repo)
    lease = json.loads(lease_path.read_text())
    lease["launcher_pid"] = 99999999
    lease["worker_pid"] = 0
    lease["created_at_epoch"] = 0
    lease_path.write_text(json.dumps(lease))

    replacement = manager.acquire(repo=repo, job_id="job-b", job_dir=job_b)
    assert replacement != token
    assert manager.owns(repo=repo, token=replacement)
    assert manager.release(repo=repo, token=replacement)


def test_event_journal_detects_mutation_and_clean_tail_truncation(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path / "events.jsonl")
    journal.append("job.created", job_id="j1")
    journal.append("main.worker_launch", job_id="j1")
    assert journal.verify() == (True, "ok")

    lines = journal.path.read_text().splitlines()
    first = json.loads(lines[0])
    first["event"] = "tampered"
    lines[0] = json.dumps(first, sort_keys=True)
    journal.path.write_text("\n".join(lines) + "\n")
    ok, reason = journal.verify()
    assert not ok and "hash mismatch" in reason

    tail = EventJournal(tmp_path / "tail.jsonl")
    tail.append("one")
    tail.append("two")
    tail.path.write_text(tail.path.read_text().splitlines()[0] + "\n")
    ok, reason = tail.verify()
    assert not ok and "anchor" in reason

    missing = EventJournal(tmp_path / "missing-anchor.jsonl")
    missing.append("one")
    missing.anchor_path.unlink()
    ok, reason = missing.verify()
    assert not ok and "anchor missing" in reason


def test_side_effect_intent_requires_terminal_confirmation(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path / "events.jsonl")
    intent = journal.intent("git.branch_plan", idempotency_key="job:branch")
    unresolved = journal.unresolved_intents()
    assert [x["intent_id"] for x in unresolved] == [intent["intent_id"]]
    journal.terminal(intent["intent_id"], "success")
    assert journal.unresolved_intents() == []


def test_goal_todo_and_workflow_updates_are_durable_and_locked(tmp_path: Path) -> None:
    goal = GoalStore(tmp_path)
    goal.initialize("ship v1.4")
    goal.add_todo("run CI")
    goal.add_todo("review feedback")
    assert goal.read_goal()["objective"] == "ship v1.4"
    assert len(goal.read_todo()["items"]) == 2

    workflow = WorkflowStore(tmp_path)
    workflow.initialize(capacity=8)
    workflow.set_owner("job-1")

    def update(i: int) -> None:
        workflow.transition(f"node-{i}", "COMPLETE", index=i)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(update, range(16)))

    data = json.loads(workflow.path.read_text())
    assert data["owner"] == "job-1"
    assert len(data["nodes"]) == 16
    assert all(data["nodes"][f"node-{i}"]["status"] == "COMPLETE" for i in range(16))


def test_completion_gate_requires_repo_prompt_and_journal_proof(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "repo")
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    prompt = "finish safely"
    prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
    (job_dir / "original-prompt.md").write_text(prompt)
    binding = build_project_binding(
        repo=repo,
        workspace_id="ws",
        project_base=tmp_path,
        project_name="repo",
        prompt_sha256=prompt_sha,
    )
    job = {
        "id": "job",
        "prompt_sha256": prompt_sha,
        "project_binding": binding,
        "binding_sha256": binding_digest(binding),
    }
    journal = EventJournal(job_dir / "journal-v14.jsonl")
    journal.append("job.created", job_id="job")

    verdict = verify_completion(
        job=job,
        job_dir=job_dir,
        repo=repo,
        reported_status="COMPLETE",
        rc=0,
        journal=journal,
    )
    assert verdict.accepted, verdict.errors

    (job_dir / "original-prompt.md").write_text("changed")
    verdict = verify_completion(
        job=job,
        job_dir=job_dir,
        repo=repo,
        reported_status="COMPLETE",
        rc=0,
        journal=journal,
    )
    assert not verdict.accepted
    assert not verdict.checks["prompt_hash"]


def test_worktree_ownership_cannot_be_overwritten_and_cleanup_fails_safe(tmp_path: Path) -> None:
    registry = WorktreeOwnershipRegistry(tmp_path)
    registry.register({
        "worktree_id": "job/worker",
        "owner_job_id": "job-a",
        "owner_agent_id": "worker-a",
        "path": "/tmp/example",
    })
    with pytest.raises(RuntimeError, match="WORKTREE_OWNERSHIP_EXISTS"):
        registry.register({
            "worktree_id": "job/worker",
            "owner_job_id": "job-b",
            "owner_agent_id": "worker-b",
            "path": "/tmp/example",
        })

    assert registry.cleanup_decision(
        ownership_proven=False, dirty=False, unique_commits=0, remote_confirmed=False
    ) == "QUARANTINE"
    assert registry.cleanup_decision(
        ownership_proven=True, dirty=True, unique_commits=0, remote_confirmed=False
    ) == "PRESERVE"
    assert registry.cleanup_decision(
        ownership_proven=True, dirty=False, unique_commits=0, remote_confirmed=False
    ) == "DELETE_ELIGIBLE"


def test_tui_binding_order_compact_ui_and_worktree_fail_closed_guards() -> None:
    tui = (APP / "ai_chat_tui.py").read_text()
    p1 = (APP / "phase1_supervisor.py").read_text()

    assert "Footer" not in tui
    assert "composer-label" not in tui
    assert "MESSAGE  ·  Enter send/queue" not in tui
    assert "def compact_route" in tui
    assert "def compact_quota_summary" in tui
    assert '("/ui compact|verbose"' in tui
    assert '"project_binding": binding' in tui
    assert '"binding_sha256": binding_digest(binding)' in tui
    assert "CHECKOUT_LEASE_MANAGER.acquire" in tui
    assert "COMPLETE CLAIM REJECTED" in tui

    new_job = tui.index("    def new_job(")
    acquire = tui.index("CHECKOUT_LEASE_MANAGER.acquire", new_job)
    branch_apply = tui.index("self._branch_apply_plan(branch_plan)", new_job)
    worker_start = tui.index("    def start(", new_job)
    assert acquire < branch_apply < worker_start

    assert "shutil.rmtree(path, ignore_errors=True)" not in p1
    assert "WORKTREE_PATH_OCCUPIED" in p1
    assert "WORKTREE_OWNERSHIP.register" in p1


def test_compact_router_fixture_hides_subscription_breakdown() -> None:
    import ai_chat_tui as tui

    raw = (
        "claude-opus(1.99) > claude-sonnet(1.82)"
    )
    compact = tui.compact_route(raw)
    assert compact == "Claude Code Opus 5.5 (1.99) > Claude Code Sonnet 5.5 (1.82)"
    legacy = tui._compact_route_part(
        "Claude Code Opus 5.5 · subscription(u=1.99,cap=0.99,avail=0.66,recent=-0.07)"
    )
    assert "subscription" not in legacy.lower()
    assert "cap=" not in legacy
    assert "(1.99)" in legacy

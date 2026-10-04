# OrchBridge

**English** | [한국어](README.ko.md)

Local multi-provider coding-agent orchestrator with adaptive routing, safe delegation, persistent queues, Git workflows, and a cross-platform TUI.

[![CI](https://github.com/JongHyun070105/orch-bridge/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/JongHyun070105/orch-bridge/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/JongHyun070105/orch-bridge)](https://github.com/JongHyun070105/orch-bridge/releases)
[![License](https://img.shields.io/github/license/JongHyun070105/orch-bridge)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![macOS and Linux](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey)

OrchBridge runs multiple AI coding CLIs behind one TUI and one routing layer. It can choose a MAIN agent, delegate independent checks, preserve project-scoped state, queue follow-up work, manage safe Git branches, show quota/health information, and notify you when long-running work finishes.

> **v1.3.0** — quality-first routing, persistent scheduled prompts, proactive cross-provider collaboration, Claude Code 5.5 with adaptive effort, global quota-aware fallback, router visibility, transcript auto-follow, and project slot pin/compaction.

## Quick start

```bash
curl -fLO https://github.com/JongHyun070105/orch-bridge/releases/download/v1.3.0/orchbridge-v1.3.0.sh
bash orchbridge-v1.3.0.sh
orch doctor
orch settings detect
cd ~/Projects/my-project
orch tui .
```

In the TUI, start with `/model auto`, `/permissions guarded`, and `/notify smart`.
OrchBridge uses locally installed and authenticated provider CLIs; it does not bundle model credentials.

## Screenshots

Screenshots will be added after the first public release. The TUI includes a project status header, task timeline, queue controls, delegate status, and the Textual command palette.

## Why OrchBridge?

Most coding-agent CLIs are excellent on their own, but real work often needs more than one model: a strong MAIN, a cheap reviewer, an independent vendor cross-check, or a fallback when quota is exhausted. OrchBridge coordinates those tools without requiring provider API keys itself.

### Highlights

- **Adaptive MAIN routing** across installed provider CLIs
- **Native Claude Code subscription support** plus Codex/AGY/Command Code integrations
- **Delegation and cross-vendor review** with bounded concurrency
- **Project-isolated state** so multiple repositories can run independently
- **Persistent FIFO task queue** for “do this next” workflows
- **Safe autonomous branch workflow** with destructive Git operations gated
- **Crash-safe worker ownership**, leases, recovery, and process-group cancellation
- **Desktop notifications** on macOS and Linux
- **Quota/health awareness** with stale-cache fallback rather than fake numbers
- **Persistent same-job steering** with `/steer <instruction>`, checkpointing, and crash-safe resume
- **Provider runtime doctor** that resolves PATH/NVM/login-shell installs and diagnoses broken local CLI installs without auto-repair
- **Terminal-safe prompt submission** that strips leaked ANSI/SGR mouse reports and suppresses accidental duplicate submits/queue entries
- **Project registry cleanup + smart Tab completion** for registered projects and local branches
- **Persistent scheduled prompts** that enter the project FIFO queue when due without interrupting an active job
- **Global provider health fallback** so exhausted Command Code credit is excluded from MAIN/judge/delegate routing until a bounded recovery probe succeeds
- **Role-aware proactive collaboration** that favors independent reviewers/researchers and does not spend useful delegate budget on provider-level blocked calls
- **Claude Code 5.5 defaults + adaptive effort** with visible progress/tool activity but no hidden chain-of-thought exposure
- **Visible ranked router utility** including exclusion reasons, quota availability, capability, and recent-use penalties
- **Project slot pin/auto controls** plus live-window compaction and transcript auto-follow
- **Textual TUI** with a working `Ctrl+P` command palette
- **Portable settings CLI**: `orch settings`

## v1.3 scheduling and routing

Scheduled prompts are project-local and persistent:

```text
/schedule 16:00 review the completed validation
/schedule in 45m check CI and summarize failures
/schedule tomorrow 09:00 prepare the next task
/schedule
/schedule cancel <number|schedule-id>
/schedule clear
```

A due schedule is added to the same persistent FIFO queue used by normal follow-up prompts. It **does not steer or interrupt the currently running job**. If the active job runs past the scheduled time, the scheduled task waits and starts when earlier queue work finishes. If the TUI was stopped, overdue schedules are delivered when that project TUI starts again.

Command Code credit exhaustion is tracked separately from short rate/network failures. A globally exhausted CMD provider is excluded from MAIN routing, the micro-judge, delegates, consults, and parallel workers. The router continues scoring healthy providers and periodically performs a bounded recovery probe.

Project window numbers can be left automatic or pinned explicitly:

```text
/project slot my-project 3
/project slot my-project auto
orch-project slot my-project 3
orch-project slot my-project auto
```

Closing a live unpinned project compacts the remaining live project numbers while pinned slots stay fixed.

## Supported platforms

| Platform | Status | Notifications | Clipboard |
| --- | --- | --- | --- |
| macOS | Supported | `terminal-notifier` or built-in `osascript` | `pbcopy` |
| Linux | Supported | `notify-send` | `wl-copy`, `xclip`, or `xsel` |

Windows is not supported in v1.3. WSL2 may work as a Linux environment but is not part of the v1.3 support contract.

## Requirements

Required:

- Python 3.11+
- Git
- tmux

Optional AI provider CLIs — install any combination:

- `codex` — OpenAI Codex CLI
- `claude` — Claude Code
- `agy` — AGY-compatible CLI
- `cmd` — Command Code

Gemini models are available through the existing AGY provider path. `orch settings detect` resolves configured binaries through the active PATH plus common user/NVM locations and login-shell PATH. `orch doctor` adds version and broken-install diagnostics. Provider authentication remains with the upstream CLI and is confirmed when that CLI handles a task.

OrchBridge does **not** install or store credentials for these providers. Each provider CLI keeps using its own authentication mechanism.

## Quick install

### Release asset

Download `orchbridge-v1.2.0.sh` from the [v1.2.0 GitHub Release](https://github.com/JongHyun070105/orch-bridge/releases/tag/v1.2.0), or fetch it directly:

```bash
bash orchbridge-v1.3.0.sh
```

If Git/tmux/Python support is missing and you want the installer to use Homebrew, apt, dnf, or pacman:

```bash
bash orchbridge-v1.3.0.sh --install-system-deps
```

### Development checkout

```bash
git clone https://github.com/JongHyun070105/orch-bridge.git
cd orch-bridge
bash install.sh
```

The installer creates a dedicated virtual environment under:

```text
~/.local/share/orchbridge/venv
```

and places CLI entrypoints in:

```text
~/.local/bin
```

Remove the runtime with `orch uninstall`. Project queues and job history are kept by default. To remove persistent project state too, run `orch uninstall --purge-state`; the command asks for confirmation unless `--yes` is also supplied.

## First run

Check the environment:

```bash
orch doctor
```

Detect provider CLIs:

```bash
orch settings detect
```

Show settings:

```bash
orch settings
```

Open the current repository in the TUI:

```bash
orch tui .
```

Or run a one-shot task:

```bash
orch run "Inspect this repository and explain the failing tests"
```

## Provider and model settings

OrchBridge keeps portable configuration at:

```text
~/.config/orchbridge/config.json
```

Useful commands:

```bash
orch settings init
orch settings show
orch settings detect

orch settings provider codex auto
orch settings provider claude on
orch settings provider agy off
orch settings provider commandcode auto

orch settings model claude_sonnet sonnet
orch settings model claude_opus opus
orch settings model commandcode xiaomi/mimo-v2.5-pro
orch settings model gemini_high gemini-3.8-flash-high
```

`auto` means “use this provider when its CLI is installed.” Missing or disabled CLIs are removed from routing instead of crashing the run.
Detection does not make provider network calls or inspect credentials. Each upstream CLI remains responsible for authentication, subscriptions, and API access.

## v1.2 input and project UX

If terminal/tmux control reports leak into the composer, OrchBridge removes the ANSI/SGR transport noise and blocks the first submit so you can verify the cleaned prompt before sending it.

Identical prompt + attachment submissions within a short safety window are suppressed so a single Enter/key-repeat cannot create both a running job and an identical queued follow-up.

Registered projects can be removed without deleting their repository or saved OrchBridge state:

```text
/project delete <name>
orch-project delete <name|slot>
```

After deletion, registered project slots are compacted. Tab completion is backed by the real project registry for `/project open|close|delete` and by local Git branches for `/branch switch|next`.

## TUI commands

Common commands include:

```text
/help
/status
/quota
/settings
/doctor
/steer <instruction>
/model auto
/model claude
/model codex
/queue
/branch
/permissions guarded
/permissions trusted
/notify smart
/restart
/reload
/version
```

### Steering an active task

`/steer <instruction>` updates the **same persisted logical job** instead of creating a new queued task. OrchBridge checkpoints the current job, safely terminates the owned MAIN process, persists steering history in order, and resumes with the original task plus the new steering instructions. A normal prompt entered while a task is running still follows the FIFO queue behavior.

### Command palette

Press `Ctrl+P`, select with arrow keys, and press `Enter`.

v1.2 keeps OrchBridge's priority composer bindings disabled while Textual's `CommandPalette` screen is active, so `Enter` reaches the selected palette command instead of being stolen by the composer.

## Permissions

The public edition starts in **guarded** mode.

```text
/permissions guarded
```

For repositories where you intentionally want broader local autonomy:

```text
/permissions trusted
```

Even in trusted mode, OrchBridge keeps destructive actions gated: no automatic force-push, destructive reset/clean, branch deletion, or automatic merge/push.

## Branch workflow

```text
/branch
/branch list
/branch switch feature/foo
/branch new agent/fix-tests
/branch next feature/bar
/branch next-new agent/next-task
```

MAIN agents may create and switch safe branches through the bounded `orch-branch` helper. Delegated workers do not get uncontrolled authoritative-branch mutation.

## Queue workflow

While a MAIN task is running, submit another prompt normally. OrchBridge stores it in a persistent project-scoped FIFO queue.

```text
/queue
/queue add Review the implementation after the tests pass
/queue pause
/queue resume
```

`smart` notification mode suppresses intermediate queue-complete spam and alerts once the final queued task completes, while immediately alerting on `FAILED`, `BLOCKED`, `NEEDS_USER`, or `NEEDS_GO`.

## Desktop notifications

```text
/notify smart
/notify all
/notify important
/notify off
/notify test
```

Terminal-side test:

```bash
orch-notify test --project demo
```

Linux users need `notify-send` for desktop notifications. The installer can install it on common distributions when `--install-system-deps` is used.

## Collaboration helpers

OrchBridge exposes low-level helpers for agents and automation:

```bash
orch-delegate --model auto --task "review this change"
orch-consult --task "independent second opinion"
orch-parallel --count 2 --task "check for correctness risks"
orch-collect --batch latest
orch-handoff --model codex --task "continue this task"
```

The router distinguishes vendors when deciding whether a review is actually independent; two Anthropic-backed Claude routes are not counted as cross-vendor verification.

## Architecture

```text
User / TUI
    |
    v
Adaptive Router
    |-- Codex CLI
    |-- Claude Code
    |-- AGY models
    `-- Command Code

MAIN
    |-- delegate / consult / parallel
    |-- safe branch manager
    |-- queue + crash recovery
    `-- fact / quota / health state

Project state: ~/.local/share/orchbridge/projects/<workspace-id>/
Global config: ~/.config/orchbridge/config.json
Cache:         ~/.cache/orchbridge/
```

## Workflows

The first public release ships workflow examples under `workflows/` for:

- implementation + verification
- independent code review
- research / evidence cross-check

These are reference contracts for contributors and future workflow-runtime work; the existing adaptive router remains the v1 execution engine.
The YAML files are examples/specifications, not executable workflow plans in v1. Delegation, parallel review, routing, and mechanical checks are invoked through existing CLI/TUI behavior.

## Troubleshooting

- **No provider is available:** install and authenticate at least one supported CLI, then run `orch doctor`, `orch settings detect`, and `orch settings show`. Missing local binaries are treated as environment diagnostics and do not trigger a long provider cooldown.
- **Codex looks partially installed:** `orch doctor` reports broken symlinks, npm package/bin mismatches, and stale `.codex-*` directories. Diagnostics are advisory; OrchBridge never deletes or repairs npm/NVM state automatically.
- **Linux install reports missing `python3-venv`:** install the package supplied by your distribution (for Debian/Ubuntu, `python3-venv`) and rerun the installer.
- **Desktop notifications are unavailable:** on Linux install `notify-send` from libnotify and use a desktop session with a notification service. SSH/headless sessions can complete jobs without notifications.
- **Clipboard integration is unavailable:** install `wl-copy`, `xclip`, or `xsel` on Linux. macOS uses `pbcopy`.
- **A task remains paused:** inspect `/status`, `/queue`, and `/quota`; resolve the reported gate before resuming explicitly.

## Limitations

- Windows is not officially supported in v1.3.0.
- Provider capabilities and authentication depend on upstream CLIs, subscriptions, and local setup.
- Quota information is best-effort and can be unavailable or stale.
- Workflow YAML files are reference examples; a declarative workflow execution engine is not included.
- OrchBridge does not merge or push branches automatically.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt pytest
pytest -q
bash -n install.sh uninstall.sh scripts/*.sh
```

CI runs Python 3.11 and 3.13 on Ubuntu and macOS. It uses local fixtures and mocked provider discovery; it does not call paid model APIs. Ubuntu CI also installs the standalone release artifact in a temporary home and exercises install, detection, configuration preservation, and uninstall.

Build release assets:

```bash
bash scripts/build_release.sh
```

## Releases and version bot

The repository uses **Conventional Commits** and Release Please:

- `fix:` → patch candidate
- `feat:` → minor candidate
- `feat!:` / `BREAKING CHANGE:` → major candidate

Pushes to `main` update an automated release PR. Merging that PR creates a Git tag/release, and the release-assets workflow validates the tag, runs release checks, builds the standalone `.sh`, source `.tar.gz`, extract-and-run `.zip`, and checksums, then uploads them to the release. The initial public tag is `v1.0.0`.

## Security and privacy

- OrchBridge does not require provider API keys itself.
- Provider authentication remains owned by provider CLIs.
- Logs redact common token/password field patterns, but contributors should still avoid putting secrets in prompts, filenames, commits, or bug reports.
- The repository contains no machine-specific user paths or private project identifiers by design; CI includes a privacy guard.

See [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).

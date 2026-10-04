# OrchBridge

[English](README.md) | **한국어**

로컬에서 여러 AI 코딩 CLI를 하나의 TUI와 라우팅 계층으로 묶어 사용하는 멀티 프로바이더 코딩 에이전트 오케스트레이터입니다. 적응형 라우팅, 안전한 위임, 영속 큐, Git 워크플로, 크로스플랫폼 TUI를 제공합니다.

[![CI](https://github.com/JongHyun070105/orch-bridge/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/JongHyun070105/orch-bridge/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/JongHyun070105/orch-bridge)](https://github.com/JongHyun070105/orch-bridge/releases)
[![License](https://img.shields.io/github/license/JongHyun070105/orch-bridge)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![macOS and Linux](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey)

OrchBridge는 여러 AI 코딩 CLI를 하나의 TUI와 라우터 뒤에서 조정합니다. MAIN 에이전트를 선택하고, 독립 검증을 다른 모델에 위임하고, 프로젝트별 상태를 보존하며, 후속 작업을 큐에 넣고, 안전한 Git 브랜치 작업과 quota/health 확인, 장시간 작업 완료 알림까지 처리할 수 있습니다.

> **v1.3.0** — 품질 중심 라우팅, 영속 예약 프롬프트, 능동적 크로스 프로바이더 협업, Claude Code 5.5 + 작업별 adaptive effort, 전역 quota fallback, 라우터 점수 표시, 채팅 자동 하단 follow, 프로젝트 slot pin/자동 압축을 추가했습니다.

## 빠른 시작

```bash
curl -fLO https://github.com/JongHyun070105/orch-bridge/releases/download/v1.3.0/orchbridge-v1.3.0.sh
bash orchbridge-v1.3.0.sh
orch doctor
orch settings detect
cd ~/Projects/my-project
orch tui .
```

TUI에서는 보통 `/model auto`, `/permissions guarded`, `/notify smart`로 시작하면 됩니다.

OrchBridge는 로컬에 설치되고 인증된 provider CLI를 사용하며, 모델 자격 증명을 자체 번들에 포함하지 않습니다.

## 주요 기능

- 설치된 provider CLI를 대상으로 하는 **적응형 MAIN 라우팅**
- **Claude Code 구독 방식 지원** 및 Codex / AGY / Command Code 연동
- 제한된 동시성을 사용하는 **delegate / cross-vendor review**
- 저장소마다 분리되는 **프로젝트별 상태**
- 다음 작업을 이어서 실행하는 **영속 FIFO 작업 큐**
- 파괴적인 Git 작업을 제한하는 **안전한 브랜치 워크플로**
- worker ownership, lease, recovery, process-group cancellation을 포함한 **크래시 안전성**
- macOS / Linux **데스크톱 알림**
- 실제 수치가 없을 때 임의 값을 만들지 않는 **quota / health 상태 관리**
- `/steer <instruction>`을 이용한 **동일 작업 영속 steering**
- PATH / NVM / login shell을 확인하고 로컬 CLI 설치 문제를 진단하는 **provider doctor**
- 터미널 ANSI/SGR mouse 제어문자 유입을 제거하고 실수로 중복 전송/큐 등록되는 것을 막는 **입력 안전장치**
- 등록 프로젝트 삭제/번호 재정렬과 프로젝트·브랜치 목록을 이용하는 **스마트 Tab 자동완성**
- 현재 작업을 끊지 않고 due 시점에 FIFO 큐로 들어가는 **영속 예약 프롬프트**
- CMD credit 소진 시 MAIN/judge/delegate/parallel에서 제외하고 다른 모델로 이어가는 **전역 provider health fallback**
- review/research/implementation 역할과 최근 사용량을 반영하는 **능동적 delegate 선택**
- **Claude Code Sonnet/Opus 5.5 기본값 + 작업 난이도별 adaptive effort**
- 내부 chain-of-thought는 노출하지 않으면서 text/tool 진행상황을 보여주는 **Claude Code 진행 표시**
- 실제 utility/capability/availability/recent penalty와 제외 사유를 보여주는 **라우터 점수 패널**
- 번호를 직접 고정하거나 자동 관리할 수 있는 **프로젝트 slot pin/auto + live compact**
- 새 메시지/진행 이벤트를 따라가는 **채팅 자동 하단 follow**
- `Ctrl+P` command palette를 지원하는 **Textual TUI**
- 이식 가능한 설정 CLI: `orch settings`

## 지원 플랫폼

| 플랫폼 | 상태 | 알림 | 클립보드 |
| --- | --- | --- | --- |
| macOS | 지원 | `terminal-notifier` 또는 내장 `osascript` | `pbcopy` |
| Linux | 지원 | `notify-send` | `wl-copy`, `xclip`, `xsel` |

Windows는 v1.3에서 공식 지원하지 않습니다. WSL2는 Linux 환경으로 동작할 수 있지만 v1.3 공식 지원 범위에는 포함되지 않습니다.

## 요구 사항

필수:

- Python 3.11+
- Git
- tmux

선택 AI provider CLI — 필요한 조합만 설치하면 됩니다.

- `codex` — OpenAI Codex CLI
- `claude` — Claude Code
- `agy` — AGY 호환 CLI
- `cmd` — Command Code

Gemini 모델은 기존 AGY provider 경로를 통해 사용할 수 있습니다.

`orch settings detect`는 현재 PATH뿐 아니라 일반적인 사용자 설치 경로, NVM 경로, login shell PATH까지 확인합니다. `orch doctor`는 버전 확인과 깨진 설치 진단을 추가로 수행합니다.

각 provider의 로그인, 구독, API 접근 권한은 해당 provider CLI가 계속 관리합니다. OrchBridge 자체는 provider API 키를 설치하거나 저장하지 않습니다.

## 설치

### Release asset

[v1.3.0 GitHub Release](https://github.com/JongHyun070105/orch-bridge/releases/tag/v1.3.0)에서 `orchbridge-v1.3.0.sh`를 내려받아 실행합니다.

```bash
bash orchbridge-v1.3.0.sh
```

Git / tmux / Python 관련 시스템 의존성이 없고 Homebrew, apt, dnf 또는 pacman을 통해 설치하도록 허용하려면:

```bash
bash orchbridge-v1.3.0.sh --install-system-deps
```

### 개발용 체크아웃

```bash
git clone https://github.com/JongHyun070105/orch-bridge.git
cd orch-bridge
bash install.sh
```

전용 가상환경은 다음 위치에 생성됩니다.

```text
~/.local/share/orchbridge/venv
```

CLI 엔트리포인트는 다음 위치에 설치됩니다.

```text
~/.local/bin
```

런타임만 제거하려면 `orch uninstall`을 사용합니다. 프로젝트 큐와 작업 이력은 기본적으로 유지됩니다.

프로젝트 상태까지 제거하려면:

```bash
orch uninstall --purge-state
```

`--yes`를 함께 쓰지 않으면 확인 절차를 거칩니다.

## 첫 실행

환경 점검:

```bash
orch doctor
```

provider CLI 탐지:

```bash
orch settings detect
```

현재 설정 확인:

```bash
orch settings
```

현재 저장소를 TUI로 열기:

```bash
orch tui .
```

단일 작업 실행:

```bash
orch run "이 저장소를 점검하고 실패 중인 테스트 원인을 설명해줘"
```

## Provider 및 모델 설정

설정 파일:

```text
~/.config/orchbridge/config.json
```

주요 명령:

```bash
orch settings init
orch settings show
orch settings detect

orch settings provider codex auto
orch settings provider claude on
orch settings provider agy off
orch settings provider commandcode auto

orch settings model claude_sonnet claude-sonnet-5-5
orch settings model claude_opus claude-opus-5-5
orch settings model commandcode xiaomi/mimo-v2.5-pro
orch settings model gemini_high gemini-3.8-flash-high
```

`auto`는 해당 CLI가 설치되어 있을 때 provider를 사용한다는 의미입니다. 설치되지 않았거나 비활성화된 CLI는 실행을 깨뜨리는 대신 라우팅 대상에서 제외됩니다.

탐지 과정에서는 provider 네트워크 호출을 하거나 자격 증명을 읽지 않습니다.

## v1.3 예약 작업 / 라우터 / 프로젝트 UX

프로젝트별 예약 프롬프트를 영속적으로 저장할 수 있습니다.

```text
/schedule 16:00 종료된 검증 결과 다시 확인
/schedule in 45m CI 실패 원인 정리
/schedule 내일 09:00 다음 작업 준비
/schedule
/schedule cancel <번호|schedule-id>
/schedule clear
```

예약 시각이 되어도 **현재 실행 중인 작업에 지시를 끼워 넣거나 중단하지 않습니다.** 예약 프롬프트는 persistent FIFO queue에 들어가며, 앞선 작업이 끝난 다음 순서대로 시작됩니다. TUI가 꺼져 있었다면 해당 프로젝트 TUI가 다시 실행될 때 overdue 예약을 큐에 전달합니다.

Command Code가 실제 credit/quota 소진 상태라면 전역 `QUOTA_EXHAUSTED`로 분류해 MAIN, decision micro-judge, delegate/consult/parallel 후보에서 제외합니다. 라우터는 Claude/Gemini/Codex 등 healthy 후보를 계속 점수화하며, 일정 간격의 작은 availability probe가 성공하면 CMD를 자동 복구합니다.

프로젝트 번호는 자동 관리와 고정을 선택할 수 있습니다.

```text
/project slot my-project 3
/project slot my-project auto
orch-project slot my-project 3
orch-project slot my-project auto
```

unpinned LIVE 프로젝트를 닫으면 뒤의 LIVE 프로젝트가 빈 번호를 자동으로 채우고, pinned 번호는 유지됩니다.

### v1.2에서 유지되는 입력 안전 기능

터미널/tmux의 ANSI/SGR mouse 제어 시퀀스가 입력창에 섞이면 첫 Enter에서는 작업을 보내지 않습니다. 제어문자만 제거한 프롬프트를 입력창에 남기고, 사용자가 확인한 뒤 다시 Enter를 눌러야 전송됩니다.

동일한 프롬프트와 첨부 조합이 짧은 시간 안에 다시 submit되면 중복 실행/대기열 등록을 차단합니다.

등록 프로젝트는 실제 저장소와 OrchBridge state를 삭제하지 않고 목록에서만 제거할 수 있습니다.

```text
/project delete <name>
orch-project delete <name|번호>
```

삭제 후 프로젝트 번호는 자동으로 당겨집니다. 또한 `/project open|close|delete`는 실제 등록 프로젝트 목록을, `/branch switch|next`는 실제 로컬 브랜치 목록을 이용해 Tab 자동완성합니다.

## TUI 명령

자주 사용하는 명령:

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

### 실행 중인 작업 방향 수정

`/steer <instruction>`은 새 큐 작업을 만드는 대신 **현재의 동일한 persisted logical job**을 수정합니다.

OrchBridge는 현재 작업을 checkpoint하고, 소유 중인 MAIN 프로세스를 안전하게 종료하고, steering history를 순서대로 저장한 다음 원래 작업과 새로운 지시를 함께 사용해 동일 job을 재개합니다.

실행 중 일반 프롬프트를 입력하는 동작은 기존처럼 FIFO 큐에 추가됩니다.

### Command Palette

`Ctrl+P`를 누르고 방향키로 항목을 선택한 뒤 `Enter`를 누릅니다.

v1.2에서는 Textual의 `CommandPalette` 화면이 활성화된 동안 OrchBridge composer의 우선 바인딩을 비활성화해 `Enter`가 선택한 palette 명령에 정상 전달됩니다.

## 권한 모드

공개판 기본값은 **guarded**입니다.

```text
/permissions guarded
```

신뢰하는 로컬 저장소에서 더 넓은 자동화 권한을 의도적으로 허용하려면:

```text
/permissions trusted
```

trusted 모드에서도 force-push, 파괴적 reset/clean, 브랜치 삭제, 자동 merge/push 같은 작업은 제한됩니다.

## 브랜치 워크플로

```text
/branch
/branch list
/branch switch feature/foo
/branch new agent/fix-tests
/branch next feature/bar
/branch next-new agent/next-task
```

MAIN 에이전트는 제한된 `orch-branch` helper를 통해 안전한 브랜치를 생성하거나 전환할 수 있습니다. delegated worker에게는 authoritative branch를 자유롭게 변경하는 권한을 주지 않습니다.

## 큐 워크플로

MAIN 작업이 실행 중일 때 일반 프롬프트를 입력하면 프로젝트별 영속 FIFO 큐에 저장됩니다.

```text
/queue
/queue add 테스트 통과 후 구현을 다시 검토해줘
/queue pause
/queue resume
```

`smart` 알림 모드는 중간 큐 완료 알림을 줄이고 마지막 작업 완료 시 알립니다. `FAILED`, `BLOCKED`, `NEEDS_USER`, `NEEDS_GO`는 즉시 알립니다.

## 데스크톱 알림

```text
/notify smart
/notify all
/notify important
/notify off
/notify test
```

터미널 테스트:

```bash
orch-notify test --project demo
```

Linux에서는 데스크톱 알림에 `notify-send`가 필요합니다.

## 협업 helper

에이전트와 자동화를 위한 저수준 helper:

```bash
orch-delegate --model auto --task "이 변경을 검토해줘"
orch-consult --task "독립적인 두 번째 의견을 줘"
orch-parallel --count 2 --task "정확성 위험을 검토해줘"
orch-collect --batch latest
orch-handoff --model codex --task "이 작업을 이어서 진행해줘"
```

라우터는 독립 검증 여부를 판단할 때 vendor까지 구분합니다. 예를 들어 Anthropic 기반 Claude 경로 두 개는 cross-vendor 검증으로 계산하지 않습니다.

## 구조

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

## 워크플로 예시

`workflows/` 아래에는 다음 목적의 예시 계약이 포함되어 있습니다.

- 구현 + 검증
- 독립 코드 리뷰
- 조사 / 근거 교차 검증

현재 YAML은 참고용 예시/명세이며 v1에서 직접 실행되는 선언형 workflow engine은 아닙니다. 실제 delegation, parallel review, routing, mechanical check는 기존 CLI/TUI 동작을 통해 수행됩니다.

## 문제 해결

- **사용 가능한 provider가 없음:** 지원 CLI를 하나 이상 설치하고 인증한 뒤 `orch doctor`, `orch settings detect`, `orch settings show`를 실행합니다.
- **Codex 설치가 일부만 된 것 같음:** `orch doctor`는 broken symlink, npm package/bin 불일치, 오래된 `.codex-*` 디렉터리를 진단합니다. 자동 삭제나 자동 복구는 하지 않습니다.
- **Linux에서 `python3-venv`가 없다고 나옴:** 배포판의 Python venv 패키지를 설치한 뒤 다시 실행합니다. Debian/Ubuntu 계열은 보통 `python3-venv`입니다.
- **데스크톱 알림을 사용할 수 없음:** Linux는 libnotify의 `notify-send`와 데스크톱 알림 서비스가 필요합니다.
- **클립보드 연동이 안 됨:** Linux에서 `wl-copy`, `xclip`, `xsel` 중 하나를 설치합니다. macOS는 `pbcopy`를 사용합니다.
- **작업이 paused 상태로 남음:** `/status`, `/queue`, `/quota`를 확인하고 보고된 gate를 해결한 뒤 명시적으로 재개합니다.

## 제한 사항

- Windows는 v1.3.0에서 공식 지원하지 않습니다.
- provider 기능과 인증은 각 upstream CLI, 구독, 로컬 환경에 따라 달라집니다.
- quota 정보는 best-effort이며 사용할 수 없거나 오래된 값일 수 있습니다.
- workflow YAML은 참고용 예시이며 선언형 workflow 실행 엔진은 포함되지 않습니다.
- OrchBridge는 브랜치를 자동 merge하거나 push하지 않습니다.

## 개발

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt pytest
pytest -q
bash -n install.sh uninstall.sh scripts/*.sh
```

CI는 Ubuntu와 macOS에서 Python 3.11 / 3.13을 검사합니다. 유료 모델 API를 호출하지 않고 로컬 fixture와 mocked provider discovery를 사용합니다.

릴리스 asset 빌드:

```bash
bash scripts/build_release.sh
```

## 릴리스 및 버전 관리

저장소는 **Conventional Commits**와 Release Please를 사용합니다.

- `fix:` → patch 후보
- `feat:` → minor 후보
- `feat!:` / `BREAKING CHANGE:` → major 후보

`main`에 push되면 Release Please가 자동 release PR을 갱신합니다. release PR을 merge하면 Git tag/release가 만들어지고, release-assets workflow가 태그를 검증한 뒤 standalone `.sh`, source `.tar.gz`, 압축 해제 후 실행 가능한 `.zip`, checksum 파일을 생성해 Release에 업로드합니다.

최초 공개 태그는 `v1.0.0`입니다.

## 보안 및 개인정보

- OrchBridge 자체는 provider API 키를 요구하지 않습니다.
- provider 인증은 각 provider CLI가 관리합니다.
- 로그는 일반적인 token/password 필드 패턴을 가리지만, prompt, 파일명, commit, bug report에 비밀정보를 넣지 않는 것이 좋습니다.
- 공개 저장소에는 개인 장비 경로나 비공개 프로젝트 식별자를 넣지 않는 것을 원칙으로 하며 CI privacy guard가 이를 검사합니다.

자세한 내용은 [SECURITY.md](SECURITY.md)를 참고하세요.

## 라이선스

MIT. [LICENSE](LICENSE)를 참고하세요.

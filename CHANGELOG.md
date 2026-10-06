# Changelog

All notable changes to OrchBridge are documented here.

## [1.4.0](https://github.com/JongHyun070105/orch-bridge/compare/v1.3.1...v1.4.0) (2026-10-06)


### Features

* acquire checkout ownership before MAIN execution ([c081803](https://github.com/JongHyun070105/orch-bridge/commit/c081803736cb0097d3f82d50b5df67e1af1b874b))
* add compact and verbose UI modes ([2d67a9a](https://github.com/JongHyun070105/orch-bridge/commit/2d67a9a13428f662bb095ca7ef99b26d9ba9b5f1))
* add compact public TUI surfaces ([56c89bd](https://github.com/JongHyun070105/orch-bridge/commit/56c89bd8c69435b82277863d04c918ad0d2f0e42))
* add durable orchestration control plane ([fe87556](https://github.com/JongHyun070105/orch-bridge/commit/fe87556bb9b7c0e7c9002b1c0543faee5a966fc2))
* add OrchBridge v1.4 durable runtime ([#20](https://github.com/JongHyun070105/orch-bridge/issues/20)) ([d814952](https://github.com/JongHyun070105/orch-bridge/commit/d8149526fb28cc4d2ff70a0f124bb444057d1290))
* bind queued work to immutable project identity ([a71011c](https://github.com/JongHyun070105/orch-bridge/commit/a71011c09b071976cb8036d923de5f855d6e131e))
* declare v1.4 runtime component schemas ([9b46181](https://github.com/JongHyun070105/orch-bridge/commit/9b46181c17eb547ec98c1486b7ec4a4b2653b810))
* enforce atomic project binding before MAIN launch ([6448248](https://github.com/JongHyun070105/orch-bridge/commit/6448248cca13af015b72700b54e9d6807ff382b6))
* expose durable goal journal and runtime status ([89783a8](https://github.com/JongHyun070105/orch-bridge/commit/89783a8fc69feb3af4fbfca8c626586dc3397afa))
* migrate public AGY Claude defaults to 5.5 ([30a78e8](https://github.com/JongHyun070105/orch-bridge/commit/30a78e8597af7dbe7ba20efcf509853ca98a4273))
* persist compact UI density ([f0476e4](https://github.com/JongHyun070105/orch-bridge/commit/f0476e44e6e42dae2ad8b3d9f239b46b2cad379c))
* reconstruct and validate durable state on recovery ([8dd3391](https://github.com/JongHyun070105/orch-bridge/commit/8dd3391f7d34c7b4158069a61a2671a7c7b7d958))
* record delegate worktree ownership and fail closed ([c812bad](https://github.com/JongHyun070105/orch-bridge/commit/c812badd6575f9e153497d67ec2fabdfaf87b9c4))
* route AGY Claude 5.5 with adaptive effort ([4d8be4b](https://github.com/JongHyun070105/orch-bridge/commit/4d8be4b6ca2daec9e3b15bdc9b54315c8c714812))
* update AGY Claude to 5.5 with effort ([dc2322a](https://github.com/JongHyun070105/orch-bridge/commit/dc2322a25b094ecf9514b57171a0faf61887550e))
* update Phase 3 AGY Claude to 5.5 ([3f37034](https://github.com/JongHyun070105/orch-bridge/commit/3f3703482657c5d48b9953a40c9806dd86a5dd77))
* verify completion before releasing checkout ownership ([67d131e](https://github.com/JongHyun070105/orch-bridge/commit/67d131e42fed672ac3797a6409bb1008b1c42689))


### Bug Fixes

* accept durable MAIN lease metadata in worker ([0379839](https://github.com/JongHyun070105/orch-bridge/commit/0379839253680c4e6b5f8db397fbdec435fb1113))
* compile compact router verbose mode ([206da12](https://github.com/JongHyun070105/orch-bridge/commit/206da12d9c830ddc72f7239590ae78e611a8a7fa))
* compile verbose router panel paths ([967b8f3](https://github.com/JongHyun070105/orch-bridge/commit/967b8f3bc9d2317c9aadc0d3184abd5693453773))
* fail closed if MAIN lease handoff is lost ([a1c9d0c](https://github.com/JongHyun070105/orch-bridge/commit/a1c9d0ce5ade870ad4c9abb922cca4f2cb80e917))
* fail closed on durable ownership and journal ambiguity ([8e887d4](https://github.com/JongHyun070105/orch-bridge/commit/8e887d4f4f9f2d53d80e1006c2ca24b4bc6c6003))
* finalize compact route labels and remove stale footer css ([c5e3db2](https://github.com/JongHyun070105/orch-bridge/commit/c5e3db2b7079fc1b16f83fbfd8877728be1bcce8))
* gate every worktree cleanup on proven ownership ([afeb8e6](https://github.com/JongHyun070105/orch-bridge/commit/afeb8e63127f6858ed23d64a009fb7a11a3b8556))
* harden checkout leases and journal recovery ([cb1e183](https://github.com/JongHyun070105/orch-bridge/commit/cb1e183678205f823cb67406586f9aa042123df0))
* harden non-git identity lease recovery and journal anchors ([77c53c1](https://github.com/JongHyun070105/orch-bridge/commit/77c53c1d3da33018fdfc02ed2984cd8a45c11a37))
* migrate legacy AGY Claude 4.6 defaults ([882a8ca](https://github.com/JongHyun070105/orch-bridge/commit/882a8ca6b99a479c4d93d190867ca099fcb669f3))
* migrate project queues and compact legacy route scores ([b5e9278](https://github.com/JongHyun070105/orch-bridge/commit/b5e9278ef9d522cddb186f739f5758512eb0ba90))
* preserve legacy runtime tests and compact route rendering ([abeef28](https://github.com/JongHyun070105/orch-bridge/commit/abeef28296ef98f9ff5b83cf2ffd027bb9a038bb))
* prevent worker handoff into quarantined checkout ([dbd2b8a](https://github.com/JongHyun070105/orch-bridge/commit/dbd2b8a52c9dbd3ce015bcb6b8bad7d7798d4a2a))
* quarantine ambiguous recovery and restore verbose runtime detail ([f8de94c](https://github.com/JongHyun070105/orch-bridge/commit/f8de94c8b3c4c8e563bd2cd7f8da95075c39d266))
* reacquire stale checkout ownership on resume ([c52afe1](https://github.com/JongHyun070105/orch-bridge/commit/c52afe1ff549490d8ba4bace0162111dd8687db3))
* roll back unregistered worktree creation ([fd3d3f8](https://github.com/JongHyun070105/orch-bridge/commit/fd3d3f818d7d095f914b05ebe55cc660f24795ce))
* use public runtime heartbeat API in MAIN worker ([cd73117](https://github.com/JongHyun070105/orch-bridge/commit/cd7311708b2e8b7b4f52bbfa49cc11e1393e7f94))
* validate persisted checkout lease ownership on resume ([827182c](https://github.com/JongHyun070105/orch-bridge/commit/827182cf1f99a09062ac522b3ae29363fcc87916))

## [1.3.1](https://github.com/JongHyun070105/orch-bridge/compare/v1.3.0...v1.3.1) (2026-10-04)


### Bug Fixes

* address v1.3 review feedback and compact router UI ([6c10ff3](https://github.com/JongHyun070105/orch-bridge/commit/6c10ff3416c78a1baa73be380c433c5d5050850d))
* address v1.3 review feedback and compact router UI ([a5f1ca1](https://github.com/JongHyun070105/orch-bridge/commit/a5f1ca1a46e4997a4933823fd25c0bb60729b3fd))
* defer commandcode resolution until quota probe is due ([06a6a20](https://github.com/JongHyun070105/orch-bridge/commit/06a6a20cef1ab5b87df4ccc9956a7e3ffea69ffd))
* space compact router scores for readability ([8a17953](https://github.com/JongHyun070105/orch-bridge/commit/8a17953ea6d2d5646b9c30ba4d053c8c98bbe7a6))

## [1.3.0](https://github.com/JongHyun070105/orch-bridge/compare/v1.2.0...v1.3.0) (2026-10-04)


### Features

* release OrchBridge v1.3 orchestrator upgrade ([2f37d26](https://github.com/JongHyun070105/orch-bridge/commit/2f37d2610b84aed1c41c2efdbcbbca22ee0f907c))

## [1.2.0](https://github.com/JongHyun070105/orch-bridge/compare/v1.1.0...v1.2.0) (2026-09-30)


### Features

* add terminal-safe input and project registry UX ([bc2fb25](https://github.com/JongHyun070105/orch-bridge/commit/bc2fb25dcffdd60a191eb8bab87d1514f64aa836))

## [1.1.0](https://github.com/JongHyun070105/orch-bridge/compare/v1.0.0...v1.1.0) (2026-09-29)


### Features

* add persistent steering and advanced provider diagnostics ([d3ca286](https://github.com/JongHyun070105/orch-bridge/commit/d3ca286241cf28f5c021d987f44341d59ddc09cf))

## 1.0.0

- First public open-source release.
- macOS and Linux support.
- Multi-provider adaptive routing and delegation.
- Persistent queue and project-scoped state.
- Safe branch workflow.
- Desktop completion notifications.
- Portable `orch settings` provider/model configuration.
- Fixed `Ctrl+P` command palette selection when composer priority bindings are enabled.
- Removed machine-specific and private-project identifiers from the public tree.

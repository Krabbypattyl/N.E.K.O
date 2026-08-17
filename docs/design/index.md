# Design and Implementation Records

These documents preserve design intent and implementation context. They are grouped by maintenance purpose, not by delivery date. Most records are written in the language used by the original implementation work.

> The current code and tests are authoritative. Read [Documentation Maintenance](/contributing/documentation) before treating a proposal or dated record as a current contract.

## Architecture and long-lived contracts

- [Avatar performance module maintenance](./avatar-performance-module-maintenance)
- [Avatar tool interaction design and maintenance](./avatar-tool-interaction-design-and-maintenance)
- [Avatar tool prompt guidelines](./avatar-tool-prompt-guidelines)
- [Cat Mind state-machine rules](./cat-idle-state-machine-rules)
- [Cat idle states](./cat-idle-states-feature)
- [Deep topic hooks](./deep-topic-hooks)
- [LLM prompt budget](./llm-prompt-budget)
- [Proactive reason-code guide](./proactive-reason-code-guide.zh-CN)
- [User activity tracker](./user-activity-tracker)
- [Voice design architecture](./voice-design-architecture)

## Implemented design records

- [ASR client phase record](./asr-client-phase1)
- [Compact chat mode](./compact-chat-mode-design)
- [Memory event journal](./memory-event-log-rfc)
- [User-driven memory evidence](./memory-evidence-rfc)
- [PNGTuber lightweight avatar](./pngtuber-lightweight-avatar-plan)
- [Translation subtitle panel](./translation-subtitle-panel-design)
- [TTS provider and voice-source unification](./tts-voice-source-unification)
- [Live2D idle motion selection and recovery](/live2d_motion_plan)
- [PNGTubeRemix layered physics compatibility](/pngtuber-remix-physics-plan)

## N.E.K.O 小剧场当前记录

N.E.K.O 仓库只维护以下两份小剧场开发文档。代码和测试高于文档；架构合同与真实表现不一致时，先把证据和解决方向记录到实测问题文档。

- [小剧场架构开发文档](./neko-theater-architecture)
- [小剧场实测问题描述以及解决方案](./neko-theater-issues-and-solutions)

作者侧 Numeric v2 生成器的专项 DTO 与页面设计由 `NEKO_Numeric_drama` 仓库维护，不能覆盖 N.E.K.O Runtime 的权限和持久化合同。

## Product-flow and interaction records

- [Seven-day floating avatar guide](./avatar-floating-7day-complete-guide-dev)
- [Post-tutorial low-disruption chat branches](./avatar-floating-post-theater-chat-branches)
- [CAT1 Playground Drop](./cat1-playground-drop-design)
- [Focus / True-Name mode](./focus-truename-mode)
- [Memory-browser particle dissolve](./memory-browser-particle-dissolve)
- [Yui guide-system cursor hiding](./yui-guide-system-cursor-hiding)

## Security, persistence, and incident analysis

- [Local mutation endpoint authentication](./security/local-mutation-auth)
- [Steam Auto-Cloud synchronization](./cloud-save-sync-optimization-plan)
- [Telemetry distribution and Steam user ID race](./telemetry-distribution-race-impact)

New records should state whether they are a current contract, implemented record, proposal, historical snapshot, or deprecated document near the beginning.

# N.E.K.O 小剧场统一开发方向

状态：当前实施依据（Numeric v2）

## 产品边界

小剧场保留两种完全隔离的模式：

- 剧本模式 `/theater-numeric`：运行 `neko.story.numeric.v2` 作者包；
- 自由模式 `/theater`：运行 RP-Hub 风格角色卡聊天沙盒；
- 统一入口 `/theater-home`：只负责模式介绍和跳转，不在同一 Session 内切换模式；
- 两种模式只共享当前猫娘配置与底层 TTS 播放能力。

自由模式不得读取或写入 Numeric v2 的节点、数值、路线、Ledger、结局和恢复指针。剧本模式不得把自由聊天内容写成正式剧情事实。

## Numeric v2 目标

作者决定宏观剧情：背景、双角色身份、幕节点、支线、结局、隐藏数值规则、路线条件与过渡合同。玩家使用自然语言决定当下行动。模型只做两件事：

1. 数值判定器依据作者增减规则提出本回合 delta 和证据；
2. Actor 根据已经确定性结算的结果生成旁白、猫娘对白和非正式推荐输入。

确定性 Runtime 负责限幅、clamp、`min_turns`、路线优先级、节点推进、结局、revision、幂等和原子提交。模型不能创建正式数值、节点、路线、事实或结局，也不能直接写 Session 和 Ledger。

## 当前合同

Story Package schema 为 `neko.story.numeric.v2`，核心结构包括：

- `meta`
- `intro`
- `characters`
- `catgirl_binding`
- `metric_schema`
- `initial_state`
- `start_node_id`
- `nodes`
- `endings`

所有 metric 对玩家隐藏。每个非结局节点必须声明 `min_turns`，范围为 1—20，InkAI 新建和生成时默认 2。达到最少回合前保持当前节点；达到后才按作者 route gate 和 priority 确定性选择唯一出口。条件尚未满足时继续留在当前节点演绎。

只有一个出口的主线节点允许空条件顺序推进；同一节点有多个出口时，每条路线都必须包含数值条件。每个可达非结局节点最终必须能到达 terminal ending。

## 回合提交顺序

```text
玩家原话
  → Session / 当前猫娘 / revision / 幂等校验
  → 一次数值判定
  → 候选状态应用 delta、min_turns 和 route gate
  → 一次 Actor 演绎
  → 重验 revision 后原子提交 Session + Ledger + 表现记录
  → 已提交猫娘对白进入共享 TTS 桥
```

Evaluator、Actor 或 Store 任一步失败都不提交半回合。TTS 失败只降级为文字，不反向撤销已经提交的正式回合。

## 前端规则

- 舞台只展示背景介绍、玩家身份和猫娘身份，不显示节点标题或场景卡；
- 演绎区按顺序显示开场、玩家原话、旁白和猫娘对白；玩家输入必须单独一行；
- 猫娘对白展示为 `「对白」`，不显示“猫娘：”前缀；服务端和 TTS 保存、播放原始文本；
- Actor 推荐输入只是可选自然语言，点击后仍走普通自由输入链路，不绑定路线；
- 前端不显示 metric 原始值、metric ID、route gate、阈值或目标节点；
- 结束后保留服务端 Session、Ledger 和表现历史，前端清除活动指针；重新开始创建新 Session。

## InkAI 边界

InkAI 只生成、编辑、校验、编译和安装 Numeric v2 Story Package。首次生成只产出主线和一个 Normal 结局，不要求模型输出 metric 或路线阈值。数值、支线和额外结局由作者在故事地图中配置；节点“AI 完善”只回填该节点的演绎约束，不修改节点类型、路线和阈值。

`min_turns` 是作者字段，但默认值由系统投影，不增加主线模型的一次输出负担。

## 持续约束

- 不新增 Planner、Director、多轮 Repair 或重复模型调用层；
- 不用旧协议、旧包或旧测试恢复已删除链路；
- 不做隐式版本迁移；
- 删除代码前确认真实消费者；
- 保留无关用户修改，不做宽范围回退；
- 所有新代码添加清晰中文注释；
- 修改后按范围验证合同、Runtime、Router、前端、八语言包和真实回放。

详细合同和测试矩阵见 [Numeric v2 运行时开发文档](./neko-theater-numeric-v2-runtime-development.md)。

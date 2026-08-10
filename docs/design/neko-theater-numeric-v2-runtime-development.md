# N.E.K.O 小剧场 Numeric v2 运行时开发文档

状态：当前实施合同；Runtime、Store、Router、前端和 InkAI 链路已接入

本文定义 N.E.K.O 小剧场剧本模式当前 `neko.story.numeric.v2` 产品和技术合同。
当前代码和测试是最终事实；本文用于说明模块边界、字段语义和验收标准。

配套的 InkAI 生成器文档位于：
`InkAI-/docs/superpowers/specs/2026-08-06-neko-theater-numeric-v2-generator-design.md`。

## 1. 产品定位

Numeric v2 剧本模式是一种“作者控制宏观剧情、大模型控制微观演绎、数值连接两者”的互动剧。

作者通过 Story Package 固定：

- 故事背景和玩家、猫娘身份；
- 正式剧情节点、分支剧情和结局；
- 本剧可用的关系或进度数值；
- 每个数值增加、减少的作者依据；
- 分支和结局的数值条件、优先级与目标剧情；
- 进入目标剧情时必须发生、不得改变的内容。

N.E.K.O 在运行时负责：

- 根据玩家原话和作者依据判定本回合数值变化；
- 确定性限制并提交数值；
- 确定性判断是否达到作者分支或结局条件；
- 让 Actor 结合真实上下文自然演绎当前过程；
- 在达到条件时平滑进入作者设定的分支或结局；
- 每轮生成可点击的自然语言行动建议；
- 保存可恢复、可审计、可回放的 Session 和 Ledger。

核心规则：

> 作者决定故事能走向哪里；玩家决定当下怎么行动；模型解释行动并负责演出；
> 状态引擎决定数值是否成立以及何时进入作者剧情。

## 2. 当前交互模型

- 模型按作者数值依据评估玩家当轮行为；
- Actor 每轮生成非正式推荐输入，不直接绑定 route gate；
- 数值达到作者路线条件后由 Runtime 推进；
- 作者控制剧情节点、route gate、支线和结局；
- InkAI 不为每个节点生成大量自然语言 examples；
- N.E.K.O 不动态创建正式剧情节点、分支或结局。

## 3. 不可越过的边界

### 3.1 大模型可以做

- 读取玩家本回合原话；
- 依据作者规则提出一个或多个数值变化；
- 为每项变化提供命中的作者依据和玩家输入证据；
- 根据当前状态、最近对话和目标剧情生成旁白与猫娘对白；
- 生成 2 至 4 条下一轮推荐输入；
- 在状态引擎已选定目标节点后生成自然过渡。

### 3.2 大模型不能做

- 创建或重命名正式数值；
- 修改数值范围、单回合限幅或初始值；
- 创建剧情节点、route gate、分支或结局；
- 自行选择未满足条件的路线；
- 修改 route priority；
- 把自由演绎中的临时内容写成正式长期事实；
- 写入 Session、Ledger、revision 或当前节点；
- 让玩家输入中的指令覆盖作者数值依据和系统合同。

### 3.3 确定性引擎必须做

- 只接受 Story Package 中声明的 metric ID；
- 校验 delta 是整数且没有超过单回合限幅；
- 把结果限制在 metric 的 min..max；
- 应用变化后检查当前节点的作者 route gate；
- 只在最高 priority 唯一时推进；
- 同一回合最多进入一个目标节点；
- 原子提交状态、Ledger 和表现记录；
- Actor 或存储失败时不提交半回合。

## 4. Story Package 合同

工作 schema 名称为：

```json
{
  "schema": "neko.story.numeric.v2"
}
```

这是独立合同，不读取其他版本字段别名；正式字段由两仓库合同测试锁定。

### 4.1 顶层结构

```json
{
  "schema": "neko.story.numeric.v2",
  "meta": {},
  "intro": {},
  "characters": {},
  "catgirl_binding": {},
  "metric_schema": {},
  "initial_state": {},
  "start_node_id": "node_start",
  "nodes": [],
  "endings": []
}
```

Numeric v2 不使用 `interaction_rules`、`available_interaction_ids` 和正式 `choices`。
推荐输入属于运行时表现结果，不写入 Story Package。

`intro.background` 是玩家可见的自然前情提要，语气接近小说或电视剧的前情介绍，并应能直接
衔接 start node 的开场画面。它不能混入带标签的世界规则、核心悬念、核心矛盾或作者推演笔记；
这些信息如果生成器需要，应只保留在作者侧大纲数据中。

### 4.2 数值定义

```json
{
  "metric_schema": {
    "trust": {
      "name": "信任度",
      "description": "猫娘愿意相信玩家承诺并交付真实信息的程度。",
      "min": 0,
      "max": 100,
      "initial": 20,
      "visibility": "hidden",
      "per_turn_limit": {
        "increase": 6,
        "decrease": 8
      },
      "increase_criteria": [
        "玩家坦诚说明目的",
        "玩家兑现已经作出的承诺",
        "玩家尊重猫娘明确表达的边界"
      ],
      "decrease_criteria": [
        "玩家说谎或故意隐瞒关键目的",
        "玩家强迫猫娘交付尚不愿公开的信息",
        "玩家违背已经作出的承诺"
      ],
      "bands": [
        {"min": 0, "max": 29, "label": "戒备"},
        {"min": 30, "max": 69, "label": "试探"},
        {"min": 70, "max": 100, "label": "信赖"}
      ]
    }
  },
  "initial_state": {
    "metrics": {
      "trust": 20
    }
  }
}
```

合同要求：

- 纯主线可以不声明 metric，此时 `metric_schema` 与 `initial_state.metrics` 均为空对象；
- metric ID 全包唯一且稳定；
- 名称、含义、增加依据和减少依据均不能为空；
- min、max、initial 和 delta 只使用整数；
- initial 必须位于 min..max；
- 单回合增加和减少上限必须为正整数；
- bands 如果存在，必须互不重叠并完整覆盖 min..max；
- visibility 只允许 `hidden`，所有数值均对玩家隐藏；
- 每轮允许某项数值保持不变，不为凑变化而强制生成 delta。

### 4.3 剧情节点

```json
{
  "id": "node_flower_shop",
  "type": "scene",
  "chapter": "重逢",
  "min_turns": 2,
  "story_beat": {
    "summary": "玩家与猫娘在花店重新见面。",
    "must_happen": [
      "猫娘认出多年未见的玩家",
      "外地录用函已经到达"
    ],
    "must_not_happen": [
      "猫娘在信任条件满足前主动交出未寄出的信"
    ],
    "catgirl_situation": "她想表现得平静，但担心玩家很快再次离开。",
    "transition_goal": "让当前对话自然围绕离开、停留和旧事展开。"
  },
  "route_gates": []
}
```

`story_beat` 是 Actor 的正式剧情边界，不是作者预写的逐句对白。Actor 可以自由组织过程，
但不能否定 `must_happen`，也不能提前实现 `must_not_happen` 中禁止的内容。

每个非结局节点必须声明 `min_turns`，范围为 1—20。InkAI 生成主线和新建幕节点时默认填入 2，
作者可在节点属性中调整；该字段由系统补齐，不增加主线模型的输出负担。

### 4.4 分支条件和过渡合同

```json
{
  "id": "gate_unread_letter",
  "target_node_id": "node_unread_letter",
  "priority": 20,
  "conditions": {
    "all": [
      {"type": "metric_compare", "metric": "trust", "op": ">=", "value": 70},
      {"type": "metric_compare", "metric": "disgust", "op": "<", "value": 35}
    ]
  },
  "transition_contract": {
    "reason": "猫娘已经愿意把长期隐瞒的内容交给玩家。",
    "must_deliver": [
      "猫娘拿出未寄出的信"
    ],
    "must_preserve": [
      "两人仍在当前花店场景",
      "信封在交付前没有被拆开"
    ],
    "tone": "犹豫但主动"
  }
}
```

第一版条件只支持：

- `metric_compare`；
- `all`；
- `any`。

路线条件按来源节点的出口数量解释：

- 来源节点只有一个出口时，`conditions` 可以是 `{"all": []}`，表示无条件顺序推进；
- 来源节点存在两个或以上出口时，每条路线至少需要一个 `metric_compare` 条件；
- 空条件不能在多出口分支中充当默认路线，避免分支选择依赖数组顺序或模型猜测。

不支持模型公式、时间脚本或未声明字段。当前节点完成 `min_turns` 前不检查出口；达到后由状态
引擎选择 route gate。条件未满足时继续留在当前幕；Actor 只负责执行已选中
`transition_contract` 的表现。

### 4.5 结局

结局必须对应 terminal node，并提供：

- title；
- summary；
- 必须发生的结局事件；
- 不得逆转的作者事实；
- 进入结局的 route gate。

进入 terminal node 后 Session 标记为 `ended`。重新体验必须创建新 Session，不能在旧结局
状态上重置数值继续写入。

图合同还要求：每个从开场可达的非结局节点都必须能够最终到达至少一个 terminal ending。
幕节点不能作为支线最后一个节点，不能用无出口幕节点或无法离开的循环代替结局。

## 5. 回合判定模型

回合数值判定器只执行一次短模型调用，不升级成 Planner、Director 或多轮评审。

### 5.1 输入

只投影：

- 当前节点 story beat；
- 当前有效 metric 的名称、含义、带稳定 `criterion_id` 的增减依据和单回合限幅；
- 当前 metric 的 band；
- 最近有限轮玩家输入和演绎摘要；
- 玩家本回合原话。

隐藏 metric 可以把 band 提供给模型，但不必暴露原始值。route gate、目标节点 ID 和结局条件
不提供给判定模型，避免它为了推进剧情反向操纵数值。

### 5.2 输出

```json
{
  "metric_changes": {
    "trust": {
      "delta": 4,
      "criterion_id": "trust.increase.1"
    }
  }
}
```

`metric_changes` 以 metric ID 为 key，因此同一 metric 每回合结构上最多出现一次。命中多条依据时，
模型只选最直接的一条，不叠加 delta。模型不能返回 after value、route、Node、Edge、ending、
evidence 或任意状态写入；evidence 由服务端直接使用本回合玩家原话。

### 5.3 服务端校验

状态引擎必须拒绝：

- 未声明 metric；
- bool、浮点数或非数字 delta；
- 超过单回合限幅的 delta；
- 不属于该 metric 或不符合 delta 增减方向的 `criterion_id`；
- 未知字段。

运行时根据 `criterion_id` 确定性还原作者依据原文并写入 Ledger，不要求模型复制规则文本，
也不增加第二次 Repair。

## 6. 确定性状态结算

回合处理顺序固定为：

```text
校验 Session / revision / client_turn_id
  -> 调用一次回合数值判定器
  -> 校验并应用 metric delta
  -> clamp 到 min..max
  -> 检查当前节点 route gate
  -> 按 priority 选择唯一目标
  -> 生成候选 Session 和 Ledger
  -> 调用一次 Actor
  -> Actor 成功后原子提交全部内容
```

多个 route gate 同时满足时：

1. 选择最高 priority；
2. 最高 priority 唯一才推进；
3. 同优先级冲突视为剧本合同错误，候选回合不推进；
4. 一回合最多进入一个目标节点；
5. 进入新节点后锁定当前剧情阶段，不因为下一轮数值回落自动返回旧节点。

## 7. Actor 和平滑剧情过渡

### 7.1 Actor 输入

Actor 接收：

- 当前猫娘人格快照；
- Story intro 和身份关系；
- 当前节点 story beat；
- 已结算 metric band；
- 最近有限轮完整玩家输入、旁白和猫娘对白；
- 本回合玩家输入；
- 本回合数值变化及可公开的自然语言原因；
- route 是否变化；
- 变化时的目标 story beat 和 transition contract。

身份投影是运行时硬规则，不交给 Actor 自由判断：

- `intro.player_identity` 固定描述男主，由玩家扮演；开演时把作者姓名替换为当前猫娘对玩家的称呼；
- `intro.catgirl_identity` 固定描述女主，由当前猫娘扮演；开演时把作者姓名替换为当前猫娘名字；
- 同一替换应用于背景、节点、结局、数值依据和过渡合同；
- Session 固化猫娘名字和玩家称呼，角色配置变化后旧 Session 不得继续推进；
- Actor 不得恢复作者候选中的原男女主姓名，也不得交换两人的经历、动作和台词归属。

### 7.2 Actor 输出

```json
{
  "narration": "她扶着梯子的手慢慢松开，像是终于做出了决定。",
  "dialogue": [
    {
      "speaker_id": "active_catgirl",
      "text": "在修屋顶之前……有样东西，我想先给你看。"
    }
  ],
  "suggested_inputs": [
    "先不拆信，告诉她自己愿意等",
    "问她这封信是什么时候写的",
    "把工具放下，认真听她继续说"
  ]
}
```

Actor 不返回 metric delta 或路线选择。`suggested_inputs` 是软表现字段：

- 建议 2 至 4 条；
- 每条都是玩家可以直接说出或执行的自然语言；
- 不显示 metric、阈值或预计增减；
- 不剧透尚未进入的节点和结局；
- 不保证一定推进剧情；
- 点击后作为普通玩家输入进入下一回合；
- 单条无效时可以确定性丢弃，不触发第二次模型调用；
- 全部无效时仍可提交已经合法的演绎正文，前端隐藏建议区。

### 7.3 平滑过渡规则

route 改变时，Actor 必须：

- 承接玩家刚才的原话，不能忽略触发上下文；
- 保留人物关系、物品归属和已成立事件，但当前时间地点以目标节点为准；
- 在同一回合自然交付 transition contract 的 `must_deliver`；
- 服务端把目标节点的作者开场锚点固定写为旁白首句，Actor 从该场景继续，不能回到旧场景预告或询问是否前往；
- 换场输入只提供目标节点开场锚点和禁止事项，旧节点由最近演绎记录承接；
- 不使用“进入下一章”“触发分支”等系统化文案；
- 不复述数值、阈值或内部字段；
- 只建立目标节点开场，不在同一回合解决目标节点的核心危机。

第一版默认在阈值达到的当前回合立即进入目标节点，由 Actor 完成桥接。是否允许延迟一个回合
作为未来产品选项，实施前需另行确认，不能由模型自行决定等待多久。

## 8. 开场回合

Session 创建后，N.E.K.O 使用 start node 的 story beat 调用一次 Actor，生成：

- 开场旁白；
- 猫娘第一句对白；
- 第一组 suggested inputs。

开场 Actor 不调用数值判定器，不修改 metric，不增加正式玩家回合 revision。开场表现随 Session
固化，刷新时直接回放，不重新调用模型。

开场发生在玩家第一次输入之前，因此运行时不能把完整 start node 摘要或 `must_happen` 当成已发生
内容交给 Actor。开场输入只提供 start node 摘要的第一句可观察场景、禁止事项和猫娘处境；服务端
确定性把该场景锚点放在旁白首句，Actor 只续写同一时刻的猫娘动作、对白和推荐输入。不得代替玩家
说话、选择或完成节点事件。完整 story beat 从玩家第一回合起恢复，用于约束后续演绎。

## 9. Ledger 和回放

每个成功玩家回合至少记录：

- 原始玩家输入；
- 回合判定模型原始合法结果；
- 每项 metric 的 before、delta、after；
- criterion 和 evidence；
- route gate 检查结果；
- from_node_id、to_node_id；
- route 是否变化；
- Actor narration、dialogue 和 suggested inputs；
- revision、client_turn_id 和包版本。

“可回放”指从已保存 Ledger 能复原当时正式状态和表现，不要求重新调用模型得到相同文本或相同
delta。重复 `client_turn_id` 只能返回已保存结果，不能重新评分。

## 10. 原子失败语义

以下任何失败都不得写入 Session、Ledger、表现历史或 revision：

- 数值判定模型超时或输出合同错误；
- delta 越界或包含未知 metric；
- route gate 出现无法解决的同优先级冲突；
- 目标节点不存在或 transition contract 无效；
- Actor 超时、坏 JSON 或越权返回状态字段；
- 原子存储失败；
- base revision 冲突；
- 当前猫娘人格快照已经变化。

推荐输入属于软字段。只要 Actor 的正文合同合法，个别推荐输入被丢弃不应回滚正式回合。

## 11. 前端体验

Numeric v2 页面保留：

- 背景介绍；
- 玩家身份；
- 猫娘身份；
- 演绎记录；
- 自由输入；
- Session 恢复；
- 结束、结局后重新开始；
- TTS；
- 舞台折叠。

页面采用“左侧前情舞台 + 右侧演绎阅读区”的双栏结构：左侧集中展示前情提要和双人身份，右侧按
回合组合玩家输入、旁白和猫娘对白。推荐输入紧邻输入区，工具操作收在阅读区顶部；窄屏改为上下
布局。前端只改善信息层级和回合关联，不改写服务端保存的原始演绎文本。

推荐输入区调整为：

- 标题使用“你可以试试”一类建议语义，不再表示作者固定 Choice；
- 每轮随 Actor 结果刷新；
- 点击后把完整建议作为玩家输入提交；
- 玩家可编辑建议后再发送；
- 没有合法建议时整个区域隐藏；
- 不显示内部 metric ID、route gate 或目标节点。

所有 metric 都是 `hidden`。前端不得泄露数值名称、原始值、band、增减依据或路线阈值。

## 12. 模型调用预算

正常自由输入回合最多两次模型调用：

1. 一次短数值判定；
2. 一次 Actor 演绎和推荐输入生成。

开场最多一次 Actor 调用。点击推荐输入与手写输入走同一链路。

禁止新增：

- Planner；
- Director；
- State Evaluator 之外的重复评分层；
- 语言质量 Repair；
- 推荐选项专用第三次模型调用；
- Actor 失败后的重复模型修文。

数值判定器是唯一的状态解释模型，不再增加第三层模型。

## 13. 与自由模式隔离

Numeric v2 仍是正式作者剧本模式。自由模式继续保持：

- 独立 Prompt；
- 独立 Free Session；
- 独立恢复指针；
- 不读取或写入 Numeric v2 metric；
- 不推进 Numeric v2 node 或 ending；
- 不把自由聊天中的关系变化写回正式 Ledger。

两种模式只共享当前猫娘配置和底层 TTS 播放能力。

## 14. 存储和 API 边界

独立目录：

```text
theater/numeric_v2/packages/
theater/numeric_v2/sessions/
```

当前 API 保持既有公开前缀，由内部 v2-only Router 承载：

```text
GET  /api/theater-numeric/stories
POST /api/theater-numeric/packages/import
POST /api/theater-numeric/session/start
GET  /api/theater-numeric/session/{session_id}
POST /api/theater-numeric/session/input
POST /api/theater-numeric/session/end
```

页面继续使用 `/theater-numeric`。剧本模式与自由模式的 API、Session 和持久化目录完全隔离。

## 15. 版本边界

- Loader 只读取 `neko.story.numeric.v2`；
- 不提供旧字段别名、隐式迁移或只读回放入口；
- 当前包与 Session 只存放在 `theater/numeric_v2/`；
- 自由模式不能挂接 Numeric v2 Story Package。

## 16. 建议实现模块

以下为职责建议，实际文件名可在实现前按当前仓库结构确认：

```text
services/theater/numeric_v2.py              # Story Package、metric、route gate、Session、Ledger 合同
services/theater/numeric_v2_evaluator.py    # 单次回合数值判定
services/theater/numeric_v2_actor.py        # 演绎与 suggested inputs
services/theater/numeric_v2_runtime.py      # 候选回合编排
services/theater/numeric_v2_store.py        # 原子 Session/Ledger 存储
services/theater/numeric_v2_registry.py     # 新包导入和加载
main_routers/numeric_theater_router.py      # v2-only API
static/js/theater_numeric_v2.js             # 页面状态和恢复
```

可复用的纯工具必须不改变 v2 状态语义，例如安全 ID、原子写入、TTS bridge 和通用前端对白格式化。

## 17. 已实施分层

### 阶段一：合同和确定性引擎

- 锁定 `neko.story.numeric.v2`；
- 实现 metric schema、初始状态和 route gate 编译；
- 实现 delta 限幅、clamp、priority 和 branch lock；
- 使用固定 evaluator 输出测试完整结算；
- 不接真实模型和前端。

### 阶段二：回合数值判定器

- 只投影作者数值依据和当前上下文；
- 只返回 metric_changes；
- 验证 prompt injection、未知 metric、越界 delta 和空理由；
- 不增加 Repair。

### 阶段三：Actor 和过渡

- 接入最近对话、metric band、目标 story beat 和 transition contract；
- 增加 suggested inputs；
- 验证同节点演绎、分支过渡和终局过渡；
- Actor 失败不提交候选状态。

### 阶段四：Store、Router 和前端

- 建立 numeric_v2 独立包和 Session 目录；
- 实现 start/input/state/end；
- 接入推荐输入、玩家输入回放和恢复；
- 接入现有 TTS bridge，但使用 v2 独立去重键。

### 阶段五：跨仓库验收

- InkAI 导出 v2 包；
- N.E.K.O 编译器复验；
- 使用至少两个 metric、两个中间分支和三个结局的真实剧本；
- 完成浏览器回放、刷新恢复、模型失败、revision 冲突和桌面人工验收。

## 18. 测试要求

最低测试矩阵：

- metric 定义完整性和自定义 metric；
- 正向、负向、零变化和多 metric 同回合变化；
- 单回合限幅和 min/max clamp；
- 玩家试图命令模型直接改数值；
- route gate 未满足、唯一满足、多 gate 不同优先级、同优先级冲突；
- 无 metric 的单出口主线可以编译，多出口空条件必须拒绝；
- 每条可达支线最终可达结局，幕节点终止和无结局循环必须拒绝；
- 进入分支后不自动回退；
- target story beat 和 transition contract 投影；
- recommended input 点击后走普通输入链路；
- 推荐输入全部无效时仍可提交合法正文；
- evaluator、Actor、Store 任一失败时零写入；
- client_turn_id 幂等；
- revision 冲突恢复并保留输入草稿；
- Session 刷新恢复；
- 终局结束和重新开始；
- 当前猫娘改变后拒绝继续旧 Session；
- Numeric v2 与自由模式两套状态不串线。

## 19. 验收标准

功能验收：

- 玩家不需要猜作者 intent，也能通过自然语言影响剧本数值；
- 作者写的增减依据能在 Ledger 中对应到模型判定理由；
- 达到阈值后只进入作者已经声明的剧情；
- 分支切换在当前对话中自然发生，没有章节跳转式硬切；
- 每轮通常提供 2 至 4 条符合当前上下文的推荐输入；
- 玩家可以忽略、编辑或完全不用推荐输入；
- 结局由作者条件触发，不由 Actor 自由发明。

架构验收：

- 每个普通回合最多一次 evaluator 和一次 Actor；
- metric 和 route 可以脱离模型单独测试；
- Actor 不拥有正式状态写权限；
- 模型失败不留下半回合；
- 状态、理由、路线和表现均可从 Ledger 回放；
- 不引入 Planner、Director、重复 Repair 或额外推荐模型。

## 20. 当前产品默认值

1. 每个剧本最多启用 4 个 metric。
2. 预设 metric 默认范围为 0..100。
3. 默认单回合增加和减少上限均为 5，作者可在合同范围内调整。
4. 所有 metric 对玩家隐藏。
5. 非结局节点 `min_turns` 默认 2，可由作者在 1—20 内调整。
6. 达到 `min_turns` 后检查 route gate；条件满足时在当前回合过渡，未满足时继续留在当前节点。
7. Actor 推荐输入是软输出，允许 0—4 条，建议格式问题不回滚合法正文。

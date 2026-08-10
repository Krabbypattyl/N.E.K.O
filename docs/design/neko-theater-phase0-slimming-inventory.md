# N.E.K.O 小剧场阶段 0：代码与 Prompt 瘦身盘点

## 1. 文档目的

本文是 [统一开发方向](./neko-theater-development-direction.md) 的阶段 0 盘点结果。

阶段 0 只做以下事情：

- 记录两个仓库当前工作区的实际模块和调用入口；
- 盘点剧本模式、自由模式和 InkAI 生成器的 Prompt、Repair、Guard 和模型调用；
- 区分安全合同复杂度与语言质量治理复杂度；
- 列出可以删除、合并、降级为观测指标或暂时保留的候选项；
- 为后续代码改动建立最小回归范围。

初始盘点阶段不做以下事情；获得真实回放证据后，可以按本文的最小范围实施收敛：

- 不删除代码；
- 不批量回退当前工作区；
- 不因为文件名看起来过时就处理模块；
- 不把历史 Round 的结果直接当作删除依据；
- 不因为某一次模型输出不好就增加新的规则或 Repair。

## 2. 盘点边界与工作区状态

本次盘点基于当前工作区，而不是基于某个干净的 Git 提交。两个仓库都存在大量未提交修改、删除和未跟踪文件，因此以下结论分为：

- **已确认调用链**：由当前代码入口和实际引用得到；
- **删除候选**：需要在干净基线或明确的阶段提交中再次核对；
- **待验证假设**：需要测试、模型回放或人工试玩后才能决定。

当前重点文件：

| 仓库 | 当前重点 |
| --- | --- |
| N.E.K.O | `config/prompts/prompts_theater.py`、`services/theater/llm.py`、`services/theater/llm_performance_guard.py`、`services/theater/turn_service.py`、`services/theater/scene_memory.py`、`services/theater/pacing.py` |
| InkAI | `theater_generator/generation/mainline.py`、`theater_generator/generation/branches.py`、`theater_generator/validation_repair.py`、`theater_generator/assessment.py`、`theater_generator/quality.py`、`theater_generator/workflow.py` |

## 3. 当前规模证据

### 3.1 N.E.K.O

当前相关模块大致规模：

| 文件 | 行数 | 初步判断 |
| --- | ---: | --- |
| `config/prompts/prompts_theater.py` | 1307 | 剧本、自由、Router、焦点规划 Prompt 混在同一文件 |
| `services/theater/llm.py` | 1587 | Router、Actor、自由 Actor、焦点规划、Repair 编排混在同一调用层 |
| `services/theater/llm_performance_guard.py` | 1453 | 安全越权检查与语言质量检查混合 |
| `services/theater/scene_memory.py` | 248 | 场景回忆和语义重复锚点 |
| `services/theater/pacing.py` | 113 | 剧本节点停留次数和强制推进 |
| `services/theater/turn_plan.py` | 122 | 回合上下文统一对象 |

### 3.2 InkAI

当前相关模块大致规模：

| 文件 | 行数 | 初步判断 |
| --- | ---: | --- |
| `theater_generator/generation/prompts.py` | 223 | 公共阶段 Prompt 入口 |
| `theater_generator/generation/mainline.py` | 7096 | 主线骨架、单元、连续性、局部 Repair 和 fallback 高度集中 |
| `theater_generator/generation/branches.py` | 1100 | 分支意图、分支详情、分支连续性与 Repair |
| `theater_generator/workflow.py` | 1130 | 阶段编排、候选确认和生成统计 |
| `theater_generator/validation_repair.py` | 804 | 独立字段修复生成器 |
| `theater_generator/assessment.py` | 191 | 模型质量评估，设计上是建议性质 |
| `theater_generator/quality.py` | 441 | 确定性质量指标和结构分析 |

这些数字不是删除目标。它们只说明后续应先按职责拆分和调用证据盘点，不能直接按行数清理。

## 4. N.E.K.O 实际 Prompt 与模型调用链

### 4.1 剧本自由输入的实际路径

```text
玩家自由输入
  -> turn_service
  -> Router Prompt / route_free_input_async
  -> 作者入口或 stay
  -> TurnPlan
  -> Actor Prompt / generate_turn_async（焦点统计仅作只读观测上下文）
  -> 输出合同 + Ledger 交付检查 + Performance Guard
  -> 一次 Actor Repair
  -> 成功候选才提交 Session / Ledger / revision
```

对应入口：

- `services/theater/turn_service.py`：组装 Router、TurnPlan、pacing 和 Actor 调用；
- `services/theater/llm.py::route_free_input_async`：Router 首次调用和 Router Repair；
- `services/theater/llm.py::generate_turn_async`：Actor、一次 Actor Repair；焦点统计不再触发辅助模型调用；
- `config/prompts/prompts_theater.py::build_theater_route_prompts`：Router Prompt；
- `config/prompts/prompts_theater.py::build_theater_turn_prompts`：剧本 Actor Prompt。

### 4.2 单回合调用预算

在最宽路径上，一次剧本自由输入可能包含：

| 调用 | 是否默认发生 | 作用 | 初步分类 |
| --- | --- | --- | --- |
| Router | 通常发生 | 判断是否命中作者入口 | 安全/流程，保留 |
| Router Repair | 失败时 | 修复 Router 合同 | 安全合同，保留一次 |
| Focus Planner | 已暂停 | 给 Actor 规划本轮主要语言焦点 | 语言质量候选，当前不调用 |
| Actor | 通常发生 | 演绎当前节点 | 核心能力，保留 |
| Actor Repair | Guard 或合同失败时 | 修复结构或安全边界 | 保留一次，收窄范围 |
| Focus Repair | 已暂停 | 重复焦点后再次换观察焦点 | 语言质量候选，当前不调用 |

因此，Focus Planner 和 Focus Repair 不是世界状态安全所必需的调用。它们只影响旁白新鲜度和语言质量，不能和 Router、Event、StateDiff、失败不落盘混为同一等级。

### 4.3 剧本 Prompt 的层次

`config/prompts/prompts_theater.py` 当前至少包含：

1. Router v2 系统 Prompt；
2. 剧本 Actor 系统 Prompt；
3. 自由模式 RP-Hub 风格系统 Prompt；
4. 自由模式预注入消息；
5. Focus Planner 系统 Prompt；
6. Router、Actor、Focus Planner 的数据投影构造；
7. Actor Repair 的大量按错误码拼接的修复指令。

剧本 Actor 系统 Prompt 同时包含三类内容：

- **必须保留的安全边界**：不能替玩家行动、不能提交作者状态、不能泄漏内部 ID、不能跨场景确认未发生结果；
- **应保留但可以压缩的演出顺序**：旁白先完成动作，再对白回应，closing narration 承接对白；
- **需要观察后再决定的语言治理**：主焦点轮换、六字片段重复、稳定环境修饰语、感官词、具体身体部位、每回合必须有新观察等。

当前最大问题不是“没有规则”，而是三类规则被放进同一套长 Prompt，并且语言质量规则又有独立 Planner、Guard 和 Repair 三层重复执行。

### 4.4 N.E.K.O Prompt 过度设计候选

#### P1：Focus Planner 与 Focus Repair

位置：

- `config/prompts/prompts_theater.py::THEATER_FOCUS_PLAN_SYSTEM_PROMPT`；
- `services/theater/llm.py` 中 Focus Planner 调用和 Focus Repair 调用；
- `scene_focus_occupancy`、`_scene_focus_repair_reason` 相关代码。

问题：

- 用额外模型调用规划“本轮旁白应该观察什么”；
- Actor Prompt 已经携带 `required_primary_information`、`forbidden_primary_lanes`、`preplanned_execution` 和重复锚点；
- Repair Prompt 又重新携带一份 `scene_focus_execution`；
- 语言质量没有稳定提升的证据，却明显增加调用次数、Prompt 长度和失败分支；
- 当前用户反馈的“重复开头”并不能证明继续增加焦点规则有效。

处理结果：

- 已将 Focus Planner 与 Focus Repair 从 `generate_turn_async` 执行链移出；
- `scene_focus_occupancy`、Scene 观察投影、重复短片段锚点和 Actor 焦点 JSON 已从当前演绎链删除；
- 不删除 Scene 的事实安全锚点，只删除语言质量层的强制焦点编排；
- 当前 Prompt 只保留安全合同、演出时序和软篇幅参考，不再把语言焦点类别作为隐藏状态传给 Actor；
- 若人工试玩证明重复严重，再评估一个单层的确定性提示，不恢复多次模型规划。

#### P1：语言重复 Guard 与兜底台词池

位置：

- `services/theater/llm_performance_guard.py::_repeats_recent_dialogue`；
- `_repeats_recent_performance`；
- `_repeats_recent_narration`；
- `_repeats_established_fact_without_review`；
- `services/theater/llm.py::_safe_observation_repair_response`。

问题：

- 逐句相似、长片段相似、已公开事实重复、Scene 焦点重复各有一套判断；
- 阶段 0 收敛后，同一个输出最多触发一次 Actor Repair；
- 兜底台词池解决的是语言质量，不属于 Story/Session 安全；
- “不重复”检测可能反过来要求模型强行制造新观察，导致引入未登记物件或新的越界内容。

处理建议：

- 硬保留：内部字段泄漏、未提交 Choice 结果、未提交玩家动作、跨 Scene 确认、不可见道具物化、隐藏观察者和未落账事实；
- 暂降级：重复对白、重复旁白、已公开事实重述、焦点占用；先记录指标，不触发硬失败；
- 固定兜底台词池不再用于语言重复，Repair 后仍重复时保留模型正文；
- 任何删除前必须补一条“安全边界仍由哪一层承担”的测试说明。

本轮已收口：

- `llm_performance_guard` 不再为重复对白、重复旁白、已公开事实重述或问题镜像返回 Repair 原因；
- 删除了上述质量判断已无消费者的比较函数和旧测试挂点；
- 未提交 Choice/玩家动作、跨 Scene 确认、不可见道具、内部字段、隐藏观察者、事实交付和作者节拍合同仍由同一套硬检查负责；
- 回归测试明确验证重复演绎只保留一次 Actor 调用，不影响安全边界 Repair。

#### P2：Actor Prompt 中的篇幅和新颖度硬要求

位置：`build_theater_turn_prompts` 的 `narration_length`、`turn_focus_decision` 和“世界树内有界延展”。

问题：

- 同时要求段落长度、句数、前后旁白非空、一个主要焦点、一个新观察、不得重复、不能新增世界内容；
- 对短输入或确认型输入，这些要求彼此可能冲突；
- 长 Prompt 把“自然演绎”变成字段执行清单，容易导致机械化回复。

处理建议：

- 保留旁白时序和作者事实交付；
- 篇幅改成软目标，不因长度直接 Repair；
- “新观察”改为可选倾向，不要求每回合凑新信息；
- 开场、Choice 推进和普通 stay 回合只保留各自最小演出目标。

### 4.5 N.E.K.O 必须保留的 Prompt/Guard

这些内容虽然写在长 Prompt 或 Guard 中，但直接服务世界状态安全：

- Router 只能返回当前白名单入口；
- Actor 不提交 Choice、Event、StateDiff 或 Ledger；
- 不能把玩家未执行动作写成完成态；
- 不能把未提交场景转场写成已发生；
- 不能把不可见道具、隐藏观察者、未登记地点和量化结果写成事实；
- 旁白、对白和 closing narration 的时序不能互相矛盾；
- Event 交付必须由输出内容证明，不能只凭模型返回 ID；
- 内部稳定 ID、Runtime 字段和规则术语不得出现在公开正文；
- 模型失败时不得保存半回合。

这些是安全合同，不属于本轮要删除的语言质量层。

## 5. N.E.K.O 自由模式 Prompt 盘点

### 5.1 当前链路

自由模式由 `services/theater/llm.py::generate_free_turn_async` 直接调用：

```text
当前猫娘人格
  + 角色卡
  + 仅第一幕使用的 scenario / opening scene
  + 独立 Free Session 最近历史
  + 当前用户消息
  -> RP-Hub 风格原生消息序列
  -> 一次自由 Actor 调用
  -> 纯文本解析
  -> 写入 Free Session
```

当前自由模式没有剧本 Actor 的 Focus Planner、Actor Repair 和语言质量 Guard，这一点符合目标架构。

### 5.2 需要保留的自由模式 Prompt 设计

- 原生 `system / user / assistant` 消息顺序；
- 角色卡、用户信息、历史和当前输入分层；
- 当前猫娘人格优先于角色卡原始人格；
- 开场背景只在 opening 使用；
- 纯文本输出，不返回 JSON 外壳；
- 自由模式可以自然改变地点、人物和关系；
- 不把自由聊天写入正式 Story、Ledger 或长期记忆。

### 5.3 待验证但暂不改的自由 Prompt 项

`THEATER_FREE_PRELUDE_MESSAGES` 目前加入了 RP-Hub 风格的“分析困难/READY”预注入消息。它可能有助于模型进入角色扮演状态，也可能增加不必要的元话语和上下文长度。

阶段 0 不直接删除它。下一步需要用相同角色卡、相同模型和相同输入做 A/B 回放：

- 保留预注入消息；
- 只保留角色卡和当前对话；
- 比较首回合自然度、重复开头、角色一致性、响应时间和 token 消耗。

没有真实回放证据前，不把它判定为必须删除。

## 6. InkAI 生成器实际调用链

### 6.1 主线骨架

位置：`theater_generator/generation/mainline.py::generate_skeleton`。

当前特点：

- 主骨架存在 `for attempt in range(3)`，最多进行三次完整骨架模型调用；
- 数量偏差可能额外触发一次骨架重组 Prompt；
- 可按单元进行局部 skeleton repair；
- 玩家代理、环境测量、Choice 证据、时序和连续性错误会进入不同 Repair 分支；
- 结局方向在特定错误下还可独立修复两次。

其中需要保留的部分：

- 稳定 ID、图结构、Event 引用、玩家代理和事实来源的确定性校验；
- 不能让模型直接改变拓扑或跳过编译；
- 局部 Repair 不得覆盖用户已确认内容。

其中明显需要审查的部分：

- 一个阶段同时承担创意、结构、连续性、代理、测量和文案长度的全量检查；
- 首轮失败后有多个语义 Repair 分支，模型很难知道当前只需要修哪一层；
- 同一错误既可能进入模型 Repair，也可能进入 Python fallback，再被后续 Validator 再次检查。

### 6.2 主线单元生成

位置：`theater_generator/generation/mainline.py::generate_unit`。

当前特点：

- `max_attempts = 3`，普通单元最多初次生成加两次 Repair；
- Choice 来源边界、玩家代理、StateDiff 证据、环境细节、必需术语和目标正文可触发不同 Repair；
- 部分错误先做 Choice-only Repair，再回接到完整单元；
- 玩家承诺来源还可能继续执行最多两次的 setup repair；
- Repair 后仍有多个确定性 fallback。

需要重点拆开的两类问题：

| 类别 | 处理方向 |
| --- | --- |
| Choice、Event、StateDiff、玩家代理、稳定 ID、允许实体引用 | 保留合同校验和局部修复 |
| 选择文案字数、表达重复、表演节拍密度、环境修饰和语言丰富度 | 改为报告或低成本局部建议，不继续增加模型调用 |

### 6.3 分支生成

位置：`theater_generator/generation/branches.py`。

当前特点：

- 先生成意图池；
- 再为选中意图生成分支详情；
- 分支详情存在一次 Repair；
- 连续性候选也有独立生成和 Repair。

分支生成不能重新引入已经删除的动态 Runtime 支线。它只服务 InkAI 作者在编译前编辑静态 Story 图，不能成为 N.E.K.O 运行时动态规划入口。

### 6.4 独立 Validation Repair

位置：`theater_generator/validation_repair.py::ValidationRepairGenerator.propose`。

当前特点：

- 面向单个字段或 Choice 内部字段生成 patch；
- 最多一次模型候选加一次 JSON 修复；
- 有稳定 ID、字段范围和 fingerprint 保护。

这部分属于较好的局部修复范式，建议保留。需要补充的是：它不应与主线生成器里另一套相似的 Repair Prompt 继续重复维护，后续应统一 Repair 错误码、字段范围和 Prompt 片段。

### 6.5 质量评估与确定性指标

- `theater_generator/assessment.py`：模型质量评估，当前设计为 advisory，不应成为默认安装硬阻断；
- `theater_generator/quality.py`：图结构、可达性、汇流、节奏和文案的确定性指标；
- `theater_generator/workflow.py`：记录调用次数和 Repair 次数。

建议：

- 保留结构、安全和可达性硬错误；
- 质量评估只生成报告和建议；
- 把 Prompt 复杂度、Repair 次数、token、耗时和最终人工评分放在同一张观测表中；
- 不因为模型评分低就自动循环改写整份项目。

## 7. InkAI Prompt 过度设计候选

### 7.1 过度集中在一个生成回合的合同

当前主线 Prompt 同时要求模型处理：

- 故事语义；
- 节点和 Edge 关系；
- 玩家 Choice 文案；
- 玩家代理边界；
- Event、StateDiff 和事实来源；
- Scene 时间地点连续性；
- 道具和线索目录；
- required_terms；
- 字数、句式和文案风格；
- 必须自检和 Repair 反馈。

建议未来拆成三层，而不是继续向同一个 Prompt 添加条款：

1. **作者合同层**：只描述模型必须返回的结构、稳定 ID 引用和作者语义；
2. **安全校验层**：由 Python Validator 检查事实、代理、实体、时序和引用；
3. **质量建议层**：报告重复、长度、丰富度和节奏，不默认触发模型重写。

### 7.2 候选删除或降级项

以下项目进入候选，不在阶段 0 直接处理：

- 每个字段的精确字数目标；
- required_terms 对非专名、非事实证据文本的逐字要求；
- 为了防止相邻文案重复而进行的多轮局部 Repair；
- “模型返回前逐项自检”中无法被 Validator 验证的语言条款；
- 同一个环境事实在 Prompt、Repair Prompt、fallback 和质量报告中的重复描述；
- 只影响人工语言评分、不影响编译安全的表演密度检查。

### 7.3 必须保留的 InkAI 合同

- 结构化 JSON 只作为生成器内部候选格式，不能绕过编译器；
- Story v3、Ledger v2、Event 和事实生命周期字段；
- Choice 必须来自作者图，稳定 ID 不能由模型随意改写；
- 玩家未选择的行动不能被目标节点提前写成完成态；
- 只能引用已登记 Event、道具、线索和实体；
- 编译前后必须由 Validator 和 N.E.K.O 复验；
- 用户已确认的阶段结果不能被后续生成静默覆盖；
- 失败候选不能替换已接受版本。

## 8. 阶段 0 的删除候选清单

### 8.1 N.E.K.O

| 候选 | 当前判断 | 下一步证据 |
| --- | --- | --- |
| Focus Planner | 高概率过度设计 | 统计命中率、调用成本和人工质量差异 |
| Focus Repair | 高概率过度设计 | 与一次 Actor Repair 的通过率比较 |
| 重复旁白/对白硬 Repair | 语言质量候选 | 人工试玩确认是否真的影响体验 |
| 兜底台词池扩展 | 禁止继续扩展 | 保持现状，等待试玩 |
| 剧本 Actor 超长语言 Prompt | 高优先级收敛 | 拆分安全条款与软质量提示 |
| 自由模式 RP-Hub 预注入消息 | 待验证 | A/B 真实模型回放 |
| Scene Memory 语义锚点 | 待评估 | 区分恢复历史需要和重复治理需要 |
| Pacing | 可能属于剧情安全/节奏合同 | 核对 Story v3 使用者后再决定 |
| Event/StateDiff/事实生命周期 | 不删除 | 这是作者状态安全底座 |

### 8.2 InkAI

| 候选 | 当前判断 | 下一步证据 |
| --- | --- | --- |
| 多套主线 Repair Prompt | 高优先级合并 | 建立错误码到 Repair 入口映射 |
| Choice-only + 完整节点双层 Repair | 保留机制，收窄触发 | 对比混合错误的真实失败率 |
| 玩家承诺 setup 多次 Repair | 安全相关但可能过重 | 统计实际命中率和 fallback 成功率 |
| 字数、required_terms、文案重复硬约束 | 质量候选 | 改为报告后跑一轮生成基准 |
| `assessment.py` 模型质量评估 | 保留为 advisory | 确认不阻塞保存/安装 |
| `quality.py` 确定性图指标 | 保留结构安全部分 | 将语言质量指标与编译错误分离 |
| 旧小说/续写模块 | 明确废弃候选 | 完成入口、导入和测试消费者扫描 |

## 9. 后续实施顺序

阶段 0 之后，不直接同时删两个仓库的大量代码。建议顺序：

1. 已补 Prompt 调用观测：记录每个 operation 的调用次数、Repair 原因、通过/失败和耗时；
2. 已暂停 N.E.K.O Focus Planner 和 Focus Repair 的执行调用，并用真实回放验证调用下降；
3. 下一步做 N.E.K.O 剧本 Prompt 的安全层/质量层拆分，保持输出合同不变；
4. 做 InkAI Repair 入口和错误码合并，先不删除安全 Validator；
5. 接入 Free Role Card 生成和独立 Validator；
6. 有基线后再删除明确废弃模块和无消费者的历史实现；
7. 最后根据人工试玩决定是否删除剩余语言质量治理。

## 10. 代码注释规则

从阶段 1 的代码改动开始，所有新增或修改的代码必须使用清晰中文注释说明：

- 该模块或分支负责什么产品职责；
- 为什么它属于剧本模式、自由模式或共享基础设施；
- 为什么一个校验是安全硬阻断，或只是质量观测；
- 为什么需要一次 Repair，以及为什么不能继续重试；
- 失败时哪些状态不能写入；
- Prompt 投影为何只携带这些字段，哪些字段被刻意隔离。

注释不能只是把代码翻译成中文，也不能用“优化”“处理一下”这类没有边界的描述。Prompt 字符串附近的注释尤其要说明产品原因，避免后续继续叠加规则时失去判断依据。

## 11. 阶段 0 已实施的第一步

已先完成一项不改变演绎行为的观测修正：

- 已移除无调用方的 `theater_focus_plan`、`theater_focus_repair` 观测枚举；当前报表只保留 Router、Actor 和一次 Actor Repair；
- Repair 调用改为独立记录，并记录固定 Repair 原因码；
- 这些指标仍然不保存 Prompt、玩家输入或模型正文；
- 第一轮观测已确认 Focus 调用会显著增加真实回合成本，随后已将 Focus Planner/Repair 从执行链暂停；
- 新增观测单测，验证 Focus 调用和 Repair 原因能独立出现在评测报告中。

这一步先修正“看不见过度设计成本”的问题，再按真实回放暂停语言质量辅助调用；没有改变 Story、Ledger、Event、StateDiff、Router 或自由模式行为。

### 11.1 阶段 0 的真实回放收敛

使用当前 `complete` 的临时 Story，在内存中创建独立状态，执行 1 次开场和 3 次留场输入：

| 版本 | 成功回合 | 模型返回 | 调用构成 |
| --- | ---: | ---: | --- |
| 收敛前 | 4/4 | 12 | Actor 4、Actor Repair 3、Focus Planner 2、Focus Repair 3 |
| 收敛后 | 4/4 | 6 | Actor 4、Actor Repair 2 |

收敛后仍保留一次 Actor Repair；真实回放中 4 个回合全部成功，语言重复只记录为结果指标，未触发第三次模型调用或固定兜底改写。此次回放没有写入正式 Story、Session 或 Ledger。

### 11.2 已删除的过时测试

随着 Focus Planner/Focus Repair 和观察兜底退出执行链，`tests/unit/test_theater_llm_light.py` 中有一批测试只是在锁死旧的语言质量实现细节，已经不再代表当前产品合同。本轮删除 12 个测试：

- 观察回合固定兜底及其对白轮换、旁白去重测试 7 个；
- Focus 冷却 Repair、Focus 分类、Focus Plan 解析和 Planner 调用测试 5 个。

Story/Ledger、事实生命周期、候选事务回滚、角色隔离和当前 Actor 一次 Repair 的测试保留。删测后目标回归仍为 `117 passed`，没有用删测试的方式掩盖安全合同失败。

## 12. 阶段 0 完成标准

阶段 0 在以下条件全部满足后结束：

- 两个仓库的实际 Prompt 入口和模型调用已经列出；
- 剧本安全层、自由聊天层和语言质量层已经分开标记；
- N.E.K.O Focus Planner/Repair 及其无消费者的统计、Prompt 投影代码已经删除，不属于安全底座；
- InkAI 多层 Repair、质量评估和结构校验的职责已区分；
- 每个删除候选都有下一步证据要求；
- 没有在未验证的情况下删除代码或扩展规则；
- 后续第一批代码改动范围、注释要求和回归范围明确。

本文档完成不表示代码已经瘦身完成。它表示下一轮可以开始做有证据的最小改动。

# 小剧场运行端框架二次审查：优化、重构与模块增减

状态：**只读审查，未修改代码、测试、Prompt 或作者稿**。本文是**建议与证据**，不是已实施变更，也不是待办清单。现行合同仍以[架构开发文档](./neko-theater-architecture.md)第 2、4、10、13 节为准；失败样本、回退口径与验证等级仍以[问题文档](./neko-theater-issues-and-solutions.md)对应条目为准。

## 范围与方法

- 基线：`HEAD 201e4fc4a`（2026-09-12），但工作区有 58 个未提交修改。行号一律相对**当前工作副本**：源码最后修改时间为 `09-16 15:59`（`numeric_v2_evaluator.py`），文档为 `09-16 16:31—16:33`。引用行号前请先用同一份工作副本核对。
- 范围：`services/theater/` 全部 24 个模块、`main_routers/numeric_theater_router.py`、`config/prompts/prompts_theater.py`、`theater_workshop/sdk/generation/`，以及 InkAI `theater_generator/generation/` 的同源副本。
- 方法：主读源码 + 三条并行审计线（Prompt 资产、问题分层、调用链与时限）。**所有入选发现都由人工复读源码复核过**；审计过程中出现过的两处未成立断言已剔除，见文末「方法说明」。
- 未执行：未跑 pytest、未调用任何模型、未启动服务或前端、未做 HTTP/桌面/TTS/整剧压测。`903 项回归`属于前一轮回退核对，不是本次结论。

## 结论摘要

框架**不需要重写**。分层（Evaluator 判定 → Runtime 结算 → Actor 演绎 → Guard 复核 → Store 提交）有业务依据，上一轮 TF-01—03 审查中的「已排除结构」结论本轮复核后仍然成立。

但本轮确认 **3 类可优化问题**，按收益排序：

1. **缺一个结构化状态（结构性根因，P1）**：Session 没有「本回合已发生事实 / 作者声明的未完成义务 / 固定文本交付态」的结构化投影。当前 5 个开放问题（ST-11、ST-16、ST-17、ST-19、ST-20）加上动作归属与跨回合重复计分，都能追溯到这一个缺口，而不是 7 个互不相关的模型误判。
2. **复核链缺乏分级与整回合预算（体验与延迟，P1）**：快速复核与争议复查发送的是**同一份消息**，只有时限、思考参数和输出预算不同；服务端没有任何整回合时限兜底。
3. **Prompt 资产分散且含确定性冗余（维护性，P2）**：规则同时存在于 `config/prompts/` 与服务内联字符串，存在字节级重复段，且部分规则已被代码确定性校验却仍写在 Prompt 里。

| 编号 | 优先级 | 性质 | 问题 |
| --- | --- | --- | --- |
| FR2-01 | P1，结构性 | 缺少结构化的回合事实/义务/交付态投影，是多个开放问题的共同根因 | Session 只有 `transition_offered` 一个布尔，其余全靠在自然语言历史里重推 |
| FR2-02 | P1，契约 | 条件固定旁白是「先演 → 后判 → 再插」，时序错位是结构决定的 | Actor 看不到条件原文，触发由事后复核的引文证据决定 |
| FR2-03 | P1，延迟 | 复核阶梯不是「分级证据」，只是同一请求换更长时限；无整回合预算 | fast/dispute 共用同一 messages，8s/30s 均为单次时限 |
| FR2-04 | P2，性能 | 预算裁剪循环重复全量序列化与分词（O(k²)），无安全风险可修 | `_fit_*` 每次淘汰都重算全部 token |
| FR2-05 | P2，维护性 | Prompt 有两个归属地、重复段与「代码已保证却仍要求模型自觉」的规则 | 另有一处「2—3 条」由额外模型调用兜底 |
| FR2-06 | P2，维护性 | 三个超大函数承载全部契约文案，任何单点改动都易漂移 | 600 / 566 / 415 行 |
| FR2-07 | P3，维护性 | 跨模块重复实现与成对生命周期 API | `_model_config`、`_log_prompt_diagnostics`、两套 `_parse_output` |
| FR2-08 | P3，模块划分 | 无模块应删除；建议合并 1 个、拆出 2 处、新增 1 处投影 | 见「模块增减」 |
| FR2-09 | P3，跨仓库 | 两端共享规则已出现一处真实逻辑分叉，且无自动同步校验 | `node_type_label` 判定不同 |

## FR2-01：缺少结构化「已发生事实 / 未完成义务 / 交付态」

**现状证据**

1. `ScriptSessionV2` 的全部字段见 `numeric_v2_runtime.py:208-233`。与回合语义相关的结构化状态只有 `metrics`、`node_turn_count`、`dialogue_policy`、`transition_offered` 四项，没有「未完成的作者义务」「已发生事件」「已交付固定文本」。
2. Ledger 事件键为 `metric_changes / from_node_id / to_node_id / transition_intent`（`numeric_v2_runtime.py:561-587`），只记录结算结果，不记录承诺与待办。
3. 演员侧唯一接近「义务」的输入是散文注入：`numeric_v2_actor.py:2146-2150` 把固定旁白说明与「已展示原文可能含往事」拼接进 system prompt；`numeric_v2_fixed_narration.py:128-129` 用一句自然语言提示「当前尚有离幕前必显片段」。
4. 因此每条约束都要在**下一回合从自然语言历史里重新推导**，判定权交给模型，而模型的推导在多个实测轨迹里出错。

**这一项如何解释现有开放问题**（证据均来自问题文档）

| 开放问题 | 表现方向 | 与缺口的对应 |
| --- | --- | --- |
| ST-11 公开引文漏接 | 未演出却演了 | 引文是否「本幕已演出」没有结构化索引集，只能靠出处比对 |
| ST-20 留幕阶段偏离 | 未到却演了 | 作者时点事实（19:47 等）存在但未绑定为幕内可见状态 |
| ST-17 最终问答遗漏 | 演了才算完成却被放行 | 作者「必需问答」不是可核对的回合义务 |
| ST-19 固定文本时序 | 该交付未交付 | 触发被拒时程序不留「已触发未交付」态 |
| 重复演出/重复计分 | 已发生却没被记为已发生 | 已发生事件没有清单，只能做文本相等去重 |

**建议改法**：在 Session 增加**只登记事实与作者声明义务**的投影（不是目标锁），并由 Runtime 在提交时写入：本轮实际发生的事件主体与结果、作者显式声明的未完成义务、固定文本 `delivered_turn / dispatch_id`。同时把它作为 Actor 与 Guard 的共同只读输入。

**必须遵守的边界**：问题文档明确否决过 `pending_goals` / 完成锁存 / 独立推荐代理 / 短仲裁 / 超期重采样 / 推测 Actor（见 `neko-theater-issues-and-solutions.md:2794-2804` 一带）。本项**不得**复活「逐项完成锁」，只能记录「已发生」与「作者已声明的义务」，且不得成为新的换幕自动条件。

**风险与验证**：① 预算挤压 — 当前 Actor 固定上下文预算已紧（`numeric_v2_budget.py:13-22`，`input_max_tokens=10000`），新增投影需从被裁掉的历史里换取空间；② 一旦写成「任务清单」会重复历史错误。验证应复用 ST-11/16/17/19/20 的冻结反例与 2.138/2.139 负例，先证明不放松授权，再谈改善。

## FR2-02：条件固定旁白的时序错位是结构性的

**现状证据**

1. 条件片段（`trigger.type == "condition"`）的原文对 Actor 不可见：`numeric_v2_fixed_narration.py:126-127` 只投影触发条件文本，注释明确「Actor 不选择片段或结算依赖」。
2. 触发判定发生在 Actor 出稿**之后**：`review_candidates()`（`:133-135`）把 id/condition/after 交给复核器，`apply_triggers()`（`:138-159`）要求模型返回 `evidence` 且该证据必须逐字出现在玩家输入、本轮正文/旁白或历史里，随后才把原文插入。
3. 调用点在 `numeric_v2_workflow.py:868` 之后，即「最终复核没有正文违规」才插入。
4. `required_before_exit` 只能阻断换幕（`numeric_v2_runtime.py:513` 调用 `required_pending`），不能保证本轮交付。

**为什么是结构问题**：Actor 在写作时不知道本轮会不会展示某段原文，程序在它写完之后才决定。因此「正文已经演了阅读后的反应」「引文有出处但条件并未真正满足」这类情形不是措辞能修的，顺序本身决定了错位。问题文档 2.138 记录过一次通用对象格式候选以「玩家伸手拿起铭牌」为证据合法触发要求接触的旧日志（`:2682-2691`），正说明「引文存在 ≠ 条件成立」。

**建议改法**：把固定文本改为**交付事务**：候选触发 → 正文复核 → 交付记录（`delivered_turn / dispatch_id`）；触发被拒时保留「待交付」证据而不是整体不采信；在 Actor 的输入里显式给出「本轮将交付 X」这一确定事实（或把 condition 改成 Runtime 可判定的结构事件），使正文与原文的先后由程序决定。

**必须保留**：原文一字不改、每 Session/节点/编号只展示一次、依赖顺序、`required_before_exit` 阻断语义、`validate_delivery` 的提交与恢复校验（`numeric_v2_fixed_narration.py:162-199`）。

**验证**：`tests/unit/test_theater_numeric_v2_fixed_narration.py` 已有入口；另需 2.138 的 r32（已接过铭牌却延后交付）与 r34→r37 反例。

## FR2-03：复核链没有分级，也没有整回合预算

**现状证据**

1. 三个时限都是**单次调用**时限：`NUMERIC_V2_EVALUATOR_TIMEOUT_SECONDS = 12.0`、`NUMERIC_V2_TRANSITION_JUDGE_TIMEOUT_SECONDS = 8.0`、`NUMERIC_V2_DISPUTE_JUDGE_TIMEOUT_SECONDS = 30.0`（`numeric_v2_evaluator.py:39/44/49`），取用于 `:1655`，包裹单次调用在 `:1712-1716`。
2. 争议复查复用的是**同一份消息**：`numeric_v2_workflow.py:629-645` 用同一个 `review_kwargs` 再调一次 `validate_transition_offer(..., dispute_review=True)`；消息构造 `numeric_v2_evaluator.py:1689-1703` 参数完全相同，差异只有 `timeout`、`extra_body`（思考参数）与输出预算。也就是说这一步是「同一证据换更长时限重跑」，不是独立证据分级。
3. 单次复核的输入上限为 `judge_input_max_tokens=6000` / `formal_judge_input_max_tokens=8000`（`numeric_v2_budget.py:18-19`），且**超出即中止调用**（`numeric_v2_evaluator.py:1705-1711`），而 `_build_transition_judge_messages` 本身有 19,454 字符的内联规则文案（函数 `:671-1270`，600 行）。用 8 秒时限处理这个规模的输入，本身就不宽裕。
4. 服务端**没有整回合超时**：`numeric_theater_router.py` 直接 await。唯一的整回合硬上限是浏览器 660 秒（`static/js/theater_transport.js:14`）。
5. 实测归因（问题文档 `:2720`）：2.138 主线最长回合 **60.702 秒 = 累计复核 39.732 秒 + Actor 18.291 秒**，该轮 **11 次争议有 9 次超时**，并明确写着「不是单次复核运行了 50 多秒」。

**调用链**（正常 3—4 次，争议 5—7 次，全部串行）

1. Evaluator 判定 ×1（≤7000 入 / ≤360 出 / 12s）
2. 条件历史查找 ×1（仅当上一步返回 `history_query`；分页并发、总 12s）→ `numeric_v2_workflow.py:679-685`
3. Actor 正文 ×1（≤10000 入 / 35s；输出合同失败时最多 4 次尝试，`numeric_v2_workflow.py:319-362`）
4. 条件补推荐 ×1（推荐数 ∉ {2,3} 时）
5. Guard 快检 ×1（≤6000/8000 入 / 8s）
6. 条件争议复查 ×1（同消息 / 30s，整回合仅一次）
7. 条件改写 ×1（共用一次额度）+ 改后复检

复核与生成**天然无法并行**（复核的输入就是生成结果）；唯一已并发的点是历史分页。

**建议改法**（按收益/风险排序）

1. **争议复查改为定向小输入**：只带违规类别、涉事段落与最少证据，输出预算从 4096 降下来。预期把 30 秒级等待压到 10 秒级，并大幅减少输入 token。**前提**是玩家授权字段（`player_action_preserved`）、`initiation_authorized`、`acceptance_authorized` 与去向公开核对一项都不能少（`numeric_v2_evaluator.py:100-124`）。
2. **增加整回合时间预算**：达到预算时走**既有**兜底路径（末稿原子提交或留幕），不新增语义规则。
3. **快检输入瘦身**：把静态规则文案与 `data` 分离，压低 `formal_judge_input_max_tokens` 的实际占用。
4. **修 FR2-04 的 O(k²) 分词**（零安全风险）。

**不得为提速削弱**：玩家授权与关键决定归属、去向公开、Runtime 独占数值与选路、原子提交与幂等/身份复验、坏按钮只能定点删除不得改正文。

## FR2-04：预算裁剪的 O(k²) 重复分词（零风险）

`numeric_v2_actor.py:1912-1918` 的 `tokens()` 每次被调用都重新 `json.dumps` 整个 `fitted`（含 system prompt）再 `count_tokens()`；`refresh_story()`（`:1876-1893`）每次淘汰都从完整记录重新渲染 `story_so_far`；淘汰循环 `:1923-1926` 每弹一条就重算一次。同样的形状见 `_fit_simple_turn_prompt_data`（`:1853` 起）与 evaluator 的装箱循环。`tiktoken` 分词是同步执行的，直接占用事件循环。

**改法**：改为增量计数（淘汰时减去被移除部分的 token 数），或缓存 `system_prompt` 的 token 数只对变化部分重算。**风险**：极低，但必须保证裁剪结果与现状字节一致——可用现有 token tier 测试（`tests/unit/test_theater_numeric_v2_token_tiers.py`）做前后比对。

## FR2-05：Prompt 有两个归属地，且含确定性冗余

**现状证据**

1. 归属分散：JSON 契约与旁白简短规则在 `config/prompts/prompts_theater.py:17-30`，而绝大多数规则是服务内联字符串（`numeric_v2_actor.py` 的 `_system_prompt` 等）。`_system_prompt` 的静态文案为 9,881 字符，普通回合分支约 1,884 字符、换场分支 1,841 字符。
2. 字节级重复：推荐输入规则的同一段文字同时出现在 `numeric_v2_actor.py:1696-1697`（换场）与 `:1737-1738`（回合）；补推荐路径 `:1536-1541` 是同一规则的第三种改写。
3. 同一约束多处重述：「不得替玩家行动/交换行动主体」出现在 `:1663`、`:1669`、`:1719`、`:1752`、`:2081` 一带；换场三段职责在 `:1629-1638` 与 `:1664-1694` 两次说明。
4. **已被代码保证却仍要求模型自觉**：推荐条数与形状由 `numeric_v2_actor_output._parse_actor_suggestions`（`:194` 起）解析校验，但 `numeric_v2_actor.py:2606` 在条数 ∉ {2,3} 时**再发一次模型调用**去补。这属于「prompt 要求 + 代码校验 + 又一次模型调用」三者叠加，本可做成确定性规范化（保留前 2—3 条或丢弃多余项），直接省掉一次调用。同类冗余还包括 JSON 围栏/引号归一（`numeric_v2_actor_output.py:178-191`）、固定旁白字段与上限校验（`numeric_v2_fixed_narration.py:17-57`）。
5. 工坊侧相反：`SCORING_RUNTIME_RULES` 已抽成共享常量并在 5 处复用（`plan_review.py:12`、`facts.py:13`、`quality.py:39`、`quality.py:121`、`evidence.py:9`）。运行侧缺同类收敛。

**建议改法（最小可行）**：① 抽出 `SUGGESTED_INPUTS_RULE` / `SCENE_UPDATE_FIELD_RULE` 等共享常量，消除同文重复；② 把「2—3 条」改为确定性规范化，去掉补推荐调用；③ 明确 Prompt 归属：静态契约放 `config/prompts/`，动态装配留在服务内；④ 删除已由代码保证的格式复述。

**风险**：措辞改动会移动实测口径。按问题文档的纪律，Prompt 变更必须配同版本正反例，不能靠局部提交率宣布修复。

## FR2-06 / FR2-07 / FR2-09：维护性发现

- **FR2-06 超大函数**：`_build_transition_judge_messages`（`numeric_v2_evaluator.py:671`，600 行）、`_execute_numeric_v2_turn`（`numeric_v2_workflow.py:392`，566 行）、`_turn_messages`（`numeric_v2_actor.py:2111`，415 行）。建议按块拆函数并抽出规则常量；拆分目标之一是**发送内容不变**，可用字节比对校验。
- **FR2-07 重复实现**：`_model_config` 在 `numeric_v2_actor.py:2528` 与 `numeric_v2_evaluator.py:1554` 是逐字副本，差异仅错误类与配置键（Actor 取 `conversation`、Evaluator 取 `summary`，这是有意的模型分工，应保留为参数）；`_log_prompt_diagnostics` 两份（`actor:1951` / `evaluator:1404`）；`_parse_output` 两份（`actor_output:282` / `evaluator:1429`）；Runtime 与 Store 成对的 `end_session`、`resume_session`、`forget_history_through_current_revision`、`restore_story_session`、`story_session_guard` 属**委托**而非重复，但两套同名 API 容易误用，建议在文档中标注哪一层是唯一入口。
- **FR2-09 跨仓库漂移**：N.E.K.O `theater_workshop/sdk/generation/` 与 InkAI `theater_generator/generation/` 的同源文件当前**基本一致**（`runtime_rules.py`、`plan_review.py` 等仅差 docstring），但 `numeric_v2.py` 已出现一处真实逻辑分叉：`node_type_label` 判定为 `node_type in {"start", "scene"}`（N.E.K.O）与 `node_type == "scene"`（InkAI）。共享规则靠人工同步，缺自动校验。建议加一个只读的清单/哈希比对脚本，在两端任一侧改动时报警——本项只是建议，本轮未新增文件。

## 模块增减

先说结论：**没有任何模块应当删除**。我逐模块核对了生产消费者与测试引用：23 个 `numeric_v2_*`/辅助模块全部至少有一个生产 import 方，此前怀疑的 `maintenance / performance / usage / trace / cast / storage_transaction` 也均有生产引用与测试引用，不存在死模块。覆盖偏薄的地方是 `llm_context`（无专项测试，仅通过 Actor/Evaluator 间接覆盖）以及 `trace`、`storage_transaction`、`fixed_narration`（各 1 个专项测试文件）。

| 动作 | 对象 | 理由 |
| --- | --- | --- |
| 合并 | `numeric_v2_budget.py`（34 行） | 三档 profile 数值完全相同（`:11-25`），旧档位名已只作兼容别名。可降为常量并入 `numeric_v2_runtime.py` 或 `context.py`，减少一个只有 34 行却需要单独维护的模块 |
| 拆出 | `numeric_v2_actor.py` 的 Prompt 文案与消息装配 | 3,093 行中很大的比例是静态契约文案；拆到独立模块后，Prompt 变更可单独 review 与评测 |
| 拆出 | `numeric_v2_evaluator.py` 的 judge 消息构造 | 600 行单函数；拆块后可对每一块独立做正反例 |
| 新增 | 回合事实/义务投影 | 即 FR2-01；可先作为 `numeric_v2_context.py` 的一个投影函数落地，不必立刻新建模块 |
| 不建议新增 | 独立 Planner / Director / 自动文学评分 / 动态剧情规划 | 架构第 2 节已明确不设；本轮没有证据支持引入 |

## 不建议改动的部分（产品底线）

以下结构本轮复核后确认**不应**为「简化」而删除或合并：

- Runtime 独占数值、路线与提交规则；Actor 与推荐不能直接写 metric/节点/路线/Ledger。
- Guard 的玩家授权、主体归属、去向公开与作者硬边界字段；坏按钮只能定点删除，不能改正文。
- 原子提交、幂等、revision 与身份复验、`before_commit` 写栅栏、TTS 提交后播放且失败只降级为文字。
- 固定旁白原文不可由模型改写、每 Session/节点/编号只展示一次、`required_before_exit` 阻断语义。
- Runtime 与 Store 各自校验：提交输入、磁盘恢复与生命周期收尾面对不同信任边界。
- 三段 `segments` 持久化结构（删除旧生成分支不等于删除持久化结构）。
- Evaluator 故障降级为 no-op、正式转场复核失败不提交这类**保守**失败策略。

## 方法说明：两处未成立断言

审查过程中出现过两条看起来很有力但不成立的结论，**已在本文剔除**，记录在此以免后续被二手摘要误导：

1. 有断言称 `numeric_v2_evaluator.py:516-519` 在 `has_natural_ending=False` 时拼出**悬空逗号**、示例 JSON 非法。实际逐行拼接后两种分支都合法（条件片段的插入位置在中间而非末尾），该断言不成立。
2. 有断言称 `numeric_v2_actor.py:1750-1783` 是**死代码**。实际 `_system_prompt` 只被 `:2088`（`phase="opening"`）与 `:2134`（`transition_compact` 或 `turn`）调用，`opening` 不命中前两个 `return`，正是落到这一段——它是**开场分支**，不是死代码。

教训：Prompt/AST 类的结论必须回到源码复读后再引用；审计摘要适合用来定位，不适合直接当证据。

## 未验证范围

本轮未运行 pytest、未调用模型、未启动服务或前端、未做 TTS 与整剧/多路线压测，也未核实全部外部私有调用者。FR2-01／02／03 属于结构建议，落地前需按各项列出的反例与负例做同版本正反例验证；FR2-04 可用现有 token tier 测试做字节级前后比对；FR2-05 的 Prompt 改动需按问题文档纪律配正反例。**本文任何结论都不代表体验已验收。**

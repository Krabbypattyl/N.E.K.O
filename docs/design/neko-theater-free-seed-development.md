# N.E.K.O 自由模式 Free Seed 开发文档

## 1. 文档目的

本文定义自由模式从完整 Story Package 中提取最小运行输入的开发合同，作为自由演绎后续实现和测试的依据。

本阶段的目标不是修改 `neko_theater_story_v3` 导入合同，而是让自由 Runtime 在进入模型和创建 Free Session 时只使用自由模式需要的内容。

## 2. 当前问题

当前自由模式虽然不推进剧情图、Choice、Event、StateDiff 或正式 Ledger，但 `free_runtime` 仍然会：

- 通过 Story Loader 加载并校验完整 Story v3；
- 把完整 Story 对象传给自由 Actor；
- 在每个自由回合重新按完整 Story ID 恢复故事。

完整 Story v3 仍然必须保留给剧本模式，但自由 Actor 不应该看到作者图、未来节点、正式结局或内部稳定 ID。

## 3. 产品边界

### 3.1 剧本模式保持不变

以下内容继续属于完整 `neko_theater_story_v3`：

- Scenes、Narrative Nodes、Edges；
- Choice、Event、StateDiff 和正式 Ledger；
- 作者事实、道具、线索、节拍和正式结局；
- Story revision、导入校验和跨端事实生命周期合同。

本阶段不删除、不放宽、不兼容旧版 Story v3 字段。

### 3.2 自由模式只消费 Free Seed

自由模式读取故事的公开开场种子，并将自由回合写入独立 Free Session。自由输出不能回写完整 Story、正式 Ledger、剧本 Session 或长期记忆。

Free Seed 是只读投影，不是第二份可编辑剧情真源。

## 4. Free Seed v1 合同

Free Seed v1 当前由 N.E.K.O 从已通过 Story v3 校验的完整 Story Package 在内存中派生，不单独落盘。

```json
{
  "schema_version": "neko_theater_free_seed_v1",
  "source_story_id": "story_xxx",
  "source_story_revision": "1",
  "title": "故事标题",
  "theme": "故事主题",
  "scenario_card": {
    "player_role": "玩家身份",
    "catgirl_role": "猫娘在故事中的身份",
    "primary_goal": "故事目标或开场钩子"
  },
  "opening_scene": {
    "id": "scene_setup",
    "title": "初始场景标题",
    "text": "开场环境和已确认事实"
  },
  "restrictions": [],
  "runtime_guardrails": {},
  "seed": {
    "forbidden_assumptions": []
  }
}
```

### 4.1 必需内容

- `schema_version`：固定为 `neko_theater_free_seed_v1`；
- `source_story_id`、`source_story_revision`：绑定来源 Story 和 Session 恢复版本；
- `title`：故事标题；
- `opening_scene.id`、`opening_scene.title`、`opening_scene.text`：自由演绎的初始舞台；
- `scenario_card.player_role`、`scenario_card.catgirl_role`：开场身份锚点。

### 4.2 可选内容

- `theme`：故事主题；
- `scenario_card.primary_goal`：兼容来源 Story 的开场钩子字段；保留在 Free Seed 中，但不发送给自由 Actor，
  也不作为自由模式必须完成的作者目标；
- `restrictions`：服务端保留的来源边界，不作为自由 Actor 的剧情路线；
- `runtime_guardrails`：服务端保留的输出硬边界，不作为自由 Actor 的作者目标；
- `seed.forbidden_assumptions`：服务端保留的禁止假设，不作为自由 Actor 的固定场景锁。

### 4.3 自由 Actor 的实际演绎输入

自由 Actor 只接收以下角色驱动续写上下文，并直接返回 RP-Hub 风格的连续正文：

- 故事标题和主题；
- 玩家身份、猫娘故事身份和服务端猫娘人格摘要；
- `opening_scene` 只在第一回合作为第一幕背景发送，而不是后续回合持续使用的当前场景；
- 本场最近自由演绎历史；
- 玩家本轮输入。

自由 Actor 不再要求 `narration`、`dialogue`、`options`、`temporary_state` 或 `is_ending` JSON 字段。
自由 Session 只保存用户消息和一整段助手正文；HTTP 响应仍可使用 JSON 传输 `session_id`、revision、
生命周期和正文，这是服务端事务协议，不是模型输出格式。

自由 Actor 必须接受玩家明确动作和对角色的明确行动要求所产生的故事后果，不得代写玩家新的动作、对白、决定或内心想法；
猫娘和其他角色仍保留独立人格、动机和边界，但可以自然离开开场地点、引入人物和事件、发展关系并形成临时局势。

人格摘要仍然完整保留，但只表示长期性格、偏好、表达习惯和行为倾向。摘要中的字段必须按字段名解释：
“厌恶：下雨天不能出门玩”表示猫娘不喜欢这种情况，不表示她在下雨天不能出门，也不能把它当成自由模式的场景限制。

### 4.4 RP-Hub 角色卡机制与 N.E.K.O 映射

本次按 [STA1N156/RP-Hub](https://github.com/STA1N156/RP-Hub) 的原始代码确认：它的聊天提示不是把整张卡作为一个“作者目标”发送，而是按固定层次拼装上下文：

```text
[User Info]
[Character]
  Name
  Personality
[Scenario / First Message]
[Chat History]
[Current User Message]
```

RP-Hub 的角色卡核心字段和作用如下：

| 字段 | RP-Hub 用途 | N.E.K.O 自由模式映射 |
| --- | --- | --- |
| `name` | 角色名称 | 猫娘名称 |
| `description` | 角色卡简介/身份说明 | `scenario_card.catgirl_role` |
| `personality` | 角色长期性格和表达资料 | 服务端完整猫娘人格摘要 |
| `first_mes` | 首条角色消息，作为聊天历史中的 Assistant 开场 | `opening_scene` 只作为首回合背景，不作为持续场景锁 |
| `scenario` | 角色所处情境 | 只在首回合发送的 `[Scenario]`，后续由聊天历史接管 |
| `mes_example` | 示例对话，用于模仿角色口吻 | 当前没有独立字段，暂不虚构示例 |
| `creator_notes` | 给卡片使用者看的说明 | 不发送给自由 Actor |
| `system_prompt` | 卡片级系统提示 | 不直接复用，避免把 RP-Hub 的全局系统规则污染 N.E.K.O |
| `post_history_instructions` | 历史之后追加的指令 | 由 N.E.K.O 的自由模式系统提示和 `[Response Task]` 承担 |
| `alternate_greetings` | 多个可选开场白 | 当前自由模式不需要固定开场白列表 |
| `tags`、`creator`、`character_version` | 元数据 | 不参与自由演绎 |
| `extensions` | 扩展数据容器 | 只作为兼容导入信息，不进入当前自由提示 |
| `character_book` | 角色绑定世界书 | 当前不移植世界书运行时 |

RP-Hub 还支持三类独立扩展：

- 世界书条目：`comment`、`content`、`enabled`、`keys`、`useRegex`、`constant`、`position`、`order`、`depth`、`scanDepth`、`probability`、`useProbability`。它会扫描近期聊天，按关键词或常驻条件把内容插入系统提示、角色设定前后或历史深度位置。
- 正则脚本：`name`、`regex`/`findRegex`、`flags`、`replacement`/`replaceString`、`placement`、`markdownOnly`、`promptOnly`、`runOnEdit`、`minDepth`、`maxDepth`、`scope`、`disabled`。它用于提示或显示文本的后处理，不是角色人格本身。
- UI 模板：`id`、`name`、`enabled`、`scope`、`order`、`placement`、`htmlTemplate`、`initialVariableState`、`variableSchema`、`updateMode`。它负责聊天界面呈现和变量状态，不应进入 N.E.K.O 自由 Actor。

当前 N.E.K.O 已复刻 RP-Hub 的聊天上下文顺序和角色扮演核心规则，但只保留适合自由 Runtime 的内容：人格、身份、故事种子、首回合背景、历史和当前输入。自由模型直接返回正文；Session revision、幂等、独立目录和 TTS 认领仍由服务端负责，这不等于自由模式继续使用剧本模式的 Choice、Event、StateDiff 或作者目标。

### 4.5 明确排除的内容

以下内容不进入 Free Seed，也不发送给自由 Actor：

- 后续 Scenes、Narrative Nodes、Edges；
- 作者 Choice、Event、StateDiff 和正式 Ledger；
- 正式结局 ID、作者内部稳定 ID 和路径条件；
- 可能包含未来剧情、结局或关系变化的完整背景 synopsis；
- 模型地址、模型名、API Key；
- 前端传入的任意猫娘人格覆盖。

当前猫娘名称和人格由 N.E.K.O 服务端读取，模型配置也由服务端读取。

## 5. Runtime 处理流程

```text
完整 Story v3
    │ 导入校验
    ▼
Free Seed v1（内存只读投影）
    │
    ├── 自由 Actor：标题、主题、身份卡、人格摘要、开场 Scene、自由历史和玩家输入
    └── Free Session：story_id、story_revision、scene_id、turns、revision
```

启动和后续回合都必须使用同一来源 Story revision。来源 Story 被替换或 revision 不一致时，Free Session 失效，不进行猜测迁移。

## 6. 本阶段开发范围

本阶段实现：

1. 新增 `services/theater/free_seed.py`，集中定义 Free Seed v1 的构造和校验；
2. 自由 Runtime 启动时从完整 Story 构造 Free Seed；
3. 自由 Runtime 续写时重新构造同一版本的 Free Seed；
4. 自由 Actor 只接收 Free Seed 和当前 Scene；
5. 增加 Free Seed 合同、模型输入隔离和 Free Session 回归测试；
6. 保持现有 `/api/theater/free/session/*` 接口字段不变。

本阶段暂不实现：

- 独立 Free Seed JSON 文件导入；
- RP-Hub 角色 JSON 直接导入；
- 允许自由模式覆盖服务端猫娘人格；
- 修改 Story v3、Ledger v2 或 InkAI 跨端导出合同；
- 改动自由 Actor 的输出 JSON 合同。

## 7. 验收标准

- 自由 Actor 的输入中不出现 `narrative_nodes`、`edges`、`events`、`initial_ledger` 等作者图字段；
- 自由 Actor 仍能获得当前 Scene、角色身份和安全限制；
- 自由模式开场、自由输入、幂等、revision 冲突和用户离场测试通过；
- 自由 Session 仍只写入 `theater/free`；
- 剧本模式现有 Story 校验和运行时测试不受影响；
- 失败的自由模型回合仍不写入 Session；
- 所有新增或修改的代码包含清晰中文注释。

## 8. 后续阶段

当内存投影经过真实人工试玩验证后，再考虑让 InkAI 在生成剧本时同时生成一份独立角色卡，供自由模式使用。角色卡建议兼容 RP-Hub/SillyTavern 的核心字段：`name`、`description`、`personality`、`first_mes`、`scenario`、`mes_example`、`alternate_greetings`；世界书、正则脚本和 UI 模板暂不作为自由模式必需依赖。

角色卡应作为 Story Package 的独立可选产物或旁车文件，通过独立 Validator 校验；自由 Session 只读取它的公开角色资料，不把角色卡写回剧本 Node、Edge、Event 或 Ledger。实现前需要先确定角色卡版本、来源 revision、导出格式和用户人格摘要覆盖优先级。

Free Seed 仍可以独立导出为 `neko_theater_free_seed_v1` 文件。该文件应通过独立 Validator，并以 `source_story_id` / `source_story_revision` 或独立自由故事版本保持可追溯关系，不能让 `Story v3` 同时接受两种根结构。

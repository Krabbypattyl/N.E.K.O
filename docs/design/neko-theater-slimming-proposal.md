# N.E.K.O 小剧场瘦身记录

## 结论

本轮已把小剧场收敛为“作者静态剧情 + 情景内自由交流”。

- 剧本声明的主线、可见分支和隐藏分支负责所有剧情推进。
- 模型只判断本轮是否命中当前作者入口，并演绎当前猫娘的旁白与对白。
- 没有命中入口时留在当前场景自由交流，不生成临时支线、动态事实、动态 Choice 或动态结局。
- Session 锁、revision、幂等、恢复、角色归属、TTS 和安全护栏继续保留。

当前事实以 [小剧场架构](./neko-theater-architecture.md) 为准。

## 删除范围

动态支线链已整体删除：

1. 自由意图累计：`intent_tracker.py`
2. 动态规划：`branch_planner.py`
3. Patch 合同：`branch_patch_contracts.py`
4. 动态事实与 Goal：`branch_runtime.py`、`branch_fact_contracts.py`
5. 支线生命周期：`branch_lifecycle.py`
6. 合同公共层：`branch_contract_common.py`、`branch_contracts.py`
7. 混合事实视图：`fact_view.py`
8. 动态 Story 扩展校验：`story_dynamic_contracts.py`
9. 动态回合编排：`turn_branch_flow.py`

对应 Narrative Eval、动态 fixture、动态专项测试以及 v2.6/v2.7 实施文档一并删除。

## 保留范围

以下能力不是冗余架构，继续保留：

- Story Loader 与静态合同；
- 静态节点、边、前置事实、Choice 和结局；
- 作者隐藏意图白名单；
- Router、Actor 与一次结构修复；
- 模型输出安全检查；
- Session 原子读写、锁、revision 和幂等；
- active session 恢复与角色切换隔离；
- TTS 对话领取；
- Projector、模型原始返回记录、回合因果记录和低基数指标。

## 新回合语义

```text
Choice
  -> 解析当前作者 Choice
  -> 提交作者目标节点
  -> Actor 演绎

自由输入
  -> 精确 completion phrase
  -> 否则 Router 只选择当前 Choice / 作者隐藏意图 / stay
  -> 命中作者入口：提交作者目标节点
  -> stay：保持节点，Actor 在当前情景回应
```

旧版连续两次或三次表达后才触发动态规划的机制不再存在。作者隐藏边只要被 Router 唯一命中，就按剧本立即进入。

## 兼容性

已导入的旧 v2.5 Story 不要求立刻清理动态扩展字段。运行时忽略 `world_contract`、`narrative_goals`、`ending_domains` 和动态内容槽，只消费静态图字段。

旧 Session 中的动态支线状态也不再参与演绎。系统不会为清理这些字段破坏用户存档，而是从保存的当前作者节点继续。

## 当前剧本验证

`story_908bef0897ef.json` 使用字符串事实 `专业认可` 解锁“个人兴趣”分支。规则层现已支持字符串事实与结构化三元组事实，因此推进到“甜点征服”后会同时出现：

- 主线：接受当前猫娘的烘焙指导；
- 支线：询问当前猫娘的个人喜好和兴趣。

公开文案中的 `{{lanlan_name}}` 会替换为用户当前猫娘的实际名字。

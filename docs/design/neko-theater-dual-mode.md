# N.E.K.O 小剧场双模式设计与实施方案

状态：当前实施边界

## 页面与入口

- `/theater-home`：统一入口，只展示剧本模式、自由模式、玩法和产品优势；
- `/theater-numeric`：Numeric v2 剧本模式；
- `/theater`：角色卡自由模式；
- 两个演绎页都可以返回统一入口，但不能在同一 Session 内切换模式。

## 剧本模式

剧本模式只读取 `neko.story.numeric.v2` Story Package，使用独立 Numeric v2 Session、Ledger、恢复指针和表现历史。作者控制节点、隐藏数值、路线、分支和结局；Evaluator 只判定本回合数值变化，Actor 只生成表现文本和推荐输入，Runtime 负责确定性结算。

## 自由模式

自由模式暂时兼容 RP-Hub 角色卡。玩家原话直接进入自由 Actor，不使用 JSON 输入外壳，不读取或推进 Numeric v2 节点、数值、路线、Ledger 和结局，也不写入 Story Package 或正式长期记忆。临时场景、关系和事件只属于当前 Free Session。

角色卡最终格式尚未锁定，当前代码不得提前固化无法替换的专有合同。

## 隔离矩阵

| 能力 | 剧本模式 | 自由模式 | 是否共享 |
| --- | --- | --- | --- |
| Prompt | Numeric v2 Evaluator + Actor | Free Actor | 否 |
| Session | Numeric v2 Session | Free Session | 否 |
| 状态 | metric、node、revision | 沙盒历史 | 否 |
| 恢复指针 | v2 页面私有 key | Free 页面私有 key | 否 |
| 持久化 | `theater/numeric_v2/` | Free 私有目录 | 否 |
| 当前猫娘配置 | 服务端当前角色 | 服务端当前角色 | 是 |
| TTS 播放桥 | 已提交猫娘对白 | 自由猫娘对白 | 是 |

## 展示约定

- 玩家输入单独一行，展示为 `「玩家原话」`；
- 猫娘对白展示为 `「对白」`，不显示“猫娘：”前缀；
- 玩家与猫娘使用不同字体颜色；
- 服务端保存原始文本，TTS 不朗读前端引号或角色标记；
- 剧本模式不显示节点标题、隐藏数值或路线阈值。

## 角色切换与失败

- Session 绑定创建时的当前猫娘配置摘要；角色变化后旧剧本 Session 拒绝继续写入；
- 自由模式角色切换只结束自由模式活动 Session，不触碰 Numeric v2；
- 模型失败时两种模式都不能留下半条正式记录；
- TTS 失败降级为文字，不改变各自 Session 状态。

## 验收

1. 两个页面使用不同 API、存储目录和本地恢复 key；
2. 剧本模式刷新可恢复完整表现历史与 revision；
3. 自由模式不读取任何 Numeric v2 包或 Session；
4. 角色切换不会串用旧人格；
5. 两种模式均可通过同一播放器发声，但播放去重键包含各自 Session 和 revision；
6. 真实桌面设备发声仍需人工验收。

# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Theater model prompts."""  # noqa: DOCSTRING_CJK

NUMERIC_V2_ACTOR_JSON_INSTRUCTION = (
    "最终回复必须且只能是一个可由 JSON.parse 直接解析的 JSON object；禁止输出 Markdown 代码围栏、"
    "JSON 前后解释、标题或任何额外文字。所有键和字符串必须使用 JSON 双引号。"
)

# Assign an event by its actor, rather than requesting a second copy whenever an
# action changes an object. This is a generation contract, not a semantic filter:
# it grants neither an unknown outcome nor permission to act for the player.
NUMERIC_V2_ACTOR_EVENT_OWNERSHIP_INSTRUCTION = (
    "猫娘自身动作及其确定的直接可见结果，在 performance 中一次交付；"
    "括号承载这个动作及必要结果，括号外是她实际说出的对白。"
    "NPC、玩家的独立动作和对白不能写入猫娘动作括号。"
    "观察或操作许可只授权该过程，不证明未知成败或完整检查结论；关键结果仍须有实际依据。"
)

NUMERIC_V2_ACTOR_NARRATION_BREVITY_INSTRUCTION = (
    NUMERIC_V2_ACTOR_EVENT_OWNERSHIP_INSTRUCTION
    + "performance 中全角括号只写当前猫娘一个必要的即时动作及上述直接结果；"
    "对白不用引号，不写‘她说’、人物名动作或小说旁白。performance 通常一至三句，以自然对白为主，默认纯对白；"
    "最终全角动作括号最多一对。"
    "不写内心或罗列身体反应，不复述玩家刚做过的动作或把玩家原话当成自己的台词。"
    "玩家未表达的后续操作仍由玩家决定；独立外部变化或在场 NPC 的具体回应写 scene_update；"
    "不能为解释猫娘动作的结果先在旁白演一次，再让她重演。"
    "普通回合不得自行改变地点或进入 next，没有独立外部事件或 NPC 答复就省略 scene_update。"
)

NUMERIC_V2_ACTOR_OPENING_NARRATION_INSTRUCTION = (
    NUMERIC_V2_ACTOR_EVENT_OWNERSHIP_INSTRUCTION
    + "performance 中全角括号只写当前猫娘一个必要的即时动作及上述直接结果；"
    "对白不用引号，不写‘她说’、人物名动作或小说旁白。performance 通常一至三句，以自然对白为主，默认纯对白；"
    "最终全角动作括号最多一对。不写内心或罗列身体反应。"
    "环境起点与独立外部事件写 scene_narration，猫娘自身的入场动作留在 performance；"
    "两段共同建立开场，不在旁白预演猫娘再让她重演。"
)

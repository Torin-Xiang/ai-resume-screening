"""Prompt 模板集中管理（方案 §6.3：所有 prompt 走统一封装入口）。

两条硬性原则写进 prompt：
1. **禁止编造**：quote 必须是原文逐字片段；引不出来就承认 not_mentioned（方案 §4.3）。
2. **合规红线**：不得使用/推断受保护属性（方案 §4.5）。
"""

from __future__ import annotations

from .compliance import PROMPT_COMPLIANCE_CLAUSE

# ---------------- 结构化提取 ----------------
EXTRACT_SYSTEM = (
    "你是简历信息抽取助手。任务：从给定的简历原文中提取结构化字段，供 HR 初筛使用。\n"
    "严格约束：\n"
    "1. 只能使用简历原文中的信息，禁止使用外部知识、禁止推测、禁止编造。\n"
    "2. 每个字段必须给出 evidence.quote——简历原文中的**逐字连续片段**。\n"
    "   若找不到这样的片段，就把该字段 status 置为 not_mentioned 并把 value 置为 null。\n"
    "3. 简历未提某项 ≠ 该项不满足。未提及就是 not_mentioned，不要自行判断。\n"
    "4. years_of_experience 为数字（单位：年），按工作经历的时间跨度计算；"
    "   在校实习是否计入以简历原文表述为准；无法判断则 not_mentioned。\n"
    "5. 只输出 JSON，不要输出任何解释性文字。\n"
    + PROMPT_COMPLIANCE_CLAUSE
)

EXTRACT_USER_TEMPLATE = """简历原文（第 {pages} 页，共 {chars} 字）：
----- 原文开始 -----
{text}
----- 原文结束 -----

请抽取以下字段：name（姓名）、contact（联系方式，手机或邮箱）、education（学历层次）、
school（毕业院校）、major（专业）、years_of_experience（工作年限，数字）、
current_company（现公司）、current_title（现岗位）、skills（技能列表）、location（现居城市）、
work_experiences（工作经历列表：company/title/start/end）、education_records（教育经历列表）。

输出 JSON 格式：
{{
  "fields": {{
    "name": {{"value": "张三", "status": "extracted", "evidence": {{"quote": "姓名：张三", "page": 1}}}},
    ...
  }},
  "work_experiences": [{{"company": "...", "title": "...", "start": "...", "end": "...",
                        "evidence": {{"quote": "...", "page": 1}}}}],
  "education_records": [{{"school": "...", "major": "...", "degree": "...",
                        "evidence": {{"quote": "...", "page": 1}}}}]
}}
"""

EXTRACT_JSON_SCHEMA_HINT = {
    "type": "object",
    "properties": {
        "fields": {"type": "object"},
        "work_experiences": {"type": "array"},
        "education_records": {"type": "array"},
    },
    "required": ["fields"],
}


# ---------------- 软性维度评分（M4） ----------------
SCORE_SYSTEM = (
    "你是招聘初筛评估助手。任务：依据评分维度的**口径定义**判断候选人简历是否命中该维度。\n"
    "严格约束：\n"
    "1. 判定必须基于简历原文证据。每个维度都要给出 evidence（原文逐字片段）。\n"
    "2. 判定结果只能是三选一：satisfied（有证据表明命中/满足）、"
    "not_satisfied（有证据表明不满足）、not_mentioned（简历未提及，无法判断）。\n"
    "   注意：简历没写 ≠ 不满足。没写就是 not_mentioned。\n"
    "3. 加分项还需给出 hit_level（0/0.5/1）：完全命中=1，部分命中=0.5，未命中=0。\n"
    "4. 禁止编造简历中不存在的经历、公司、技能或数据。\n"
    "5. 只输出 JSON。\n" + PROMPT_COMPLIANCE_CLAUSE
)

SCORE_USER_TEMPLATE = """岗位：{job_title}

评分维度（口径以 definition 为准，正反例仅作参照）：
{dimensions}

候选人简历原文：
----- 原文开始 -----
{text}
----- 原文结束 -----

请对上面每个维度逐一判定，输出 JSON：
{{
  "results": [
    {{"id": "维度id", "judgement": "satisfied|not_satisfied|not_mentioned",
      "hit_level": 1, "reason": "一句话说明理由",
      "evidence": [{{"quote": "原文片段", "page": 1}}]}}
  ]
}}
"""


# ---------------- JD → 评分卡草稿（M3） ----------------
RUBRIC_SYSTEM = (
    "你是招聘评分卡设计助手。任务：根据 JD 生成一份**可编辑的评分卡草稿**，供 HR 调整。\n"
    "要求：\n"
    "1. 输出三类维度：hard_filters（必须项，不满足即一票否决）、bonus（加分项，带权重）、"
    "red_flags（减分项，带扣分）。\n"
    "2. 每个维度必须写清 definition（判定口径的自然语言定义）、positive_examples、"
    "negative_examples——口径不清会导致同一份简历每次判定不同，这是最需要避免的问题。\n"
    "3. 学历、工作年限、工作地点、专业、跳槽频次这类**能用规则判定的硬条件**，"
    "要放在 hard_filters 并标注建议的 rule id（min_degree / min_years / location / major / job_hop）。\n"
    "4. 权重建议：加分项权重 1-10，减分项扣分 1-10。\n"
    "5. 只输出 JSON。\n" + PROMPT_COMPLIANCE_CLAUSE
)

RUBRIC_USER_TEMPLATE = """岗位 JD：
----- JD 开始 -----
{jd}
----- JD 结束 -----

输出 JSON：
{{
  "job_title": "岗位名称",
  "jd_summary": "一句话概括",
  "hard_filters": [{{"id": "英文id", "name": "名称", "definition": "口径定义",
                   "positive_examples": ["..."], "negative_examples": ["..."],
                   "rule": "min_degree|min_years|location|major|job_hop|null",
                   "params": {{}}}}],
  "bonus": [{{"id": "...", "name": "...", "definition": "...", "weight": 10,
             "positive_examples": ["..."], "negative_examples": ["..."]}}],
  "red_flags": [{{"id": "...", "name": "...", "definition": "...", "penalty": 8,
                 "positive_examples": ["..."], "negative_examples": ["..."]}}]
}}
"""

"""离线脚本模型（provider=scripted）：不联网、不花钱、结果可复现。

**它不是模型。** 所有字段与判定都由确定性规则（正则 / 关键词 / JD 原文匹配）得出，
不具备任何语义理解能力。用途有三个：

1. 没有密钥或额度时，查看 CLI 与报告的**完整输出形态**；
2. 作为回归基线——同样的输入必须得到同样的输出；
3. 给"改标准后重排"这类纯计算流程提供稳定输入。

> **看格式，不要把内容当结论。** 真实筛选必须配模型密钥（见 `.env.example`）。

实现上照样遵守两条产品红线，否则演示出来的"形态"本身就是假的：

- **每条结论都带原文引用**，且 quote 取自 prompt 里给出的原文（逐字子串），
  因此能通过 `core/evidence.py` 的反编造校验；
- **找不到就返回 `not_mentioned`**，绝不为凑满字段而编造
  ——`not_mentioned` 与 `not_satisfied` 的区别正是三分组的地基。

关于 token：本适配器**上报 0**。它确实不消耗任何 token，往 run.json 里写
一个估算值会让"成本"这个字段失去可信度；要看成本量级请用真实模型跑一次。
"""

from __future__ import annotations

import re
from typing import Any

from resume_screening.core.llm import LLMResponse, Usage

_ORIGINAL = re.compile(r"----- 原文开始 -----\n(.*?)\n----- 原文结束 -----", re.S)
_JD = re.compile(r"----- JD 开始 -----\n(.*?)\n----- JD 结束 -----", re.S)

#: 中国手机号 11 位 = `1` + `[3-9]` + 9 位数字，中间可能夹分隔符（如 199-6217-1738）。
#: 这里写成 `1[3-9]\d` + 8 组，合计 11 位 —— 曾误写成 9 组（12 位），
#: 结果带横线的号码一个都匹配不上。
_PHONE = re.compile(r"1[3-9]\d(?:-?\d){8}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_CJK_LINE = re.compile(r"^[\u4e00-\u9fa5]{2,4}$")

#: 经历小节的标题。公司/岗位按"小节内第一段经历"取，不按公司名关键词猜——
#: 关键词法会把「西北农林科技大学」（含"科技"）当成公司。
_EXPERIENCE_HEADINGS = ("实习经历", "工作经历", "工作经验", "职业经历")
_TITLE_HINT = re.compile(r"(实习生|产品经理|工程师|助理|专员|组长|设计师|分析师)")

#: 招聘平台导出 PDF 的水印：一长串无中文、无空格的编码。它会被塞进正文，
#: 甚至出现在姓名之前，按"前几行找短中文行"取姓名时必须先剔掉。
_NOISE_MIN_LEN = 20

#: 学历关键词，按"高→低"排列，取命中的最高层次
_DEGREES = ("博士", "硕士", "研究生", "本科", "学士", "大专", "专科")
_DEGREE_ALIAS = {"研究生": "硕士", "学士": "本科", "专科": "大专"}

#: 识别城市用（脚本模型不做地理推断，只认显式出现的地名）
_CITY_HINTS = ("北京", "上海", "杭州", "深圳", "广州", "成都", "南京", "武汉", "西安", "苏州")


def _is_noise(line: str) -> bool:
    s = line.strip()
    return len(s) >= _NOISE_MIN_LEN and not re.search(r"[\u4e00-\u9fff]", s)


def _clean_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not _is_noise(ln)]


def _quote_for(text: str, needle: str) -> str | None:
    """取包含 needle 的整行原文作为证据（保证是逐字子串，能过校验）。"""
    for line in text.splitlines():
        if needle and needle in line and not _is_noise(line):
            return line.strip()
    return None


# ————————————————————— ① JD → 评分卡 —————————————————————


#: JD 里真正用来生成维度的两节。**必须按小节取**——把"岗位职责"也收进来，
#: 维度会变成一堆"负责 XX"的职责描述，评分卡就没法用了。
#: 同时收录两种小节写法：`## 任职要求`（Markdown）与裸行 `任职要求`（纯文本）。
_SECTION_LABELS = (
    "岗位职责",
    "任职要求",
    "任职资格",
    "岗位要求",
    "加分项",
    "加分点",
    "其他要求",
)
_IN_SCOPE_LABELS = ("任职要求", "任职资格", "岗位要求", "加分项", "加分点", "其他要求")
_SECTION_RE = re.compile(r"^(?:#{1,6}\s*)?(" + "|".join(_SECTION_LABELS) + r")\s*$")


def _jd_lines(jd: str) -> list[str]:
    """取出「任职要求 / 加分项」小节下以数字编号的条款行（折行自动并入上一条）。"""
    out: list[str] = []
    in_scope = False
    for raw in jd.splitlines():
        line = raw.strip()
        if not line:
            continue
        heading = _SECTION_RE.match(line)
        if heading:
            in_scope = heading.group(1) in _IN_SCOPE_LABELS
            continue
        if not in_scope:
            continue
        m = re.match(r"^\d+[.、]\s*(.+)$", line)
        if m:
            body = m.group(1).strip().rstrip("；;。")
            if len(body) >= 6:
                out.append(body)
            continue
        # 折行：JD 里的长条款会断行，续行不带编号，直接并回上一条
        if out:
            out[-1] = f"{out[-1]}{line.rstrip('；;。')}"
    return out


def _dim(body: str, *, dim_id: str, kind: str) -> dict[str, Any]:
    return {
        "id": dim_id,
        "name": body[:18],
        "definition": f"{body}（判据照抄 JD 原文，未做任何扩写）",
        "positive_examples": [f"简历中出现与「{body[:10]}」直接相关的经历描述"],
        "negative_examples": [f"简历中完全没有与「{body[:10]}」相关的内容"],
        "kind": kind,
        "rule": None,
        "params": {},
    }


def build_rubric_from_jd(jd: str, job_title: str) -> dict[str, Any]:
    """把 JD 条款按"能规则判定的进必须项、其余进加分项"分桶。

    与真实模型的差别：这里**不做语义归纳**，一条 JD 条款对应一个维度，
    definition 直接引用原文。所以维度会偏多、名字偏长。
    """
    hard: list[dict[str, Any]] = []
    bonus: list[dict[str, Any]] = []
    used: set[str] = set()

    def take(hard_filter: dict[str, Any], fallback: str) -> None:
        """同一维度只收一次；重复出现的同类条款顺延到加分项，避免 id 撞车。"""
        if hard_filter["id"] not in used:
            used.add(hard_filter["id"])
            hard.append(hard_filter)
        else:
            bonus.append(_dim(fallback, dim_id=f"b{len(bonus) + 1}", kind="bonus"))

    for body in _jd_lines(jd):
        if ("学历" in body or "本科" in body or "硕士" in body) and "min_degree" not in used:
            take(
                {
                    "id": "min_degree",
                    "name": "学历门槛",
                    "definition": f"{body}（判据照抄 JD 原文）",
                    "positive_examples": ["本科", "硕士", "博士"],
                    "negative_examples": ["大专", "专科"],
                    "kind": "hard_filter",
                    "rule": "min_degree",
                    "params": {"min_level": "本科"},
                },
                body,
            )
        elif ("AI" in body or "大模型" in body) and "ai_exp" not in used:
            take(_dim(body, dim_id="ai_exp", kind="hard_filter"), body)
        else:
            bonus.append(_dim(body, dim_id=f"b{len(bonus) + 1}", kind="bonus"))

    # 工作地点写在独立小节，不在「任职要求」里，单独扫一次
    if "杭州" in jd:
        hard.append(
            {
                "id": "location",
                "name": "工作地点",
                "definition": "JD 明确工作地点为杭州（判据照抄 JD 原文）。",
                "positive_examples": ["现居杭州", "可到杭州实习"],
                "negative_examples": ["仅接受其他城市"],
                "kind": "hard_filter",
                "rule": "location",
                "params": {"cities": ["杭州"]},
            }
        )

    for i, item in enumerate(bonus):
        item["weight"] = max(1, 10 - i)

    return {
        "job_title": job_title,
        "jd_summary": "由离线脚本模型从 JD 条款逐条映射生成，未做语义归纳",
        "hard_filters": hard[:4],
        "bonus": bonus[:6],
        "red_flags": [],
    }


# ————————————————————— ② 简历原文 → 字段 —————————————————————


def _field(value: Any, quote: str | None) -> dict[str, Any]:
    if value in (None, "") or not quote:
        return {"value": None, "status": "not_mentioned", "evidence": None}
    return {"value": value, "status": "extracted", "evidence": {"quote": quote, "page": 1}}


def extract_fields_from_text(text: str) -> dict[str, Any]:
    """从原文抽字段。抽不到就 `not_mentioned` —— 这是刻意的，不是缺陷。"""
    lines = _clean_lines(text)

    name = next((ln for ln in lines[:12] if _CJK_LINE.match(ln)), None)

    contact = None
    phone = _PHONE.search(text)
    email = _EMAIL.search(text)
    if phone:
        contact = phone.group()
    elif email:
        contact = email.group()

    education = None
    for key in _DEGREES:
        if key in text:
            education = _DEGREE_ALIAS.get(key, key)
            break

    school = next((ln for ln in lines if "大学" in ln or "学院" in ln), None)

    # 专业：学历所在行去掉学历词与日期后的中文片段；抽不到就承认未提及
    major = None
    if school is not None:
        idx = lines.index(school)
        for cand in lines[idx + 1 : idx + 4]:
            stripped = re.sub(r"(硕士|本科|学士|博士|研究生|大专|专科)", "", cand)
            stripped = re.sub(r"[|\d.\-—/至年月日\s（）()]+", "", stripped)
            if 2 <= len(stripped) <= 20 and len(stripped) == len(
                re.sub(r"[^\u4e00-\u9fa5A-Za-z]", "", stripped)
            ):
                major = stripped
                break

    location = next((c for c in _CITY_HINTS if c in "\n".join(lines[:6])), None)

    # 公司 / 岗位：取「实习经历」小节里的第一段经历（简历通常从最近往前写）。
    # 不按公司名关键词猜——「西北农林科技大学」含"科技"，关键词法会把它当公司。
    company = title = None
    exp_at = next((i for i, ln in enumerate(lines) if ln in _EXPERIENCE_HEADINGS), None)
    if exp_at is not None:
        window = lines[exp_at + 1 : exp_at + 4]
        if window:
            head = window[0]
            company = head.split("|")[0].strip() or None
            # 岗位可能与公司同行（「点涨科技| 产品实习生」），也可能在下一行
            for cand in window:
                m = _TITLE_HINT.search(cand)
                if m:
                    title = cand.split("|")[-1].strip() if "|" in cand else cand
                    break

    return {
        "fields": {
            "name": _field(name, name),
            "contact": _field(contact, _quote_for(text, contact) if contact else None),
            "education": _field(education, _quote_for(text, education) if education else None),
            "school": _field(school, school),
            "major": _field(major, _quote_for(text, major) if major else None),
            "years_of_experience": _field(None, None),  # 应届生简历不做年限推断
            "current_company": _field(company, company),
            "current_title": _field(title, title),
            "skills": _field(None, None),
            "location": _field(location, _quote_for(text, location) if location else None),
        },
        "work_experiences": [],
        "education_records": [],
    }


# ————————————————————— ③ 维度判定 —————————————————————

#: 评分 prompt 里维度的渲染格式（`core/scoring.py::_render_dimensions`）：
#: `- id=xxx｜维度名｜类型=hard_filter`。脚本模型从 prompt 里读**真实**的 id 与名字，
#: 而不是自己维护一份副本——副本一旦与评分卡错位，理由里就会写成
#: 「命中「数据意识与分析」」而方括号里是「熟悉主流大模型产品」这种自相矛盾的输出。
_DIM_LINE = re.compile(r"^- id=(\S+?)｜(.+?)｜类型=", re.M)

#: 维度 id → 命中关键词。关键词只能硬编码（脚本模型没有语义理解）；
#: 命中不了就返回"未提及"——不猜。id 认不出时同样返回未提及。
_DIM_KEYWORDS: dict[str, tuple[str, ...]] = {
    "ai_exp": ("AI", "大模型", "AIGC", "Agent", "LLM", "Prompt"),
    "b1": ("大模型", "ChatGPT", "通义千问", "DeepSeek", "文心"),
    "b2": ("逻辑", "结构化", "PRD", "需求文档", "竞品"),
    "b3": ("数据", "分析", "Excel", "SQL", "指标"),
    "b4": ("协作", "跨部门", "协同", "推动", "沟通"),
    "b5": ("关注", "热情", "学习", "迭代", "方法论"),
    "b6": ("从0 到1", "从 0 到 1", "从零到一", "0 到 1", "独立设计", "孵化"),
}


def judge_dimensions(dimensions: list[dict[str, Any]], text: str) -> dict[str, Any]:
    """对 prompt 中出现的每个维度给一条判定。

    `dimensions` 从 prompt 解析而来（`_DIM_LINE`），因此名字与评分卡一致。
    """
    results: list[dict[str, Any]] = []
    for dim in dimensions:
        dim_id = str(dim.get("id") or "")
        name = str(dim.get("name") or dim_id)
        keywords = _DIM_KEYWORDS.get(dim_id, ())
        quote = next((q for q in (_quote_for(text, k) for k in keywords) if q), None)
        if not quote:
            results.append(
                {
                    "id": dim_id,
                    "judgement": "not_mentioned",
                    "hit_level": 0,
                    "reason": f"规则未在原文中找到与「{name}」相关的片段，按未提及处理",
                    "evidence": [],
                }
            )
            continue
        results.append(
            {
                "id": dim_id,
                "judgement": "satisfied",
                "hit_level": 1,
                "reason": f"原文出现关键词，命中「{name}」（脚本模型按关键词判定）",
                "evidence": [{"quote": quote, "page": 1}],
            }
        )
    return {"results": results}


# ————————————————————— 适配器 —————————————————————


class ScriptedLLMClient:
    """按 prompt 类型走不同规则；完全确定性，无随机、无网络。"""

    name = "scripted"

    def __init__(self, *, model: str = "scripted-1", **_: Any) -> None:
        self.model = model
        self.extract_calls = 0
        self.score_calls = 0
        self.rubric_calls = 0

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        from resume_screening.core.prompts import RUBRIC_SYSTEM, SCORE_SYSTEM

        # 延迟导入：避免模块级循环依赖（prompts 会 import compliance）
        from resume_screening.core.prompts import EXTRACT_SYSTEM

        usage = Usage(model=self.model)  # 离线：确实不消耗 token，如实上报 0

        if system == RUBRIC_SYSTEM:
            self.rubric_calls += 1
            m = _JD.search(user)
            jd = m.group(1) if m else user
            title = ""
            t = re.search(r"岗位名称\s*\n\s*(.+)", jd)
            if t:
                title = t.group(1).strip()
            return LLMResponse(payload=build_rubric_from_jd(jd, title), usage=usage, raw="scripted")

        if system == EXTRACT_SYSTEM:
            self.extract_calls += 1
            m = _ORIGINAL.search(user)
            text = m.group(1) if m else user
            return LLMResponse(payload=extract_fields_from_text(text), usage=usage, raw="scripted")

        if system == SCORE_SYSTEM:
            self.score_calls += 1
            m = _ORIGINAL.search(user)
            text = m.group(1) if m else user
            # 维度清单从 prompt 里解析，保证理由里的名字与评分卡一致
            dims = [{"id": i, "name": n} for i, n in _DIM_LINE.findall(user)]
            return LLMResponse(payload=judge_dimensions(dims, text), usage=usage, raw="scripted")

        return LLMResponse(payload={}, usage=usage, raw="scripted")

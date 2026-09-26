"""离线演示服务器：不联网、不需要 API key，用脚本化模型喂出一份真实的工作台。

存在的理由有两个，都不是"图方便"：

1. **让界面能被验收。** 判断内核的正确性有单测覆盖，但"HR 打开这个页面看到的
   到底是什么"只能在真浏览器里看。没有这个脚本，验收就得先申请 API key、
   再准备真实脱敏简历，门槛高到没人会天天跑，界面就会悄悄退化。
2. **让"三组都有人"成为常态。** 合成一份必然产生 推荐 / 待确认 / 不推荐 的数据，
   否则每组各一条的视觉问题（黄色标签、空组、理由过长）永远碰不到。

刻意**不使用** `LLM_PROVIDER=fake` 那个通道：它的默认客户端对任何 prompt 都返回 `{}`，
结果是所有字段"未提取"、所有人都落进待确认——看起来像跑的，其实什么都没验证。

用法：
    python scripts/offline_demo.py                # 起服务，浏览器打开后自己看（Ctrl+C 停止）
    python scripts/offline_demo.py --smoke        # 起服务 + 跑浏览器冒烟 + 退出（返回码即结论）
    python scripts/offline_demo.py --port 8011 --open   # 换端口并自动打开浏览器
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import fitz  # noqa: E402  PyMuPDF
import httpx  # noqa: E402
import uvicorn  # noqa: E402

from resume_screening.adapters.parsers.pdf_pymupdf import PdfPymupdfParser  # noqa: E402
from resume_screening.core.llm import LLMResponse, Usage  # noqa: E402
from resume_screening.core.prompts import EXTRACT_SYSTEM, PROPOSE_SYSTEM, SCORE_SYSTEM  # noqa: E402
from resume_screening.core.rubric import Rubric  # noqa: E402
from resume_screening.services import jobs as jobs_service  # noqa: E402
from resume_screening.store.db import Database  # noqa: E402
from resume_screening.web.app import create_app  # noqa: E402

# ——————————————————————— 三个虚构候选人 ———————————————————————
# 每个候选人的 PDF 正文与"模型返回的证据"必须逐字对得上：提取环节会校验
# quote 是否真的出现在原文里，对不上就降级为"未提及"——这正是设计原则第 2 条。
#
# 编排上刻意让三个人落进三个不同的分组，好把三分组的语义一次看全：
#   王示例 → 推荐（必须项满足，且命中加分项）
#   李示例 → 待确认（**简历没写学历**：必须项未提及，不得据此淘汰）
#   赵示例 → 不推荐（学历明确不满足：必须项一票否决）
# 「未提及」与「不满足」必须分到两组，是这套东西最不能省的一条。

CANDIDATES: list[dict] = [
    {
        "name": "王示例",
        "file": "王示例-AI应用开发工程师.pdf",
        "lines": [
            "个人简历",
            "姓名：王示例",
            "联系电话：13800000001 邮箱：wang@example.com",
            "求职意向：AI 应用开发工程师",
            "教育背景",
            "2018.09-2022.06 示例大学 计算机科学与技术 本科",
            "工作经历",
            "2022.07-至今 示例科技有限公司 算法工程师",
            "负责 RAG 检索增强生成系统的设计与上线",
            "独立微调过开源大模型并部署到生产环境",
            "搭建数据清洗与效果评测流水线，把迭代周期从两周压到三天",
            "项目经历",
            "企业知识库问答助手：基于向量检索实现多轮问答，覆盖内部十二个业务部门",
            "上线后问答准确率由百分之六十提升到百分之八十五，月活使用人数超过两千",
            "技能：Python、PyTorch、RAG、Agent、Docker",
            "自我评价：习惯用数据验证效果，能独立推进从原型到上线的完整链路",
        ],
        "contact": "13800000001",
        "education": "本科",
        "school": "示例大学",
        "major": "计算机科学与技术",
        "company": "示例科技有限公司",
        "title": "算法工程师",
        "skills": ["Python", "PyTorch", "RAG"],
        "years": 4,
        # 加分项：有证据表明命中 → 推荐
        "ai_exp": ("satisfied", 1, "简历写明负责 RAG 系统上线并独立微调模型，符合口径中的正例"),
        # 评分证据必须逐字出现在**这个人自己的简历**里。写死一句通用文案会踩坑：
        # 别人的简历里没有这句话时，`next(...)` 抛出的 StopIteration 在生成器里
        # 会被转成 RuntimeError，整份简历直接 failed——而且报错信息完全看不出原因。
        "quote": "负责 RAG 检索增强生成系统的设计与上线",
    },
    {
        "name": "李示例",
        "file": "李示例-数据平台开发.pdf",
        "lines": [
            "个人简历",
            "姓名：李示例",
            "联系电话：13800000002 邮箱：li@example.com",
            "求职意向：数据平台开发工程师",
            "教育背景",
            "2020.09-2023.06 示例理工大学 软件工程",
            "工作经历",
            "2023.07-至今 示例数据科技有限公司 后端工程师",
            "负责离线数仓调度平台的开发与维护",
            "主导把原有定时任务迁移到分布式调度框架，任务失败率下降一半",
            "项目经历",
            "实时指标看板：承接业务方二十余项核心指标的秒级查询需求",
            "通过分层建模与预聚合把看板首屏加载时间从八秒降到一点五秒",
            "技能：Java、SQL、Flink、Kafka、Doris",
            "自我评价：做过完整的数据链路建设，偏工程实现，习惯先量化再优化",
        ],
        "contact": "13800000002",
        # 简历里没写学历层次 —— 于是硬门槛"未提及"，落入待确认而不是被淘汰
        "education": None,
        "school": "示例理工大学",
        "major": "软件工程",
        "company": "示例数据科技有限公司",
        "title": "后端工程师",
        "skills": ["Java", "SQL", "Flink"],
        "years": 2,
        # 加分项：简历根本没提 AI 项目 → 只是拿不到这一项的分，不影响结论
        "ai_exp": ("not_mentioned", 0, "简历未提及 AI 项目经历，无法判断（未提及不等于不满足）"),
        "quote": "负责离线数仓调度平台的开发与维护",
    },
    {
        "name": "赵示例",
        "file": "赵示例-前端开发.pdf",
        "lines": [
            "个人简历",
            "姓名：赵示例",
            "联系电话：13800000003 邮箱：zhao@example.com",
            "求职意向：前端开发工程师",
            "教育背景",
            "2021.09-2024.06 示例职业学院 软件技术 大专",
            "工作经历",
            "2024.07-至今 示例网络科技有限公司 前端工程师",
            "负责管理后台页面的开发与维护",
            "参与组件库建设，把重复表单场景的开发工作量减少约三成",
            "项目经历",
            "运营活动配置平台：支持业务方自助搭建落地页，无需前端介入",
            "上线半年支撑了四十余场活动，页面平均搭建耗时从一天降到两小时",
            "技能：JavaScript、Vue、CSS、Node.js",
            "自我评价：做事细致，愿意承接没人愿意接的繁琐页面改造工作",
        ],
        "contact": "13800000003",
        "education": "大专",
        "school": "示例职业学院",
        "major": "软件技术",
        "company": "示例网络科技有限公司",
        "title": "前端工程师",
        "skills": ["JavaScript", "Vue", "CSS"],
        "years": 1,
        # 学历硬门槛由规则判定为不满足 → 一票否决 → 不推荐
        "ai_exp": ("not_satisfied", 0, "简历未涉及 AI 相关工作内容"),
        "quote": "负责管理后台页面的开发与维护",
    },
]

RUBRIC: dict = {
    "version": "v1",
    "job_title": "AI 应用开发工程师",
    "jd_summary": "负责 RAG 与 Agent 应用落地，需有 AI 项目实操经验",
    "hard_filters": [
        {
            "id": "deg",
            "name": "本科及以上",
            "definition": "最高学历为本科及以上。以简历写明的最高学历为准。",
            "positive_examples": ["本科", "硕士", "博士"],
            "negative_examples": ["大专", "中专"],
            # 硬门槛用规则而不是模型：布尔判断，规则成本为 0、可解释、不会漂移
            "rule": "min_degree",
            "params": {"min_level": "本科"},
        }
    ],
    "bonus": [
        {
            "id": "ai_exp",
            "name": "AI 项目经验",
            "definition": "实际参与过 AI 能力的设计或交付，含模型微调、检索增强、Agent 编排。"
            "仅调用现成的通用接口写业务逻辑不算。",
            "positive_examples": ["独立微调过模型", "搭建过 RAG 检索流程"],
            "negative_examples": ["仅调用接口写周报", "只做过数据处理脚本"],
            "weight": 20,
        }
    ],
    "red_flags": [],
}


# ——————————————————————— 脚本化模型 ———————————————————————


def _pick(candidate: dict, *, page: int = 1) -> dict:
    """按候选人产出一份"模型提取结果"，每项都带能在原文里找到的证据。"""

    def item(value: object, quote: str) -> dict:
        return {"value": value, "status": "extracted", "evidence": {"quote": quote, "page": page}}

    def absent() -> dict:
        """简历里确实没有这一项 —— 与"提取失败"是两回事，但都只能给未提及。"""
        return {"value": None, "status": "not_mentioned", "evidence": None}

    judgement, hit_level, reason = candidate["ai_exp"]
    education = (
        item(candidate["education"], candidate["education"])
        if candidate["education"]
        else absent()
    )
    return {
        "fields": {
            "name": item(candidate["name"], candidate["name"]),
            "contact": item(candidate["contact"], candidate["contact"]),
            "education": education,
            "school": item(candidate["school"], candidate["school"]),
            "major": item(candidate["major"], candidate["major"]),
            "years_of_experience": item(candidate["years"], "至今"),
            "current_company": item(candidate["company"], candidate["company"]),
            "current_title": item(candidate["title"], candidate["title"]),
            "skills": item(candidate["skills"], "技能"),
        },
        "_ai_exp": (judgement, hit_level, reason),
    }


class DemoLLM:
    """按 prompt 类型返回内容；提案逻辑按 HR 的说法走不同分支。"""

    name = "demo"

    def __init__(self) -> None:
        self.extract_calls = 0
        self.score_calls = 0
        self.propose_calls = 0

    def _candidate_for(self, user: str) -> dict:
        # 用"这份简历里出现了谁的名字"来分派，等价于真实模型读了正文再回答
        for candidate in CANDIDATES:
            if candidate["name"] in user:
                return candidate
        return CANDIDATES[0]

    def complete_json(self, *, system, user, json_schema=None, temperature=0.0):
        usage = Usage(model="demo", prompt_tokens=120, completion_tokens=60)

        if system == EXTRACT_SYSTEM:
            self.extract_calls += 1
            payload = _pick(self._candidate_for(user))
            payload.pop("_ai_exp", None)
            return LLMResponse(payload=payload, usage=usage, raw="demo")

        if system == SCORE_SYSTEM:
            self.score_calls += 1
            candidate = self._candidate_for(user)
            _, hit_level, reason = candidate["ai_exp"]
            quote = candidate["quote"]
            if quote not in user:  # 兜底：宁可返回"未提及"，也不要抛异常让整份简历挂掉
                return LLMResponse(
                    payload={
                        "results": [
                            {
                                "id": "ai_exp",
                                "judgement": "not_mentioned",
                                "hit_level": 0,
                                "reason": "简历中未找到与该维度相关的原文，无法判断",
                                "evidence": [],
                            }
                        ]
                    },
                    usage=usage,
                    raw="demo",
                )
            return LLMResponse(
                payload={
                    "results": [
                        {
                            "id": "ai_exp",
                            "judgement": candidate["ai_exp"][0],
                            "hit_level": hit_level,
                            "reason": reason,
                            "evidence": [{"quote": quote, "page": 1}],
                        }
                    ]
                },
                usage=usage,
                raw="demo",
            )

        if system == PROPOSE_SYSTEM:
            self.propose_calls += 1
            return LLMResponse(payload=_proposal_for(user), usage=usage, raw="demo")

        return LLMResponse(payload={}, usage=usage, raw="demo")


UTTERANCE_PATTERN = re.compile(r"----- 原话开始 -----\n(.*?)\n----- 原话结束 -----", re.S)


def _proposal_for(prompt: str) -> dict:
    """按 HR 的说法给出不同的变更清单，把三条路径都演示到。

    注意这里必须**只按 HR 的原话**分支，不能拿整个 prompt 去匹配：
    prompt 里同时包含着当前标准的 JSON，而标准里就写着"本科及以上""学历"
    这些词——拿全文匹配的话，无论 HR 说什么都会命中"删掉学历门槛"那一支。
    （这个坑我踩过：演示里点"提高权重"，标准却被删掉了必须项。）

    另外，合规拦截发生在**调用模型之前**，所以"把 35 岁以上的筛掉"这类说法
    根本走不到这里——那是刻意的顺序（技术方案 §7）。
    """
    matched = UTTERANCE_PATTERN.search(prompt)
    utterance = matched.group(1) if matched else prompt

    deg = next(d for d in RUBRIC["hard_filters"] if d["id"] == "deg")
    ai = next(d for d in RUBRIC["bonus"] if d["id"] == "ai_exp")

    if any(word in utterance for word in ("学历", "本科", "硬性", "卡")):
        # 删掉必须项：能精确预估，而且会让"不推荐"的人明显上移，最值得看的一条影响
        return {
            "summary": "取消学历硬门槛，学历不再一票否决",
            "patches": [
                {
                    "op": "remove",
                    "kind": "hard_filter",
                    "target_id": "deg",
                    "before": deg,
                    "after": None,
                    "reason": "业务方反馈该岗位更看实际项目能力，学历不作为硬性条件",
                }
            ],
        }

    if any(word in utterance for word in ("权重", "加大", "提高", "更看重")):
        heavier = dict(ai, weight=45)
        return {
            "summary": "AI 项目经验权重由 20 提高到 45，排序会更看重这一项",
            "patches": [
                {
                    "op": "modify",
                    "kind": "bonus",
                    "target_id": "ai_exp",
                    "before": ai,
                    "after": heavier,
                    "reason": "该岗位的核心产出依赖 AI 项目实操，权重需要与重要性匹配",
                }
            ],
        }

    # 默认：新增一个维度 → 旧结论推不出新维度，属于"算不准"路径
    return {
        "summary": "新增一个加分项：有招聘系统或筛选工具搭建经验",
        "patches": [
            {
                "op": "add",
                "kind": "bonus",
                "target_id": "recruit_tool",
                "before": None,
                "after": {
                    "id": "recruit_tool",
                    "name": "招聘系统搭建经验",
                    "definition": "亲手搭建或深度参与过简历筛选、人才库等招聘侧系统，"
                    "含规则引擎、排序策略、评估流程。只是使用现成系统不算。",
                    "positive_examples": ["搭过简历筛选流程", "设计过候选人排序策略"],
                    "negative_examples": ["仅使用招聘网站后台"],
                    "weight": 25,
                },
                "reason": "该岗位需要自己落地筛选工具，有相关经验上手更快",
            }
        ],
    }


# ——————————————————————— 数据播种 ———————————————————————


def write_resume_pdf(path: Path, candidate: dict) -> Path:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    y = 60.0
    for line in candidate["lines"]:
        page.insert_text((50, y), line, fontname="china-s", fontsize=10)
        y += 26
    doc.save(str(path))
    doc.close()
    return path


def seed(data_dir: Path) -> tuple[str, list[Path]]:
    """建一个岗位 + 一份标准，并在磁盘上生成三份合成简历。"""
    db = Database(data_dir).init_schema()
    job = jobs_service.create_job(
        db,
        title="AI 应用开发工程师",
        jd_text="负责 RAG 与 Agent 应用落地；需要能独立设计并交付 AI 项目。",
    )
    jobs_service.set_rubric(db, job.job_id, Rubric.model_validate(RUBRIC), note="演示初始标准")

    pdf_dir = data_dir / "demo_resumes"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    return job.job_id, [write_resume_pdf(pdf_dir / c["file"], c) for c in CANDIDATES]


def upload_all(base: str, job_id: str, pdfs: list[Path], *, timeout: float = 60.0) -> None:
    """走真实上传接口，而不是直接写库——验收要验的就是这条链路。"""
    with httpx.Client(base_url=base, timeout=30.0) as client:
        files = [("files", (p.name, p.read_bytes(), "application/pdf")) for p in pdfs]
        resp = client.post(f"/api/jobs/{job_id}/resumes", files=files)
        resp.raise_for_status()
        print(f"已上传 {resp.json()['accepted']} 份简历")

        deadline = time.time() + timeout
        while time.time() < deadline:
            ranking = client.get(f"/api/jobs/{job_id}/ranking").json()
            if ranking["pending"] == 0:
                groups = "、".join(
                    f"{g['label']} {g['count']}" for g in ranking["groups"] if g["count"]
                )
                print(f"处理完成：{groups}（共 {ranking['total']} 份）")
                return
            time.sleep(0.15)
    raise SystemExit("简历处理超时，未能在预期时间内出结论")


# ——————————————————————— 入口 ———————————————————————


def run_smoke(
    target: str,
    *,
    expect_rows: int,
    chat: str | None,
    apply: bool,
    expect_refused: bool,
    export: bool = False,
) -> int:
    import os
    import subprocess

    node = os.environ.get("NODE_BIN") or "node"
    env = {
        **os.environ,
        "TARGET": target,
        "OUT": str(REPO_ROOT / "web" / "e2e" / "ui-smoke.png"),
        "EXPECT_ROWS": str(expect_rows),
    }
    if chat:
        env["CHAT"] = chat
        if apply:
            env["APPLY"] = "1"
    if expect_refused:
        env["EXPECT_REFUSED"] = "1"
    if export:
        env["EXPORT"] = "1"
    print(f"运行浏览器冒烟：node web/e2e/ui-smoke.mjs（TARGET={target}）")
    proc = subprocess.run(
        [node, str(REPO_ROOT / "web" / "e2e" / "ui-smoke.mjs")],
        cwd=str(REPO_ROOT),
        env=env,
    )
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="离线演示服务器（脚本化模型，不联网）")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--data-dir", default="", help="留空则用临时目录（退出即丢）")
    parser.add_argument("--smoke", action="store_true", help="跑完浏览器冒烟后退出")
    parser.add_argument("--chat", default="学历不卡了", help="冒烟时提交的那句话")
    parser.add_argument(
        "--no-apply", action="store_true", help="只提交到「变更清单 + 影响」，不点确认生效"
    )
    parser.add_argument(
        "--expect-refused", action="store_true", help="要求这次改动被合规拒绝（验证红线拦得住）"
    )
    parser.add_argument("--no-export", action="store_true", help="冒烟时不点击「导出 Excel」")
    parser.add_argument("--open", action="store_true", help="自动打开浏览器")
    args = parser.parse_args()

    data_dir = Path(args.data_dir) if args.data_dir else Path(tempfile.mkdtemp(prefix="demo-"))
    data_dir.mkdir(parents=True, exist_ok=True)

    llm = DemoLLM()
    app = create_app(data_dir=data_dir, llm=llm, parser=PdfPymupdfParser())
    job_id, pdfs = seed(data_dir)

    base = f"http://{args.host}:{args.port}"
    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 30
    while time.time() < deadline and not server.started:
        time.sleep(0.1)
    if not server.started:
        print("服务未能在 30 秒内启动", file=sys.stderr)
        return 1

    print(f"工作台已启动：{base}/")
    print(f"数据目录：{data_dir}")
    try:
        upload_all(base, job_id, pdfs)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        server.should_exit = True
        return 1

    if args.smoke:
        code = run_smoke(
            base + "/",
            expect_rows=len(CANDIDATES),
            chat=args.chat,
            apply=not args.no_apply,
            expect_refused=args.expect_refused,
            export=not args.no_export,
        )
        server.should_exit = True
        thread.join(timeout=10)
        print("浏览器冒烟通过" if code == 0 else f"浏览器冒烟失败（退出码 {code}）")
        return code

    if args.open:
        webbrowser.open(base + "/")
    print("按 Ctrl+C 停止。")
    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

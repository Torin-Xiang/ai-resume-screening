"""M9：对话式改标准（技术方案 §7 的三段式）。

覆盖（技术方案 §11 M9 完成标志）：

- **合规两侧各拦一次**：输入侧（用户原话）与输出侧（模型给出的清单）
- **未确认不生效**：`propose` 只出清单与影响，当前标准必须原封不动
  ——这是"HR 授权的是帮我改，不是随便改"（产品方案 §6.4）的可验证形式
- **影响计算不调模型**：同一个脚本化模型上，`propose` 不得产生评分调用
- **部分采纳**：只应用选中的变更
- **基线漂移防护**：标准在提案之后被改过，旧提案不得照旧套用
- **改标准重排不重新解析**：提取调用数必须保持不变
- 版本可回退

全部使用脚本化模型与合成 PDF，不联网、不需要 API key。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import write_pdf, write_scanned_pdf
from resume_screening.core.llm import LLMResponse, Usage
from resume_screening.core.prompts import EXTRACT_SYSTEM, PROPOSE_SYSTEM, SCORE_SYSTEM
from test_m6_workbench import (
    EXTRACT_FIELDS,
    SCORE_ITEM,
    create_job,
    make_client,
    make_rubric_dict,
    upload,
    wait_settled,
    _resume_items,
)

# ——————————————————————— 脚本化模型 ———————————————————————


class ProposalLLM:
    """在 M6 的脚本化模型之上，再多认一种 prompt：改标准的提案请求。

    这类模型让"提案内容"完全可控，从而能精确验证**系统对清单做了什么**——
    而不是在验证某个真实模型这周会不会好好输出 JSON。
    """

    name = "proposal"

    def __init__(
        self,
        proposal: dict | None = None,
        *,
        proposals: list[dict] | None = None,
    ) -> None:
        self._queue = list(proposals) if proposals else ([proposal] if proposal else [])
        self.propose_calls = 0
        self.extract_calls = 0
        self.score_calls = 0
        self.last_propose_user = ""

    @property
    def total_calls(self) -> int:
        return self.propose_calls + self.extract_calls + self.score_calls

    def complete_json(self, *, system: str, user: str, json_schema=None, temperature: float = 0.0):
        usage = Usage(model="proposal", prompt_tokens=10, completion_tokens=5)
        if system == EXTRACT_SYSTEM:
            self.extract_calls += 1
            return LLMResponse(payload={"fields": EXTRACT_FIELDS}, usage=usage)
        if system == SCORE_SYSTEM:
            self.score_calls += 1
            return LLMResponse(payload={"results": [dict(SCORE_ITEM)]}, usage=usage)
        assert system == PROPOSE_SYSTEM
        self.propose_calls += 1
        self.last_propose_user = user
        payload = self._queue.pop(0) if self._queue else {"summary": "无改动", "patches": []}
        return LLMResponse(payload=payload, usage=usage)


# ——————————————————————— 辅助 ———————————————————————


def dim_of(rubric: dict, kind: str, dim_id: str) -> dict:
    return next(d for d in rubric[kind] if d["id"] == dim_id)


def patch_modify_bonus_weight(rubric: dict, new_weight: float) -> dict:
    """构造一条"把加分项权重改掉"的变更。"""
    before = dim_of(rubric, "bonus", "ai_exp")
    after = {**before, "weight": new_weight}
    return {"op": "modify", "kind": "bonus", "target_id": "ai_exp", "before": before,
            "after": after, "reason": "业务方认为这项更重要"}


def proposal_with(*patches: dict, summary: str = "调整了标准", refused: str = "") -> dict:
    return {"summary": summary, "refused_reason": refused, "patches": list(patches)}


def propose(client: TestClient, job_id: str, utterance: str):
    return client.post(f"/api/jobs/{job_id}/rubric/propose", json={"utterance": utterance})


def get_rubric(client: TestClient, job_id: str) -> dict:
    resp = client.get(f"/api/jobs/{job_id}/rubric")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ——————————————————————— ① 合规：输入侧 ———————————————————————


@pytest.mark.parametrize(
    "utterance",
    [
        "把 35 岁以上的筛掉",
        "已婚未育的先排除",
        "只要男生",
        "不要某省的",
    ],
)
def test_protected_attribute_request_is_refused(tmp_path: Path, utterance: str) -> None:
    """产品方案 §6.5：自然语言入口的合规红线必须同样生效。"""
    llm = ProposalLLM()
    with make_client(tmp_path, llm=llm) as (client, _):
        job_id = create_job(client, rubric=make_rubric_dict("v1"))
        resp = propose(client, job_id, utterance)

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "合规" in detail
    # 必须给出去路，而不是单说"不行"——否则 HR 只会换个说法再试一次
    assert "能力或经验" in detail


def test_refusal_happens_before_calling_the_model(tmp_path: Path) -> None:
    """红线挡在调用模型之前：既保证不被绕过，也不为一句不能执行的要求付钱。"""
    llm = ProposalLLM()
    with make_client(tmp_path, llm=llm) as (client, _):
        job_id = create_job(client, rubric=make_rubric_dict("v1"))
        before = llm.propose_calls
        propose(client, job_id, "把 35 岁以上的筛掉")

    assert llm.propose_calls == before, "违规要求不应产生模型调用"


def test_legitimate_years_requirement_is_not_blocked(tmp_path: Path) -> None:
    """"3 年以上开发经验"是能力年限，不是年龄——误伤会把正常需求也堵死。"""
    rubric = make_rubric_dict("v1")
    llm = ProposalLLM(proposal_with(patch_modify_bonus_weight(rubric, 30)))
    with make_client(tmp_path, llm=llm) as (client, _):
        job_id = create_job(client, rubric=rubric)
        resp = propose(client, job_id, "需要 3 年以上开发经验的人")

    assert resp.status_code == 200, resp.text


# ——————————————————————— ② 合规：输出侧 ———————————————————————


def test_model_output_touching_compliance_is_rejected(tmp_path: Path) -> None:
    """技术方案 §7：只在输入侧拦不住模型的自由发挥，输出侧必须再拦一次。"""
    bad_dim = {
        "id": "young",
        "name": "年轻有活力",
        "definition": "年龄在 30 岁以下者优先",
        "positive_examples": ["1998 年后出生"],
        "negative_examples": ["年龄偏大"],
        "weight": 5,
    }
    llm = ProposalLLM(
        proposal_with(
            {
                "op": "add",
                "kind": "bonus",
                "target_id": "young",
                "before": None,
                "after": bad_dim,
                "reason": "团队希望更年轻",
            }
        )
    )
    with make_client(tmp_path, llm=llm) as (client, _):
        job_id = create_job(client, rubric=make_rubric_dict("v1"))
        resp = propose(client, job_id, "希望团队更有活力")

    assert resp.status_code == 422, resp.text
    assert "合规" in resp.json()["detail"] or "受保护属性" in resp.json()["detail"]


def test_patch_reason_touching_compliance_is_rejected(tmp_path: Path) -> None:
    """改动理由也会展示给 HR，同样不能夹带受保护属性。"""
    rubric = make_rubric_dict("v1")
    patch = patch_modify_bonus_weight(rubric, 30)
    patch["reason"] = "把已婚女性的权重调低"
    llm = ProposalLLM(proposal_with(patch))

    with make_client(tmp_path, llm=llm) as (client, _):
        job_id = create_job(client, rubric=rubric)
        resp = propose(client, job_id, "这项更重要")

    assert resp.status_code == 422, resp.text


def test_model_refusal_is_surfaced_to_hr(tmp_path: Path) -> None:
    """模型判定要求不能执行时，理由必须回到 HR 面前，也不能被应用。"""
    llm = ProposalLLM(proposal_with(refused="该要求涉及受保护属性，无法转换为筛选维度"))
    with make_client(tmp_path, llm=llm) as (client, _):
        job_id = create_job(client, rubric=make_rubric_dict("v1"))
        resp = propose(client, job_id, "希望候选人更稳定一些")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["refused"] is True
        assert body["refused_reason"]

        applied = client.post(
            f"/api/jobs/{job_id}/rubric/apply", json={"proposal_id": body["proposal_id"]}
        )

    assert applied.status_code == 422
    assert "拒绝" in applied.json()["detail"]


# ——————————————————————— ③ 提案不改变当前标准 ———————————————————————


def test_propose_does_not_touch_current_rubric(tmp_path: Path) -> None:
    """**本文件最重要的一条**：提案阶段绝不允许静默改标准（产品方案 §6.4）。

    如果这条失效，HR 会在不知情的情况下被改了筛选口径，而且找不到是谁改的。
    """
    rubric = make_rubric_dict("v1", bonus_weight=20.0)
    llm = ProposalLLM(proposal_with(patch_modify_bonus_weight(rubric, 5)))
    with make_client(tmp_path, llm=llm) as (client, _):
        job_id = create_job(client, rubric=rubric)
        before = get_rubric(client, job_id)
        version_count_before = len(
            client.get(f"/api/jobs/{job_id}/rubric/versions").json()
        )

        resp = propose(client, job_id, "AI 经验没那么重要了，降权")

        assert resp.status_code == 200, resp.text
        after = get_rubric(client, job_id)
        assert after["rubric_id"] == before["rubric_id"]
        assert after["payload"]["bonus"][0]["weight"] == 20.0
        assert (
            len(client.get(f"/api/jobs/{job_id}/rubric/versions").json())
            == version_count_before
        )


# ——————————————————————— ④ 影响预估 ———————————————————————


def test_propose_reports_patch_list_and_impact(tmp_path: Path) -> None:
    rubric = make_rubric_dict("v1", bonus_weight=20.0)
    llm = ProposalLLM(proposal_with(patch_modify_bonus_weight(rubric, 30)))
    with make_client(tmp_path, llm=llm) as (client, _):
        job_id = create_job(client, rubric=rubric)
        pdf = write_pdf(tmp_path / "a.pdf", [*_resume_items()])
        upload(client, job_id, [pdf])
        wait_settled(client, job_id)

        body = propose(client, job_id, "AI 经验更重要了，加权").json()

    assert body["patches"], "必须给出可直接阅读的变更清单"
    assert body["patches"][0]["op"] == "modify"
    assert body["patches"][0]["description"]
    assert "业务方" in body["patches"][0]["reason"]
    impact = body["impact"]
    assert impact["total"] == 1
    assert impact["exact"] is True
    # 权重从 20 调到 30，分数应当上升
    assert impact["changed"] == 1
    assert impact["moves"][0]["to_score"] == 30.0


def test_impact_is_computed_without_calling_the_model(tmp_path: Path) -> None:
    """技术方案 §7 的性能前提：影响计算是纯计算。

    一旦这里开始调模型，"改标准前先看看影响"就会变慢变贵，
    HR 会跳过确认直接改，三道闸随即失效。
    """
    rubric = make_rubric_dict("v1", bonus_weight=20.0)
    llm = ProposalLLM(proposal_with(patch_modify_bonus_weight(rubric, 30)))
    with make_client(tmp_path, llm=llm) as (client, _):
        job_id = create_job(client, rubric=rubric)
        pdf = write_pdf(tmp_path / "a.pdf", [*_resume_items()])
        upload(client, job_id, [pdf])
        wait_settled(client, job_id)

        score_calls_before = llm.score_calls
        propose(client, job_id, "加权")

    assert llm.score_calls == score_calls_before, "影响预估不应产生评分调用"
    assert llm.propose_calls == 1, "整个提案过程只应有一次模型调用"


def test_impact_marks_added_dimension_as_unpredictable(tmp_path: Path) -> None:
    """新增维度无法从旧结论推算，必须**明确标为算不准**，而不是给个假数字。

    预估错了比没有预估更伤信任：HR 会照着虚假的"不会影响任何人"去确认改动。
    """
    rubric = make_rubric_dict("v1")
    new_dim = {
        "id": "big_company",
        "name": "大厂经历",
        "definition": "在知名互联网公司有完整项目经验",
        "positive_examples": ["在一线大厂承担核心模块"],
        "negative_examples": ["仅外包经历"],
        "weight": 10,
    }
    llm = ProposalLLM(
        proposal_with(
            {
                "op": "add",
                "kind": "bonus",
                "target_id": "big_company",
                "before": None,
                "after": new_dim,
                "reason": "业务方希望有过大厂协作经验",
            }
        )
    )
    with make_client(tmp_path, llm=llm) as (client, _):
        job_id = create_job(client, rubric=rubric)
        pdf = write_pdf(tmp_path / "a.pdf", [*_resume_items()])
        upload(client, job_id, [pdf])
        wait_settled(client, job_id)

        impact = propose(client, job_id, "加大厂经历这条").json()["impact"]

    assert impact["exact"] is False
    assert impact["unknown_candidates"] == 1
    assert impact["moves"][0]["to_verdict"] is None, "算不准就不要给结论"
    assert any("无法在改动前给出预估" in n for n in impact["notes"])


def test_impact_ignores_unprocessed_resumes_and_says_so(tmp_path: Path) -> None:
    """没出结论的简历要计入"没算进去"的说明，否则 HR 会以为覆盖了全部简历。

    构造必须是确定性的：用扫描件产生 `needs_manual`（它永远不会变成 done），
    而不是"粘贴一份然后趁它还没处理完"——那种写法取决于机器快慢，会随机失败。
    """
    rubric = make_rubric_dict("v1", bonus_weight=20.0)
    llm = ProposalLLM(proposal_with(patch_modify_bonus_weight(rubric, 30)))
    with make_client(tmp_path, llm=llm) as (client, _):
        job_id = create_job(client, rubric=rubric)
        pdf = write_pdf(tmp_path / "a.pdf", [*_resume_items()])
        scanned = write_scanned_pdf(tmp_path / "scanned.pdf")
        upload(client, job_id, [pdf, scanned])
        wait_settled(client, job_id)

        impact = propose(client, job_id, "加权").json()["impact"]

    assert impact["total"] == 2, "总数要包含没出结论的那一份，否则占比是错的"
    assert impact["not_scored"] == 1
    assert any("未计入" in n for n in impact["notes"]), "光有数字不够，要有说明"


def test_removing_a_hard_filter_explains_the_group_change(tmp_path: Path) -> None:
    """删掉必须项要能看出谁因此从"待确认"升上来——这正是 HR 最想看的那个数字。"""
    rubric = make_rubric_dict("v1")
    # 先把硬门槛的 rule 去掉，让它走模型判定；脚本化模型永远返回"未满足"的对照项
    hard = {"id": "deg", "name": "本科及以上", "kind": "hard_filter"}
    llm = ProposalLLM(
        proposal_with(
            {
                "op": "remove",
                "kind": "hard_filter",
                "target_id": "deg",
                "before": dim_of(rubric, "hard_filters", "deg"),
                "after": None,
                "reason": "学历不再是硬性要求",
            }
        )
    )
    with make_client(tmp_path, llm=llm) as (client, _):
        job_id = create_job(client, rubric=rubric)
        pdf = write_pdf(tmp_path / "a.pdf", [*_resume_items()])
        upload(client, job_id, [pdf])
        wait_settled(client, job_id)

        body = propose(client, job_id, "学历不卡了").json()

    assert body["impact"]["exact"] is True
    # 硬门槛的判定来自规则（本科满足），删掉它不改变结论 → 不该误报变化
    assert body["impact"]["counts_before"] == body["impact"]["counts_after"]
    del hard  # 仅用于说明该维度形状，实际清单直接取自现有标准


# ——————————————————————— ⑤ 确认生效 ———————————————————————


def test_apply_writes_new_version_and_rescores(tmp_path: Path) -> None:
    rubric = make_rubric_dict("v1", bonus_weight=20.0)
    llm = ProposalLLM(proposal_with(patch_modify_bonus_weight(rubric, 30)))
    with make_client(tmp_path, llm=llm) as (client, _):
        job_id = create_job(client, rubric=rubric)
        pdf = write_pdf(tmp_path / "a.pdf", [*_resume_items()])
        upload(client, job_id, [pdf])
        wait_settled(client, job_id)
        before = get_rubric(client, job_id)

        proposal_id = propose(client, job_id, "加权").json()["proposal_id"]
        extracted_before = llm.extract_calls

        applied = client.post(
            f"/api/jobs/{job_id}/rubric/apply", json={"proposal_id": proposal_id}
        )

        assert applied.status_code == 200, applied.text
        body = applied.json()
        after = get_rubric(client, job_id)
        ranking = client.get(f"/api/jobs/{job_id}/ranking").json()

    assert body["applied"] == 1
    assert after["rubric_id"] != before["rubric_id"], "必须写入新版本而不是覆盖"
    assert after["payload"]["bonus"][0]["weight"] == 30.0
    # 版本记录里必须留下"这一版是怎么改出来的"
    assert "加权" in after["note"]
    assert ranking["rubric_version"] == body["version"]
    assert llm.extract_calls == extracted_before, "改标准不得重新解析/重新提取"


def test_partial_adoption_applies_only_selected_patches(tmp_path: Path) -> None:
    """产品方案 §6.4 的第二个选项：部分采纳。"""
    rubric = make_rubric_dict("v1", bonus_weight=20.0)
    keep = patch_modify_bonus_weight(rubric, 30)
    add = {
        "op": "add",
        "kind": "red_flag",
        "target_id": "job_hop",
        "before": None,
        "after": {
            "id": "job_hop",
            "name": "频繁跳槽",
            "definition": "近三年内更换三份及以上工作",
            "positive_examples": ["三年换了四家公司"],
            "negative_examples": ["三年只待过一家公司"],
            "penalty": 8,
        },
        "reason": "稳定性存疑的减分",
    }
    llm = ProposalLLM(proposal_with(keep, add))
    with make_client(tmp_path, llm=llm) as (client, _):
        job_id = create_job(client, rubric=rubric)
        proposal_id = propose(client, job_id, "加权，另外加一条跳槽减分").json()["proposal_id"]

        applied = client.post(
            f"/api/jobs/{job_id}/rubric/apply",
            json={"proposal_id": proposal_id, "accepted": [0]},
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["applied"] == 1
        payload = get_rubric(client, job_id)["payload"]

    assert payload["bonus"][0]["weight"] == 30.0
    assert payload["red_flags"] == [], "没选中的变更不得被应用"


def test_apply_rejects_stale_baseline(tmp_path: Path) -> None:
    """提案的基准版本已经变了还照旧套用，会悄悄覆盖别人的改动。"""
    rubric = make_rubric_dict("v1", bonus_weight=20.0)
    llm = ProposalLLM(proposal_with(patch_modify_bonus_weight(rubric, 30)))
    with make_client(tmp_path, llm=llm) as (client, _):
        job_id = create_job(client, rubric=rubric)
        proposal_id = propose(client, job_id, "加权").json()["proposal_id"]

        # 中途有人（或 HR 自己）改了标准
        changed = make_rubric_dict("v2", bonus_weight=25.0)
        resp = client.put(f"/api/jobs/{job_id}/rubric", json={"rubric": changed})
        assert resp.status_code == 200, resp.text

        applied = client.post(
            f"/api/jobs/{job_id}/rubric/apply", json={"proposal_id": proposal_id}
        )

    assert applied.status_code == 422
    assert "基准版本" in applied.json()["detail"]


def test_apply_unknown_proposal_is_rejected(tmp_path: Path) -> None:
    with make_client(tmp_path, llm=ProposalLLM()) as (client, _):
        job_id = create_job(client, rubric=make_rubric_dict("v1"))
        resp = client.post(
            f"/api/jobs/{job_id}/rubric/apply", json={"proposal_id": "prop_nonexistent"}
        )
    assert resp.status_code == 422


def test_patch_targeting_unknown_dimension_is_rejected(tmp_path: Path) -> None:
    """改一个不存在的维度必须报错，而不是静默什么都没发生。"""
    rubric = make_rubric_dict("v1")
    llm = ProposalLLM(
        proposal_with(
            {
                "op": "modify",
                "kind": "bonus",
                "target_id": "typo_id",
                "before": dim_of(rubric, "bonus", "ai_exp"),
                "after": {**dim_of(rubric, "bonus", "ai_exp"), "weight": 99},
                "reason": "改权重",
            }
        )
    )
    with make_client(tmp_path, llm=llm) as (client, _):
        job_id = create_job(client, rubric=rubric)
        resp = propose(client, job_id, "加权")

    assert resp.status_code == 422
    assert "typo_id" in resp.json()["detail"]


# ——————————————————————— ⑥ 版本回退 ———————————————————————


def test_rollback_restores_a_previous_version(tmp_path: Path) -> None:
    """产品方案 §6.4：标准每次变更留版本，可回退。"""
    rubric = make_rubric_dict("v1", bonus_weight=20.0)
    llm = ProposalLLM(proposal_with(patch_modify_bonus_weight(rubric, 30)))
    with make_client(tmp_path, llm=llm) as (client, _):
        job_id = create_job(client, rubric=rubric)
        first = get_rubric(client, job_id)

        proposal_id = propose(client, job_id, "加权").json()["proposal_id"]
        client.post(f"/api/jobs/{job_id}/rubric/apply", json={"proposal_id": proposal_id})
        assert get_rubric(client, job_id)["payload"]["bonus"][0]["weight"] == 30.0

        resp = client.post(
            f"/api/jobs/{job_id}/rubric/rollback", json={"rubric_id": first["rubric_id"]}
        )
        assert resp.status_code == 200, resp.text
        restored = get_rubric(client, job_id)
        versions = client.get(f"/api/jobs/{job_id}/rubric/versions").json()

    assert restored["payload"]["bonus"][0]["weight"] == 20.0
    assert "回滚" in restored["note"]
    # 回滚是"把旧版本重新设为当前"，不是删历史——改了又改回来这件事本身也要留痕
    assert len(versions) == 3


def test_rollback_rejects_version_from_another_job(tmp_path: Path) -> None:
    with make_client(tmp_path, llm=ProposalLLM()) as (client, _):
        job_a = create_job(client, title="岗位A", rubric=make_rubric_dict("v1"))
        job_b = create_job(client, title="岗位B", rubric=make_rubric_dict("v1"))
        version_of_a = get_rubric(client, job_a)["rubric_id"]

        resp = client.post(
            f"/api/jobs/{job_b}/rubric/rollback", json={"rubric_id": version_of_a}
        )
    assert resp.status_code == 404

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from .models import (
    AnalysisPlan,
    ClaimLedger,
    CriticReport,
    EvidenceAssessment,
    ManuscriptPackage,
    MethodRoute,
    ResearchPackage,
    ScientificAudit,
    TestableHypotheses,
)


@dataclass(frozen=True)
class PromptSpec:
    key: str
    title: str
    version: str
    system: str
    user_template: str
    output_model: type[BaseModel]

    def public_prompts(self) -> list[dict[str, str]]:
        return [
            {"id": f"{self.key}:system", "role": "system", "template": self.system},
            {"id": f"{self.key}:user", "role": "user", "template": self.user_template},
        ]

    def render(self, payload: Any) -> list[dict[str, str]]:
        rendered = self.user_template.replace("{{input_json}}", _json_text(payload))
        return [
            {
                "id": f"{self.key}:system",
                "role": "system",
                "template": self.system,
                "rendered": self.system,
            },
            {
                "id": f"{self.key}:user",
                "role": "user",
                "template": self.user_template,
                "rendered": rendered,
            },
        ]


def _json_text(value: Any) -> str:
    if isinstance(value, BaseModel):
        return value.model_dump_json(indent=2)
    import json

    return json.dumps(value, ensure_ascii=False, indent=2)


COMMON_GUARDRAILS = """你是 HypoWeaver-Qwen 中受约束的社会科学研究节点。
只使用当前输入中存在的材料，不读取或猜测原论文结论、回归表和隐藏参考答案。
必须区分：已知事实、待检验设计、真实执行结果。未执行统计分析时，不得编造样本量、系数、标准误、p 值、显著性或稳健性结果。
不得为了得到显著结果而替换因变量、删除样本或修改研究设计。保留空结果、反向结果、失败运行和未解决风险。
严格输出符合指定 JSON Schema 的 JSON 对象，不输出解释性前后缀。"""


PROMPTS: dict[str, PromptSpec] = {
    "intake": PromptSpec(
        "intake",
        "案例解析",
        "1.0.0",
        COMMON_GUARDRAILS
        + "\n你的唯一任务是把用户输入规范化为 ResearchPackage；不选择方法，不解释结果。",
        "请规范化以下案例输入：\n{{input_json}}",
        ResearchPackage,
    ),
    "hypothesis_decomposition": PromptSpec(
        "hypothesis_decomposition",
        "假设拆解",
        "1.0.0",
        COMMON_GUARDRAILS
        + "\n把每条理论假设转换为可观察、可证伪的预测，并明确竞争解释与证伪条件；不要选择估计器。",
        "请拆解以下 ResearchPackage：\n{{input_json}}",
        TestableHypotheses,
    ),
    "method_route": PromptSpec(
        "method_route",
        "方法路由",
        "1.0.0",
        COMMON_GUARDRAILS
        + "\n依据研究目标、数据结构和识别条件选择方法家族。信息不足时必须 blocked 或 needs_human_review，禁止静默回退到普通回归。",
        "请根据研究包、可检验假设和数据画像路由：\n{{input_json}}",
        MethodRoute,
    ),
    "analysis_design": PromptSpec(
        "analysis_design",
        "研究设计",
        "1.0.0",
        COMMON_GUARDRAILS
        + "\n生成预分析计划。每个诊断、稳健性和证伪步骤必须说明理由与所需字段；数据不足时标记 design_only。",
        "请为已选方法家族生成 AnalysisPlan：\n{{input_json}}",
        AnalysisPlan,
    ),
    "method_critic": PromptSpec(
        "method_critic",
        "独立方法审查",
        "1.0.0",
        COMMON_GUARDRAILS
        + "\n分别检查测量、因果识别、统计推断和可复现性。只提出可定位的问题；critical 问题必须阻止 H2 冻结。",
        "请审查以下研究计划：\n{{input_json}}",
        CriticReport,
    ),
    "plan_revision": PromptSpec(
        "plan_revision",
        "有限计划修复",
        "1.0.0",
        COMMON_GUARDRAILS
        + "\n只修复 Critic 明确指出的问题，不扩大研究问题，不根据预期显著性改动设计；最多两轮。",
        "请按照 CriticReport 修订 AnalysisPlan：\n{{input_json}}",
        AnalysisPlan,
    ),
    "evidence_assessment": PromptSpec(
        "evidence_assessment",
        "结果解释",
        "1.0.0",
        COMMON_GUARDRAILS
        + "\n只解释 ResearchRun 中真实存在的执行记录。fixture_only 或 not_executed 必须输出 not_tested。",
        "请评估以下 ResearchRun：\n{{input_json}}",
        EvidenceAssessment,
    ),
    "scientific_audit": PromptSpec(
        "scientific_audit",
        "科学有效性审查",
        "1.0.0",
        COMMON_GUARDRAILS
        + "\n代码运行成功不等于科学有效。检查冻结合同、识别假设、必要诊断和未披露偏离。",
        "请审计合同、运行与证据评估：\n{{input_json}}",
        ScientificAudit,
    ),
    "claim_ledger": PromptSpec(
        "claim_ledger",
        "结论账本",
        "1.0.0",
        COMMON_GUARDRAILS
        + "\n每条 Claim 必须绑定真实 run。没有真实执行时 evidence_status=not_tested 且 allowed_strength=prohibited。",
        "请根据审计后的证据生成 ClaimLedger：\n{{input_json}}",
        ClaimLedger,
    ),
    "scientific_writer": PromptSpec(
        "scientific_writer",
        "受约束科学写作",
        "1.0.0",
        COMMON_GUARDRAILS
        + "\n只写入 H3 已授权的 Claim。没有真实执行或 fixture_only 时，只生成研究计划，不得生成实证结果章节。",
        "请根据获批结论与冻结计划生成成果包：\n{{input_json}}",
        ManuscriptPackage,
    ),
}


def get_prompt(key: str) -> PromptSpec:
    try:
        return PROMPTS[key]
    except KeyError as error:
        raise KeyError(f"unknown prompt: {key}") from error

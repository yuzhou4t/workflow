from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .models import (
    AnalysisPlan,
    CaseSubmission,
    ClaimLedger,
    CriticReport,
    DataProfile,
    EvidenceAssessment,
    FormalResearchContract,
    GateDecisionRequest,
    ManuscriptPackage,
    MethodRoute,
    ResearchPackage,
    ResearchRun,
    ScientificAudit,
    TestableHypotheses,
)
from .prompts import get_prompt


DEFINITION_VERSION = "1.1.0"


def _schema(model: type[BaseModel] | None) -> dict[str, Any] | None:
    return model.model_json_schema() if model else None


def _node(
    node_id: str,
    title: str,
    node_type: str,
    stage_id: str,
    description: str,
    x: int,
    y: int,
    *,
    input_model: type[BaseModel] | None = None,
    output_model: type[BaseModel] | None = None,
    prompt_key: str | None = None,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "title": title,
        "type": node_type,
        "stage_id": stage_id,
        "description": description,
        "position": {"x": x, "y": y},
        "prompts": get_prompt(prompt_key).public_prompts() if prompt_key else [],
        "input_schema": _schema(input_model),
        "output_schema": _schema(output_model),
    }


def _edge(source: str, target: str, label: str | None = None) -> dict[str, Any]:
    edge = {"id": f"{source}--{target}", "source": source, "target": target}
    if label:
        edge["label"] = label
    return edge


def build_app_a_definition() -> dict[str, Any]:
    stages = [
        {
            "id": "intake",
            "order": 1,
            "title": "案例接入",
            "description": "标准案例包进入代码运行时，完成规范化、确定性校验与 H1 研究边界确认。",
            "node_ids": ["case_input", "intake_agent", "input_validation", "h1_gate"],
        },
        {
            "id": "understanding",
            "order": 2,
            "title": "研究理解",
            "description": "假设拆解与数据画像并行，汇合后进行方法路由。",
            "node_ids": ["hypothesis_decomposition", "data_profile", "method_route"],
        },
        {
            "id": "design",
            "order": 3,
            "title": "方法设计",
            "description": "七个方法家族互斥路由，仅命中的设计器执行，随后统一为 AnalysisPlan。",
            "node_ids": [
                "design_policy_causal",
                "design_panel_association",
                "design_mechanism_boundary",
                "design_market_event",
                "design_spatial",
                "design_measurement_efficiency",
                "design_structural_macro",
                "analysis_plan_merge",
            ],
        },
        {
            "id": "review",
            "order": 4,
            "title": "独立审查与冻结",
            "description": "四个 Critic 并行审查，最多两轮有限修复；H2 批准后冻结研究合同。",
            "node_ids": [
                "critic_measurement",
                "critic_causal",
                "critic_statistical",
                "critic_reproducibility",
                "critic_merge",
                "plan_revision",
                "h2_gate",
                "contract_freeze",
            ],
        },
        {
            "id": "execution",
            "order": 5,
            "title": "执行边界",
            "description": "Fixture 与外部 Python 执行器互斥；Fixture 永远不得伪造统计结果。",
            "node_ids": ["execution_router", "fixture_executor", "external_executor", "research_run_merge"],
        },
        {
            "id": "audit",
            "order": 6,
            "title": "结果与结论审计",
            "description": "结果解释、科学有效性审查与 ClaimLedger 把执行结果约束成可授权结论。",
            "node_ids": ["evidence_assessment", "scientific_audit", "claim_ledger", "h3_gate"],
        },
        {
            "id": "writing",
            "order": 7,
            "title": "受约束成果生成",
            "description": "Writer 只能读取 H3 授权结论；真实写作失败时保持可重试状态，不用短模板伪装完成。",
            "node_ids": ["scientific_writer", "consistency_audit", "complete"],
        },
    ]

    nodes = [
        _node("case_input", "标准案例包", "start", "intake", "接收预设案例或用户提交的结构化研究包。隐藏参考材料会被 Schema 拒绝。", 80, 160, output_model=CaseSubmission),
        _node("intake_agent", "Research Intake", "code", "intake", "在 H1 前用确定性代码将输入规范化为统一 ResearchPackage，不调用外部模型。", 360, 160, input_model=CaseSubmission, output_model=ResearchPackage),
        _node("input_validation", "确定性输入校验", "code", "intake", "由 Pydantic 与代码规则检查假设、结果变量和泄漏字段，不由模型自行判定。", 640, 160, input_model=ResearchPackage),
        _node("h1_gate", "H1 · 研究边界确认", "gate", "intake", "服务端真正停止，等待批准、退回或拒绝。", 920, 160, input_model=ResearchPackage, output_model=GateDecisionRequest),
        _node("hypothesis_decomposition", "假设拆解", "llm", "understanding", "把理论命题转成可观察预测、竞争解释和证伪条件。", 1220, 80, input_model=ResearchPackage, output_model=TestableHypotheses, prompt_key="hypothesis_decomposition"),
        _node("data_profile", "数据画像", "code", "understanding", "对数据引用做确定性画像；数据尚未接入时明确返回 not_executed。", 1220, 260, input_model=ResearchPackage, output_model=DataProfile),
        _node("method_route", "Method Router", "router", "understanding", "汇合假设与数据画像，禁止在条件不足时静默选择普通回归。", 1520, 160, input_model=DataProfile, output_model=MethodRoute, prompt_key="method_route"),
    ]

    branches = [
        ("policy_causal", "政策因果设计", 1780, 20),
        ("panel_association", "面板关联设计", 1780, 120),
        ("mechanism_boundary", "机制与边界设计", 1780, 220),
        ("market_event", "市场事件设计", 1780, 320),
        ("spatial", "空间计量设计", 1780, 420),
        ("measurement_efficiency", "测度与效率设计", 1780, 520),
        ("structural_macro", "结构宏观设计", 1780, 620),
    ]
    for family, title, x, y in branches:
        nodes.append(
            _node(
                f"design_{family}",
                title,
                "llm",
                "design",
                f"仅当 MethodRoute.primary_route={family} 时执行。",
                x,
                y,
                input_model=MethodRoute,
                output_model=AnalysisPlan,
                prompt_key="analysis_design",
            )
        )
    nodes.extend(
        [
            _node("analysis_plan_merge", "AnalysisPlan 汇合", "merge", "design", "将唯一命中的设计分支规范化为统一 AnalysisPlan。", 2080, 280, input_model=AnalysisPlan, output_model=AnalysisPlan),
            _node("critic_measurement", "测量 Critic", "llm", "review", "独立检查变量定义、层级和测量误差。", 2360, 40, input_model=AnalysisPlan, output_model=CriticReport, prompt_key="method_critic"),
            _node("critic_causal", "因果识别 Critic", "llm", "review", "独立检查识别假设、处理分配和竞争政策。", 2360, 160, input_model=AnalysisPlan, output_model=CriticReport, prompt_key="method_critic"),
            _node("critic_statistical", "统计推断 Critic", "llm", "review", "独立检查估计器、标准误、诊断与多重检验。", 2360, 280, input_model=AnalysisPlan, output_model=CriticReport, prompt_key="method_critic"),
            _node("critic_reproducibility", "复现 Critic", "llm", "review", "独立检查数据版本、样本规则、日志和可复现性。", 2360, 400, input_model=AnalysisPlan, output_model=CriticReport, prompt_key="method_critic"),
            _node("critic_merge", "CriticReport 汇合", "merge", "review", "合并四类问题；critical 未解决时禁止进入 H2。", 2660, 220, input_model=CriticReport, output_model=CriticReport),
            _node("plan_revision", "有限修复（最多 2 轮）", "llm", "review", "只修复 Critic 指定问题，并记录修订轮次和偏离。", 2940, 120, input_model=CriticReport, output_model=AnalysisPlan, prompt_key="plan_revision"),
            _node("h2_gate", "H2 · 冻结分析计划", "gate", "review", "人工确认样本、变量、模型、诊断和停止条件。", 3220, 220, input_model=AnalysisPlan, output_model=GateDecisionRequest),
            _node("contract_freeze", "FormalResearchContract", "code", "review", "对研究包和计划计算哈希并冻结，后续变更必须进入偏离记录。", 3500, 220, input_model=AnalysisPlan, output_model=FormalResearchContract),
            _node("execution_router", "执行器路由", "router", "execution", "根据 Run 模式在 Fixture 与外部 Python 执行器之间互斥选择。", 3780, 220, input_model=FormalResearchContract),
            _node("fixture_executor", "Fixture Executor", "code", "execution", "只验证状态机和接口；输出 fixture_only/not_executed，绝不生成统计量。", 4060, 120, input_model=FormalResearchContract, output_model=ResearchRun),
            _node("external_executor", "Python Research Engine", "http", "execution", "把冻结合同交给独立计量执行服务，并校验返回的 ResearchRun。", 4060, 320, input_model=FormalResearchContract, output_model=ResearchRun),
            _node("research_run_merge", "ResearchRun 汇合", "merge", "execution", "统一两类执行器输出，同时保留 execution_status 与 scientific_status。", 4340, 220, input_model=ResearchRun, output_model=ResearchRun),
            _node("evidence_assessment", "Evidence Assessment", "llm", "audit", "逐条解释真实执行记录；未执行则标记 not_tested。", 4620, 140, input_model=ResearchRun, output_model=EvidenceAssessment, prompt_key="evidence_assessment"),
            _node("scientific_audit", "Scientific Audit", "llm", "audit", "独立判断合同遵从与科学有效性，代码成功不能自动通过。", 4900, 140, input_model=EvidenceAssessment, output_model=ScientificAudit, prompt_key="scientific_audit"),
            _node("claim_ledger", "ClaimLedger", "llm", "audit", "将证据约束为可追溯、可降级、可拒绝的结论。", 5180, 140, input_model=ScientificAudit, output_model=ClaimLedger, prompt_key="claim_ledger"),
            _node("h3_gate", "H3 · 逐条结论授权", "gate", "audit", "人工逐条批准、降级、暂缓或拒绝 Claim；Fixture 只能拒绝或暂缓。", 5460, 140, input_model=ClaimLedger, output_model=GateDecisionRequest),
            _node("scientific_writer", "Scientific Writer", "llm", "writing", "将完整论文拆成 8 个通用分节写作任务；只读取冻结计划、真实运行和 H3 授权结论，失败后可在原 Run 重试。", 5740, 140, input_model=ClaimLedger, output_model=ManuscriptPackage, prompt_key="scientific_writer_section"),
            _node("consistency_audit", "写作一致性审计", "code", "writing", "确定性检查完整章节、未授权 Claim、虚构统计量、Run 引用与成果模式。", 6020, 140, input_model=ManuscriptPackage, output_model=ManuscriptPackage),
            _node("complete", "封存成果包", "end", "writing", "计算封存哈希并结束主 Run；隐藏参考结果仍不可见。", 6300, 140, input_model=ManuscriptPackage),
        ]
    )

    edges = [
        _edge("case_input", "intake_agent"),
        _edge("intake_agent", "input_validation"),
        _edge("input_validation", "h1_gate"),
        _edge("h1_gate", "hypothesis_decomposition", "批准"),
        _edge("h1_gate", "data_profile", "批准"),
        _edge("hypothesis_decomposition", "method_route"),
        _edge("data_profile", "method_route"),
    ]
    for family, *_ in branches:
        edges.append(_edge("method_route", f"design_{family}", family))
        edges.append(_edge(f"design_{family}", "analysis_plan_merge"))
    for critic in ("critic_measurement", "critic_causal", "critic_statistical", "critic_reproducibility"):
        edges.append(_edge("analysis_plan_merge", critic))
        edges.append(_edge(critic, "critic_merge"))
    edges.extend(
        [
            _edge("critic_merge", "plan_revision", "需要修订"),
            _edge("plan_revision", "critic_measurement", "下一轮"),
            _edge("plan_revision", "critic_causal", "下一轮"),
            _edge("plan_revision", "critic_statistical", "下一轮"),
            _edge("plan_revision", "critic_reproducibility", "下一轮"),
            _edge("critic_merge", "h2_gate", "通过"),
            _edge("h2_gate", "contract_freeze", "批准"),
            _edge("contract_freeze", "execution_router"),
            _edge("execution_router", "fixture_executor", "fixture"),
            _edge("execution_router", "external_executor", "external"),
            _edge("fixture_executor", "research_run_merge"),
            _edge("external_executor", "research_run_merge"),
            _edge("research_run_merge", "evidence_assessment"),
            _edge("evidence_assessment", "scientific_audit"),
            _edge("scientific_audit", "claim_ledger"),
            _edge("claim_ledger", "h3_gate"),
            _edge("h3_gate", "scientific_writer", "授权后"),
            _edge("scientific_writer", "consistency_audit"),
            _edge("consistency_audit", "complete"),
        ]
    )

    return {
        "id": "app-a",
        "version": DEFINITION_VERSION,
        "name": "HypoWeaver-Qwen 代码工作流",
        "description": "代码原生、可停止、可恢复的社会科学假设验证链路。Dify YAML 只保留为设计参考，不参与运行。",
        "stages": stages,
        "nodes": nodes,
        "edges": edges,
    }

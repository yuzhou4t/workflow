from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .models import (
    AnalysisPlan,
    CaseSubmission,
    CandidatePlanBatch,
    CandidateDesignSet,
    ClaimLedger,
    CriticReport,
    DataProfile,
    DesignArena,
    DesignReviewerReport,
    EvidenceAssessment,
    EvidenceClaimBundle,
    EvidenceRegistry,
    FormalResearchContract,
    GateDecisionRequest,
    ManuscriptPackage,
    ManuscriptSectionDraftBatch,
    MethodRoute,
    ResearchPackage,
    ResearchRun,
    ReproductionAudit,
    ReviewerReportBatch,
    ScientificAudit,
    TestableHypotheses,
)
from .prompts import get_prompt


DEFINITION_VERSION = "1.6.0"


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
            "description": "七个方法家族互斥路由；两次批量调用在结果不可见时形成三种有边界且执行指纹不同的候选方案，再由代码执行 Probe。",
            "node_ids": [
                "design_policy_causal",
                "design_panel_association",
                "design_mechanism_boundary",
                "design_market_event",
                "design_spatial",
                "design_measurement_efficiency",
                "design_structural_macro",
                "candidate_design_set",
                "probe_run",
            ],
        },
        {
            "id": "review",
            "order": 4,
            "title": "独立审查与冻结",
            "description": "两次网络调用承载四份隔离 Reviewer 报告；各维度独立审查全部候选且不投票决定真理，H2 再从可行候选中选择并冻结。",
            "node_ids": [
                "critic_measurement",
                "critic_causal",
                "critic_statistical",
                "critic_reproducibility",
                "test_dag_compile",
                "design_arena_merge",
                "analysis_plan_merge",
                "plan_revision",
                "h2_gate",
                "design_selection",
                "contract_freeze",
            ],
        },
        {
            "id": "execution",
            "order": 5,
            "title": "执行边界",
            "description": "Fixture 与外部 Python 执行器互斥；企业面板和政策 DID 真实执行后由独立 NumPy 实现重新读取冻结数据并复算。",
            "node_ids": ["execution_router", "fixture_executor", "external_executor", "research_run_merge", "replication_executor", "reproduction_audit"],
        },
        {
            "id": "audit",
            "order": 6,
            "title": "结果与结论审计",
            "description": "一次 Evidence/Claim Bundle 与一次独立科学审计把执行结果交给确定性 Evidence Registry 和 Claim Gate。",
            "node_ids": [
                "evidence_assessment",
                "scientific_audit",
                "claim_ledger",
                "evidence_registry",
                "claim_gate",
                "h3_gate",
            ],
        },
        {
            "id": "writing",
            "order": 7,
            "title": "受约束成果生成",
            "description": "两次 Writer Batch 只读取安全叙述与 statement ID；IR 编译和重读审计通过后仍需 H4 人工批准才能封存。",
            "node_ids": [
                "scientific_writer",
                "manuscript_ir_compile",
                "consistency_audit",
                "h4_gate",
                "complete",
            ],
        },
    ]

    nodes = [
        _node("case_input", "标准案例包", "start", "intake", "接收预设案例或用户提交的结构化研究包。隐藏参考材料会被 Schema 拒绝。", 80, 160, output_model=CaseSubmission),
        _node("intake_agent", "Research Intake", "code", "intake", "在 H1 前用确定性代码将输入规范化为统一 ResearchPackage，不调用外部模型。", 360, 160, input_model=CaseSubmission, output_model=ResearchPackage),
        _node("input_validation", "确定性输入校验", "code", "intake", "由 Pydantic 与代码规则检查假设、结果变量和泄漏字段，不由模型自行判定。", 640, 160, input_model=ResearchPackage),
        _node("h1_gate", "H1 · 研究边界确认", "gate", "intake", "服务端真正停止，等待批准、退回或拒绝。", 920, 160, input_model=ResearchPackage, output_model=GateDecisionRequest),
        _node("hypothesis_decomposition", "假设拆解", "llm", "understanding", "把理论命题转成可观察预测、竞争解释和证伪条件。", 1220, 80, input_model=ResearchPackage, output_model=TestableHypotheses, prompt_key="hypothesis_decomposition"),
        _node("data_profile", "数据画像", "code", "understanding", "对数据引用做确定性画像；数据尚未接入时明确返回 not_executed。", 1220, 260, input_model=ResearchPackage, output_model=DataProfile),
        _node("method_route", "Method Router", "router", "understanding", "由代码汇合假设与数据画像，禁止在条件不足时静默选择普通回归。", 1520, 160, input_model=DataProfile, output_model=MethodRoute),
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
            )
        )
    nodes.extend(
        [
            _node("candidate_design_set", "候选研究设计集", "llm", "design", "用两次批量调用形成三种固定策略候选；代码绑定稳定 ID，并拒绝仅靠说明文字伪造的重复执行指纹。", 2080, 280, input_model=MethodRoute, output_model=CandidatePlanBatch, prompt_key="candidate_plan_batch"),
            _node("probe_run", "ProbeRun", "code", "design", "只检查字段、数据结构、资产、识别条件和执行器能力；禁止读取系数与 p 值。", 2320, 280, input_model=CandidateDesignSet, output_model=CandidateDesignSet),
            _node("critic_measurement", "测量 Reviewer", "llm", "review", "与复现维度共享一次传输上下文，但独立检查全部候选的变量定义、层级和测量误差。", 2560, 40, input_model=CandidateDesignSet, output_model=ReviewerReportBatch, prompt_key="reviewer_report_batch"),
            _node("critic_causal", "因果识别 Reviewer", "llm", "review", "与统计维度共享一次传输上下文，但独立检查全部候选的识别假设与竞争解释。", 2560, 160, input_model=CandidateDesignSet, output_model=ReviewerReportBatch, prompt_key="reviewer_report_batch"),
            _node("critic_statistical", "统计推断 Reviewer", "llm", "review", "与因果维度共享一次传输上下文，但独立检查全部候选的估计器、标准误和诊断。", 2560, 280, input_model=CandidateDesignSet, output_model=ReviewerReportBatch, prompt_key="reviewer_report_batch"),
            _node("critic_reproducibility", "复现 Reviewer", "llm", "review", "与测量维度共享一次传输上下文，但独立检查全部候选的数据版本、样本规则和可复现性。", 2560, 400, input_model=CandidateDesignSet, output_model=ReviewerReportBatch, prompt_key="reviewer_report_batch"),
            _node("test_dag_compile", "方法专属 Test DAG", "code", "review", "使用结构化 threat_id 绑定企业面板或政策 DID 检查、稳定 Claim ID、必做优先级与非可执行占位；不解析 Reviewer 自然语言。", 2740, 220, input_model=CandidateDesignSet, output_model=AnalysisPlan),
            _node("design_arena_merge", "Reviewer Arena 汇合", "merge", "review", "不计算总分或多数票；淘汰 Probe 硬失败、Reviewer reject 或经方法注册表校准后仍必须人工修复的 critical 候选。", 2840, 220, input_model=DesignReviewerReport, output_model=DesignArena),
            _node("analysis_plan_merge", "H2 暂定方案", "merge", "review", "保留全部可行候选，并为 H2 标记一个可更改的暂定方案。", 3080, 220, input_model=DesignArena, output_model=AnalysisPlan),
            _node("plan_revision", "人工修订与复审", "llm", "review", "H2 退回后只修订所选方案，并重新执行结构化审查。", 3320, 80, input_model=CriticReport, output_model=AnalysisPlan, prompt_key="plan_revision"),
            _node("h2_gate", "H2 · 选择并冻结分析计划", "gate", "review", "人工从可行候选中明确选择一个，确认样本、变量、模型、诊断和停止条件。", 3320, 260, input_model=DesignArena, output_model=GateDecisionRequest),
            _node("design_selection", "候选选择记录", "code", "review", "记录 H2 所选 candidate_id，并将对应方案及风险绑定到合同。", 3560, 260, input_model=DesignArena, output_model=AnalysisPlan),
            _node("contract_freeze", "FormalResearchContract", "code", "review", "对研究包和所选计划计算哈希并冻结，后续变更必须进入偏离记录。", 3800, 260, input_model=AnalysisPlan, output_model=FormalResearchContract),
            _node("execution_router", "执行器路由", "router", "execution", "根据 Run 模式在 Fixture 与外部 Python 执行器之间互斥选择。", 3780, 220, input_model=FormalResearchContract),
            _node("fixture_executor", "Fixture Executor", "code", "execution", "只验证状态机和接口；输出 fixture_only/not_executed，绝不生成统计量。", 4060, 120, input_model=FormalResearchContract, output_model=ResearchRun),
            _node("external_executor", "Python Research Engine", "http", "execution", "把冻结合同交给独立计量执行服务，并校验返回的 ResearchRun。", 4060, 320, input_model=FormalResearchContract, output_model=ResearchRun),
            _node("research_run_merge", "ResearchRun 汇合", "merge", "execution", "统一两类执行器输出，同时保留 execution_status 与 scientific_status。", 4340, 220, input_model=ResearchRun, output_model=ResearchRun),
            _node("replication_executor", "独立复现执行", "http", "execution", "企业面板使用 NumPy 双向去均值与企业聚类复算；政策 DID 使用 NumPy 多维组内变换与手工交互聚类复算；空间路径仅标记同实现重跑。", 4500, 320, input_model=FormalResearchContract, output_model=ResearchRun),
            _node("reproduction_audit", "复现一致性审计", "code", "execution", "逐步核对合同、数据哈希、样本流、固定效应、聚类设置、实现身份以及系数和标准误容差；核心不一致即阻塞。", 4620, 220, input_model=ResearchRun, output_model=ReproductionAudit),
            _node("evidence_assessment", "Evidence + Candidate Claims", "llm", "audit", "一次调用同时返回 EvidenceAssessment 与原始 Candidate ClaimLedger；拆分后原始候选账本保持不变。", 4620, 140, input_model=ResearchRun, output_model=EvidenceClaimBundle, prompt_key="evidence_claim_bundle"),
            _node("scientific_audit", "Scientific Audit", "llm", "audit", "独立判断合同遵从与科学有效性，代码成功不能自动通过。", 4900, 140, input_model=EvidenceAssessment, output_model=ScientificAudit, prompt_key="scientific_audit"),
            _node("claim_ledger", "Candidate ClaimLedger", "merge", "audit", "从 Evidence/Claim Bundle 原样拆出 LLM 候选结论；独立审计不得改写，该产物也不直接进入 H3。", 5180, 140, input_model=EvidenceClaimBundle, output_model=ClaimLedger),
            _node("evidence_registry", "Evidence Registry", "code", "audit", "将冻结检查的终态、执行引用、独立复算与审计结果编译为 Claim 级证据。", 5280, 260, input_model=ResearchRun, output_model=EvidenceRegistry),
            _node("claim_gate", "确定性 Claim Gate", "code", "audit", "无 LLM、无随机数、无 I/O；拒绝未知引用与未授权因果表述，输出 H3 唯一可读的 ClaimLedger。", 5380, 260, input_model=EvidenceRegistry, output_model=ClaimLedger),
            _node("h3_gate", "H3 · 逐条结论授权", "gate", "audit", "人工逐条批准、降级、暂缓或拒绝 Claim；Fixture 只能拒绝或暂缓。", 5460, 140, input_model=ClaimLedger, output_model=GateDecisionRequest),
            _node("scientific_writer", "Scientific Writer", "llm", "writing", "用两次批量调用覆盖八章；只读取安全叙述与 statement ID，定向修复只重写未通过章节。", 5740, 140, input_model=ClaimLedger, output_model=ManuscriptSectionDraftBatch, prompt_key="manuscript_section_draft_batch"),
            _node("manuscript_ir_compile", "Manuscript IR 编译", "code", "writing", "从获批 Claim 与成功 Execution 重建语句注册表，解析锚点并按 JSON Pointer 注入受保护值。", 5900, 140, input_model=ManuscriptPackage, output_model=ManuscriptPackage),
            _node("consistency_audit", "写作一致性审计", "code", "writing", "确定性检查完整章节、未授权 Claim、虚构统计量、Run 引用与成果模式。", 6020, 140, input_model=ManuscriptPackage, output_model=ManuscriptPackage),
            _node("h4_gate", "H4 · 最终稿审核", "gate", "writing", "一致性审计通过后仍暂停，等待人工批准、退回重写或拒绝。", 6260, 140, input_model=ManuscriptPackage, output_model=GateDecisionRequest),
            _node("complete", "封存成果包", "end", "writing", "H4 批准后计算封存哈希并结束主 Run；隐藏参考结果仍不可见。", 6500, 140, input_model=ManuscriptPackage),
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
        edges.append(_edge(f"design_{family}", "candidate_design_set"))
    edges.append(_edge("candidate_design_set", "probe_run"))
    for critic in ("critic_measurement", "critic_causal", "critic_statistical", "critic_reproducibility"):
        edges.append(_edge("probe_run", critic))
        edges.append(_edge(critic, "test_dag_compile"))
    edges.extend(
        [
            _edge("test_dag_compile", "design_arena_merge"),
            _edge("design_arena_merge", "analysis_plan_merge", "保留可行候选"),
            _edge("analysis_plan_merge", "h2_gate"),
            _edge("h2_gate", "plan_revision", "退回修订"),
            _edge("plan_revision", "probe_run", "重新审查"),
            _edge("h2_gate", "design_selection", "批准所选候选"),
            _edge("design_selection", "contract_freeze"),
            _edge("contract_freeze", "execution_router"),
            _edge("execution_router", "fixture_executor", "fixture"),
            _edge("execution_router", "external_executor", "external"),
            _edge("fixture_executor", "research_run_merge"),
            _edge("external_executor", "research_run_merge"),
            _edge("research_run_merge", "replication_executor", "真实研究"),
            _edge("replication_executor", "reproduction_audit"),
            _edge("research_run_merge", "reproduction_audit", "Fixture"),
            _edge("reproduction_audit", "evidence_assessment", "通过"),
            _edge("evidence_assessment", "scientific_audit"),
            _edge("scientific_audit", "claim_ledger"),
            _edge("claim_ledger", "evidence_registry"),
            _edge("evidence_registry", "claim_gate"),
            _edge("claim_gate", "h3_gate"),
            _edge("h3_gate", "scientific_writer", "授权后"),
            _edge("scientific_writer", "manuscript_ir_compile"),
            _edge("manuscript_ir_compile", "consistency_audit"),
            _edge("consistency_audit", "h4_gate"),
            _edge("h4_gate", "complete", "批准"),
            _edge("h4_gate", "scientific_writer", "退回重写"),
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

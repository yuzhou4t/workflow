from __future__ import annotations

import json
from typing import Any, Protocol, TypeVar
from uuid import uuid4

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from .models import (
    AnalysisPlan,
    CaseSubmission,
    ClaimLedger,
    ClaimRecord,
    CriticReport,
    DataProfile,
    EvidenceAssessment,
    ExecutionRecord,
    FormalResearchContract,
    ManuscriptPackage,
    ManuscriptSection,
    MethodRoute,
    ModelSpec,
    PlannedStep,
    ResearchPackage,
    ResearchRun,
    ScientificAudit,
    TestableHypotheses,
    TestableHypothesis,
)
from .prompts import get_prompt
from .runtime_config import RuntimeConfigStore


OutputModel = TypeVar("OutputModel", bound=BaseModel)


class ModelGateway(Protocol):
    provider_name: str

    async def generate(
        self, prompt_key: str, payload: dict[str, Any], output_model: type[OutputModel]
    ) -> OutputModel: ...


class ResearchExecutor(Protocol):
    executor_name: str

    async def execute(self, contract: FormalResearchContract) -> ResearchRun: ...


def _variables(package: ResearchPackage, *roles: str) -> list[str]:
    return [variable.name for variable in package.variables if variable.role in roles]


def _planned(step_id: str, name: str, rationale: str, **parameters: Any) -> PlannedStep:
    return PlannedStep(
        step_id=step_id,
        name=name,
        rationale=rationale,
        parameters=parameters,
    )


class FixtureModelGateway:
    """Deterministic adapter for workflow verification; it never fabricates evidence."""

    provider_name = "fixture"

    async def generate(
        self, prompt_key: str, payload: dict[str, Any], output_model: type[OutputModel]
    ) -> OutputModel:
        handlers = {
            "intake": self._intake,
            "hypothesis_decomposition": self._decompose,
            "method_route": self._route,
            "analysis_design": self._design,
            "method_critic": self._critic,
            "plan_revision": self._revise,
            "evidence_assessment": self._assess,
            "scientific_audit": self._audit,
            "claim_ledger": self._claims,
            "scientific_writer": self._write,
        }
        try:
            raw = handlers[prompt_key](payload)
        except KeyError as error:
            raise ValueError(f"fixture has no handler for prompt {prompt_key}") from error
        return output_model.model_validate(raw)

    @staticmethod
    def _intake(payload: dict[str, Any]) -> dict[str, Any]:
        case = CaseSubmission.model_validate(payload["case"])
        return ResearchPackage(
            **case.model_dump(),
            input_conflicts=[],
            missing_required_information=(
                [] if case.dataset_refs else ["尚未接入可执行数据资产；本次只能形成研究设计。"]
            ),
        ).model_dump()

    @staticmethod
    def _decompose(payload: dict[str, Any]) -> dict[str, Any]:
        package = ResearchPackage.model_validate(payload["research_package"])
        outcomes = _variables(package, "outcome")
        exposures = _variables(package, "treatment", "exposure")
        mechanisms = _variables(package, "mediator")
        return TestableHypotheses(
            items=[
                TestableHypothesis(
                    hypothesis_id=hypothesis.hypothesis_id,
                    theoretical_claim=hypothesis.statement,
                    observable_prediction=(
                        f"在预先定义样本与模型中，{', '.join(exposures) or '核心解释变量'}"
                        f"与 {', '.join(outcomes) or '结果变量'} 呈{hypothesis.expected_direction}方向关系。"
                    ),
                    analysis_unit=package.unit_of_analysis,
                    outcome_variables=outcomes,
                    treatment_or_exposure_variables=exposures,
                    mechanism_variables=mechanisms,
                    boundary_conditions=["仅适用于冻结合同中的样本、时期与变量口径。"],
                    competing_explanations=["遗漏变量", "反向因果", "同期政策或共同趋势"],
                    falsification_conditions=["前置趋势或伪处理检验显示同类效应。", "替代口径下方向不稳定。"],
                )
                for hypothesis in package.hypotheses
            ]
        ).model_dump()

    @staticmethod
    def _route(payload: dict[str, Any]) -> dict[str, Any]:
        package = ResearchPackage.model_validate(payload["research_package"])
        profile = DataProfile.model_validate(payload["data_profile"])
        text = " ".join(
            [package.title, package.research_question, *package.known_policy_facts]
        ).lower()
        if profile.data_structure == "event" or any(word in text for word in ("公告日", "发行日", "事件研究")):
            route = "market_event"
            goal = "causal"
            assumptions = ["事件窗口无其他重大混杂事件", "预期收益模型设定合理"]
        elif profile.data_structure == "spatial_panel" or any(word in text for word in ("空间溢出", "邻近地区")):
            route = "spatial"
            goal = "causal"
            assumptions = ["空间权重矩阵事先定义", "空间依赖结构可识别"]
        elif any(word in text for word in ("政策", "试验区", "指引", "试点", "did")):
            route = "policy_causal"
            goal = "causal"
            assumptions = ["平行趋势", "无预期效应", "不存在与处理同时发生的差异化冲击"]
        elif any(word in text for word in ("机制", "中介", "调节", "异质性")):
            route = "mechanism_boundary"
            goal = "mechanism"
            assumptions = ["机制变量时间顺序合理", "机制结论不超过识别设计"]
        elif any(word in text for word in ("指数", "效率", "sbm", "熵值")):
            route = "measurement_efficiency"
            goal = "measurement"
            assumptions = ["指标选择与权重规则预先确定"]
        elif any(word in text for word in ("dsge", "结构模型", "宏观模拟")):
            route = "structural_macro"
            goal = "structural"
            assumptions = ["结构参数可识别", "校准目标与数据矩一致"]
        elif profile.data_structure in ("panel", "cross_section"):
            route = "panel_association"
            goal = "associational"
            assumptions = ["固定效应和控制变量足以支持受限关联解释"]
        else:
            return MethodRoute(
                route_status="needs_human_review",
                research_goal="mixed",
                primary_route=None,
                route_reason=["研究目标与数据结构不足以唯一确定方法家族。"],
                required_assumptions=[],
                testable_assumptions=[],
                untestable_assumptions=[],
                alternative_routes=[],
                rejected_routes=[],
                missing_information=["请补充数据结构、分析层级或外生冲击信息。"],
            ).model_dump()
        return MethodRoute(
            route_status="routed",
            research_goal=goal,
            primary_route=route,
            route_reason=[f"研究目标与输入特征匹配 {route} 方法家族。"],
            required_assumptions=assumptions,
            testable_assumptions=assumptions[:2],
            untestable_assumptions=assumptions[2:],
            alternative_routes=[],
            rejected_routes=[],
            missing_information=([] if package.dataset_refs else ["尚未提供可执行数据资产"]),
        ).model_dump()

    @staticmethod
    def _design(payload: dict[str, Any]) -> dict[str, Any]:
        package = ResearchPackage.model_validate(payload["research_package"])
        route = MethodRoute.model_validate(payload["method_route"])
        profile = DataProfile.model_validate(payload["data_profile"])
        family = route.primary_route
        if family is None:
            raise ValueError("cannot design without a routed method family")
        outcomes = _variables(package, "outcome")
        exposures = _variables(package, "treatment", "exposure")
        controls = _variables(package, "control")
        entity = _variables(package, "id")
        time = _variables(package, "time")
        estimator = {
            "policy_causal": "DID / staggered-adoption DID（按政策实施方式确定）",
            "panel_association": "双向固定效应面板模型",
            "mechanism_boundary": "固定效应主模型 + 预注册机制/边界检验",
            "market_event": "事件研究",
            "spatial": "SAR/SEM/SDM 选择诊断",
            "measurement_efficiency": "熵值法或 Super-SBM（按指标目标确定）",
            "structural_macro": "结构模型设计（高级分支）",
        }[family]
        diagnostics = [_planned("diag_data", "数据完整性与主键诊断", "确认样本和唯一键可执行")]
        if family == "policy_causal":
            diagnostics.extend(
                [
                    _planned("diag_parallel", "平行趋势与动态效应", "DID 的必要识别诊断"),
                    _planned("diag_anticipation", "预期效应检查", "排除政策前行为调整"),
                ]
            )
        if family == "spatial":
            diagnostics.append(_planned("diag_spatial", "空间相关诊断", "选择空间模型并检验权重矩阵敏感性"))
        formula = None
        if outcomes and exposures:
            formula = f"{outcomes[0]} ~ {' + '.join(exposures + controls)}"
        return AnalysisPlan(
            plan_id=f"plan-{package.case_id}",
            plan_version=1,
            method_family=family,
            base_method_family="panel_association" if family == "mechanism_boundary" else None,
            design_only=not bool(package.dataset_refs),
            estimands=[_planned("estimand_main", "核心估计对象", "对应 H1 的预先定义效应或关联参数")],
            sample_rules=[_planned("sample_main", "冻结样本边界", "禁止观察结果后调整样本", period=package.sample_period)],
            variable_construction=[_planned("vars_main", "冻结变量口径", "使用案例包定义并记录全部变换")],
            baseline_models=[
                ModelSpec(
                    step_id="model_baseline",
                    name="基准模型",
                    rationale="对应主假设的首要模型",
                    estimator=estimator,
                    formula=formula,
                    outcome=outcomes[0] if outcomes else None,
                    treatments_or_exposures=exposures,
                    controls=controls,
                    fixed_effects=entity + time,
                    standard_error_strategy="按分析层级聚类；具体维度在 H2 前确认",
                )
            ],
            diagnostics=diagnostics,
            robustness_tests=[_planned("robust_alt_measure", "替代变量口径", "检验结论对测量选择的敏感性")],
            falsification_tests=[_planned("falsification_placebo", "安慰剂或伪处理", "排除机械相关和共同趋势")],
            mechanism_tests=(
                [_planned("mechanism_predefined", "预注册机制检验", "机制证据不得写成已证明因果链")]
                if _variables(package, "mediator")
                else []
            ),
            heterogeneity_tests=[_planned("heterogeneity_predefined", "预定义异质性", "只允许理论事先支持的分组")],
            identification_assumptions=route.required_assumptions,
            alternative_explanations=["反向因果", "遗漏变量", "同期政策"],
            failure_conditions=["必要识别诊断失败", "核心变量无法按冻结口径构造"],
            stop_conditions=["完成预注册模型与诊断后停止，不按显著性追加模型"],
            required_data_fields=[variable.name for variable in package.variables],
            unsupported_requested_analyses=(
                ["当前未接入数据，所有统计分析均未执行"] if not package.dataset_refs else []
            ),
        ).model_dump()

    @staticmethod
    def _critic(payload: dict[str, Any]) -> dict[str, Any]:
        plan = AnalysisPlan.model_validate(payload["analysis_plan"])
        dimension = payload.get("dimension", "reproducibility")
        remaining = []
        if plan.design_only:
            remaining.append("尚未接入数据，只能审查设计，不能确认可执行性。")
        return CriticReport(
            report_id=f"critic-{dimension}-{plan.plan_version}",
            review_round=max(1, plan.revision_round + 1),
            verdict="pass",
            issues=[],
            approved_elements=[f"{dimension} 维度未发现阻止 H2 的结构性问题。"],
            remaining_risks=remaining,
        ).model_dump()

    @staticmethod
    def _revise(payload: dict[str, Any]) -> dict[str, Any]:
        plan = AnalysisPlan.model_validate(payload["analysis_plan"])
        revised = plan.model_copy(deep=True)
        revised.plan_version += 1
        revised.revision_round += 1
        return revised.model_dump()

    @staticmethod
    def _assess(payload: dict[str, Any]) -> dict[str, Any]:
        run = ResearchRun.model_validate(payload["research_run"])
        if run.fixture_only or run.execution_status in ("not_executed", "fixture_only"):
            return EvidenceAssessment(
                evidence_status="not_tested",
                execution_status=run.execution_status,
                scientific_status="not_evaluated",
                supporting_run_ids=[],
                opposing_run_ids=[],
                limitations=[run.not_executed_reason or "未执行真实统计分析"],
            ).model_dump()
        return EvidenceAssessment(
            evidence_status="inconclusive",
            execution_status=run.execution_status,
            scientific_status=run.scientific_status,
            supporting_run_ids=[],
            opposing_run_ids=[],
            limitations=["需要由配置的模型网关解释真实执行记录。"],
        ).model_dump()

    @staticmethod
    def _audit(payload: dict[str, Any]) -> dict[str, Any]:
        assessment = EvidenceAssessment.model_validate(payload["evidence_assessment"])
        if assessment.evidence_status == "not_tested":
            return ScientificAudit(
                verdict="not_evaluated",
                contract_compliant=True,
                critical_issues=[],
                unresolved_risks=assessment.limitations,
            ).model_dump()
        return ScientificAudit(
            verdict="limited",
            contract_compliant=True,
            critical_issues=[],
            unresolved_risks=assessment.limitations,
        ).model_dump()

    @staticmethod
    def _claims(payload: dict[str, Any]) -> dict[str, Any]:
        package = ResearchPackage.model_validate(payload["research_package"])
        run = ResearchRun.model_validate(payload["research_run"])
        assessment = EvidenceAssessment.model_validate(payload["evidence_assessment"])
        no_evidence = run.fixture_only or assessment.evidence_status == "not_tested"
        return ClaimLedger(
            ledger_id=f"ledger-{run.research_run_id}",
            case_id=package.case_id,
            research_run_id=run.research_run_id,
            claims=[
                ClaimRecord(
                    claim_id=f"claim-{hypothesis.hypothesis_id}",
                    hypothesis_id=hypothesis.hypothesis_id,
                    claim_text=(
                        f"{hypothesis.statement}（尚未检验）"
                        if no_evidence
                        else hypothesis.statement
                    ),
                    evidence_status="not_tested" if no_evidence else assessment.evidence_status,
                    allowed_strength="prohibited" if no_evidence else "insufficient",
                    supporting_runs=[],
                    opposing_runs=[],
                    scope="冻结合同定义的样本、时期与变量口径",
                    robustness_status="not_executed" if no_evidence else "pending_review",
                    unresolved_risks=assessment.limitations,
                )
                for hypothesis in package.hypotheses
            ],
            excluded_findings=[],
            unresolved_issues=assessment.limitations,
        ).model_dump()

    @staticmethod
    def _write(payload: dict[str, Any]) -> dict[str, Any]:
        package = ResearchPackage.model_validate(payload["research_package"])
        plan = AnalysisPlan.model_validate(payload["analysis_plan"])
        run = ResearchRun.model_validate(payload["research_run"])
        approved_claims = payload.get("approved_claims", [])
        plan_md = "\n".join(
            [
                f"# {package.title}：科学假设与研究计划",
                "",
                f"## 研究问题\n{package.research_question}",
                "",
                "## 待检验假设",
                *[f"- {item.statement}" for item in package.hypotheses],
                "",
                f"## 方法路线\n{plan.method_family}",
                "",
                "## 基准设计",
                *[f"- {model.name}：{model.estimator}" for model in plan.baseline_models],
                "",
                "## 必要诊断与证伪",
                *[f"- {step.name}" for step in [*plan.diagnostics, *plan.falsification_tests]],
                "",
                "## 当前证据边界",
                "本 Run 未执行真实统计分析，不报告任何样本量、系数、显著性或实证结论。",
            ]
        )
        plan_only = run.fixture_only or run.execution_status in ("not_executed", "fixture_only")
        sections = [
            ManuscriptSection(
                section_id="research_plan",
                title="科学假设与研究计划",
                content_markdown=plan_md,
                status="generated",
            )
        ]
        if not plan_only:
            sections.append(
                ManuscriptSection(
                    section_id="approved_findings",
                    title="获批实证发现",
                    content_markdown="\n".join(
                        f"- {claim['final_text'] or claim['claim_text']}" for claim in approved_claims
                    ),
                    status="generated",
                    claim_ids=[claim["claim_id"] for claim in approved_claims],
                    run_ids=[run.research_run_id],
                )
            )
        return ManuscriptPackage(
            package_id=f"manuscript-{package.case_id}",
            case_id=package.case_id,
            mode="research_plan_only" if plan_only else "full_manuscript",
            status="ready_for_human_review",
            research_plan_markdown=plan_md,
            manuscript_sections=sections,
            empirical_findings_status="prohibited_fixture" if run.fixture_only else ("not_executed" if plan_only else "included"),
            disclosures=[
                "该成果由 HypoWeaver-Qwen 代码工作流生成。",
                "Fixture 模式仅验证流程，不构成实证研究结果。",
            ] if plan_only else ["所有实证表述仅来自 H3 授权结论。"],
            unresolved_issues=[] if approved_claims else ["当前没有可写入的获批实证结论。"],
        ).model_dump()


class QwenModelGateway:
    provider_name = "qwen"

    def __init__(self) -> None:
        config = RuntimeConfigStore().resolve()
        if not config.qwen_api_key:
            raise RuntimeError(
                "Qwen API Key is required; configure runtime settings or DASHSCOPE_API_KEY"
            )
        self.model = config.qwen_model
        self.client = AsyncOpenAI(
            api_key=config.qwen_api_key,
            base_url=config.qwen_base_url,
        )

    async def generate(
        self, prompt_key: str, payload: dict[str, Any], output_model: type[OutputModel]
    ) -> OutputModel:
        prompt = get_prompt(prompt_key)
        messages = [
            {"role": item["role"], "content": item["rendered"]}
            for item in prompt.render(payload)
        ]
        last_error: Exception | None = None
        for attempt in range(2):
            if attempt:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "上一输出未通过 JSON Schema 校验。只修复结构，不改变研究判断。"
                            f"\nSchema: {json.dumps(output_model.model_json_schema(), ensure_ascii=False)}"
                            f"\n错误: {last_error}"
                        ),
                    }
                )
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0,
            )
            content = response.choices[0].message.content or "{}"
            try:
                return output_model.model_validate_json(content)
            except (ValidationError, ValueError) as error:
                last_error = error
        raise ValueError(f"qwen output failed schema validation: {last_error}")


class FixtureExecutor:
    executor_name = "fixture"

    async def execute(self, contract: FormalResearchContract) -> ResearchRun:
        return ResearchRun(
            research_run_id=f"research-{uuid4()}",
            case_id=contract.case_id,
            contract_hash=contract.approved_plan_hash,
            plan_version=contract.approved_plan.plan_version,
            execution_status="fixture_only",
            scientific_status="not_evaluated",
            fixture_only=True,
            not_executed_reason="Fixture Executor 只验证工作流状态与接口，未执行任何统计模型。",
            executions=[
                ExecutionRecord(
                    execution_id=f"execution-{uuid4()}",
                    run_type="baseline",
                    plan_step_id="model_baseline",
                    execution_status="not_executed",
                    estimates=[],
                    diagnostic_results={},
                    warnings=["未配置真实 Python Research Engine。"],
                )
            ],
            warnings=["Fixture 结果不得进入实证论文结论。"],
        )


class HttpResearchExecutor:
    executor_name = "external"

    def __init__(self) -> None:
        config = RuntimeConfigStore().resolve()
        if not config.research_engine_url:
            raise RuntimeError(
                "Python Research Engine URL is required; configure runtime settings or RESEARCH_ENGINE_URL"
            )
        self.url = config.research_engine_url
        self.token = config.research_engine_token

    async def execute(self, contract: FormalResearchContract) -> ResearchRun:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{self.url.rstrip('/')}/v1/runs",
                json={"contract": contract.model_dump(mode="json")},
                headers=headers,
            )
            response.raise_for_status()
        return ResearchRun.model_validate(response.json())

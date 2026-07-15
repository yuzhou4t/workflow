from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd
from pydantic import BaseModel

from .adapters import (
    FixtureExecutor,
    FixtureModelGateway,
    HttpResearchExecutor,
    ModelGateway,
    QwenModelGateway,
    ResearchExecutor,
)
from .definition import DEFINITION_VERSION
from .case_import import CaseImportError, DatasetRegistry
from .models import (
    AnalysisPlan,
    CaseSubmission,
    CandidateDesignSet,
    ClaimLedger,
    DesignArena,
    DesignCandidate,
    DesignEnvelope,
    DesignReviewerReport,
    CriticIssue,
    CriticReport,
    DataProfile,
    DecisionRecord,
    EvidenceAssessment,
    FormalResearchContract,
    FULL_MANUSCRIPT_SECTION_IDS,
    GateDecisionRequest,
    ManuscriptPackage,
    ManuscriptSection,
    MethodRoute,
    PromptContent,
    ProbeCheck,
    ProbeReport,
    ResearchPackage,
    ResearchRun,
    ReproductionAudit,
    RevisionRequest,
    RunEvent,
    RunState,
    ScientificAudit,
    StepAttempt,
    TRACEABLE_MANUSCRIPT_SECTION_IDS,
    TestableHypotheses,
    CreateRunRequest,
    utc_now,
)
from .prompts import get_prompt
from .repository import RunRepository, VersionConflictError
from .seal import canonical_sha256, sign_manifest
from .spatial import SpatialWeights, is_spatial_weights_filename


class WorkflowTransitionError(RuntimeError):
    pass


PRESET_CASES: dict[str, CaseSubmission] = {
    "green-finance-did": CaseSubmission(
        case_id="green-finance-did",
        title="绿色金融试验区政策评估",
        research_question="绿色金融改革创新试验区政策是否促进企业绿色创新？",
        hypotheses=[
            {
                "hypothesis_id": "H1",
                "statement": "绿色金融改革创新试验区政策促进企业绿色创新。",
                "expected_direction": "positive",
                "mechanism": "政策通过缓解绿色项目融资约束并强化创新激励发挥作用。",
            }
        ],
        unit_of_analysis="企业—年度",
        sample_period="政策前后若干年度（由数据组最终确认）",
        data_structure_hint="panel",
        variables=[
            {"name": "firm_id", "label": "企业代码", "role": "id"},
            {"name": "year", "label": "年份", "role": "time"},
            {"name": "green_patent", "label": "绿色专利", "role": "outcome"},
            {"name": "treat_post", "label": "试验区政策处理", "role": "treatment"},
            {"name": "firm_size", "label": "企业规模", "role": "control"},
            {"name": "leverage", "label": "资产负债率", "role": "control"},
        ],
        known_policy_facts=["政策存在明确实施时间和试点地区；具体名单由案例包提供。"],
        constraints=["原论文结论与回归结果不得进入 App A。"],
    ),
    "esg-panel": CaseSubmission(
        case_id="esg-panel",
        title="ESG 与企业融资成本",
        research_question="企业 ESG 表现是否与融资成本下降相关？",
        hypotheses=[
            {
                "hypothesis_id": "H1",
                "statement": "企业 ESG 表现改善与融资成本下降相关。",
                "expected_direction": "negative",
                "mechanism": "更好的 ESG 表现降低信息不对称并改善融资环境。",
            }
        ],
        unit_of_analysis="企业—年度",
        sample_period="由数据组最终确认",
        data_structure_hint="panel",
        variables=[
            {"name": "firm_id", "label": "企业代码", "role": "id"},
            {"name": "year", "label": "年份", "role": "time"},
            {"name": "financing_cost", "label": "融资成本", "role": "outcome"},
            {"name": "esg_score", "label": "ESG 评分", "role": "exposure"},
            {"name": "firm_size", "label": "企业规模", "role": "control"},
            {"name": "roa", "label": "总资产收益率", "role": "control"},
        ],
        constraints=["本设计默认只支持受限关联表述，因果结论需要额外识别策略。"],
    ),
}


MAX_MANUSCRIPT_REPAIR_ROUNDS = 2
LEGACY_OVERBROAD_EXECUTION_WARNING = (
    "稳健性、证伪、机制和异质性步骤尚未执行，因此科学状态标记为 limited。"
)
WRITER_ESCALATION_MODEL = "qwen3.7-max"
REVIEWER_MODEL = "qwen3.7-max"
DESIGN_RETRY_MODEL = "qwen3.7-max"
DESIGN_STRATEGIES: tuple[tuple[str, str], ...] = (
    ("direct_baseline", "以目标估计量和最小可执行模型为优先，不追逐显著性。"),
    ("identification_first", "优先处理识别威胁、竞争解释与证伪条件。"),
    ("measurement_robustness", "优先处理变量口径、缺失样本与测量敏感性。"),
)
WRITER_ESCALATION_SECTION_IDS = {
    "introduction",
    "theory_hypotheses",
    "data_variables",
    "research_design",
    "empirical_results",
    "discussion_limitations",
}


MANUSCRIPT_SECTION_SPECS: tuple[dict[str, str], ...] = (
    {
        "section_id": "abstract",
        "title": "摘要",
        "target_characters": "450-650",
        "focus": "概括研究问题、数据与方法、已执行主要证据、识别边界与贡献，不引入新数字。",
        "evidence_keys": "research_context,data_profile,frozen_design,executed_evidence,authorized_claims,writing_requirements",
    },
    {
        "section_id": "introduction",
        "title": "一、引言",
        "target_characters": "650-900",
        "focus": "从研究问题的经济与治理意义切入，说明现实张力、核心问题、分析思路与本稿实际完成的工作；无文献时不宣称文献空白或创新性。",
        "evidence_keys": "research_context,authorized_claims,writing_requirements",
    },
    {
        "section_id": "theory_hypotheses",
        "title": "二、理论分析与研究假设",
        "target_characters": "650-900",
        "focus": "围绕输入中的假设和可用机制形成可证伪推理，同时陈述反向因果和遗漏变量等竞争性解释。",
        "evidence_keys": "research_context,frozen_design,writing_requirements",
    },
    {
        "section_id": "data_variables",
        "title": "三、数据、样本与变量",
        "target_characters": "650-900",
        "focus": "完整交代分析单位、样本时期、筛选规则、变量角色、定义、来源、预处理和已知数据质量情况；每个来源必须忠于输入。",
        "evidence_keys": "research_context,data_profile,frozen_design,executed_evidence,writing_requirements",
    },
    {
        "section_id": "research_design",
        "title": "四、研究设计",
        "target_characters": "650-900",
        "focus": "说明估计对象、模型形式、固定效应、标准误处理、控制变量、识别假设与冻结的后续检验。",
        "evidence_keys": "research_context,frozen_design,executed_evidence,writing_requirements",
    },
    {
        "section_id": "empirical_results",
        "title": "五、实证结果",
        "target_characters": "650-900",
        "focus": "只解释真实执行的基准结果及其范围，不引用不存在的图表，不对统计量作无基准的价值评价；明确区分已执行与尚未执行分析。",
        "evidence_keys": "research_context,executed_evidence,authorized_claims,writing_requirements",
    },
    {
        "section_id": "discussion_limitations",
        "title": "六、讨论、局限与后续检验",
        "target_characters": "650-900",
        "focus": "解释证据可能的理论含义，系统呈现测量、反向因果、时变混杂、稳健性与外部效度边界，并列出可执行的后续检验。",
        "evidence_keys": "research_context,frozen_design,executed_evidence,authorized_claims,writing_requirements",
    },
    {
        "section_id": "conclusion",
        "title": "七、结论",
        "target_characters": "450-650",
        "focus": "回答研究问题，保持 H3 授权的证据强度，概括审慎启示；后续工作优先使用冻结计划，其他方法需新设计审批，不将关联表述升格为因果结论。",
        "evidence_keys": "research_context,executed_evidence,authorized_claims,writing_requirements",
    },
)


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _hash(value: Any) -> str:
    return canonical_sha256(_plain(value))


class WorkflowEngine:
    def __init__(
        self,
        repository: RunRepository,
        dataset_registry: DatasetRegistry | None = None,
    ) -> None:
        self.repository = repository
        self.dataset_registry = dataset_registry or DatasetRegistry()

    def _gateway(self, state: RunState) -> ModelGateway:
        return QwenModelGateway() if state.model_provider == "qwen" else FixtureModelGateway()

    def _reviewer_gateway(self, state: RunState) -> ModelGateway:
        if state.model_provider == "qwen":
            return QwenModelGateway(model_override=REVIEWER_MODEL)
        return FixtureModelGateway()

    @staticmethod
    def _event(
        state: RunState,
        event_type: str,
        message: str,
        *,
        node_id: str | None = None,
        status: str | None = None,
    ) -> None:
        state.events.append(
            RunEvent(
                seq=len(state.events) + 1,
                type=event_type,
                message=message,
                node_id=node_id,
                status=status,
            )
        )

    @staticmethod
    def _put_artifact(state: RunState, key: str, value: Any) -> dict[str, Any]:
        payload = _plain(value)
        envelope = {
            "artifact_id": f"{state.id}:{key}",
            "kind": key,
            "sha256": _hash(payload),
            "payload": payload,
        }
        state.artifacts[key] = envelope
        return envelope

    @staticmethod
    def _artifact(state: RunState, key: str, model: type[BaseModel]) -> Any:
        try:
            payload = state.artifacts[key]["payload"]
        except KeyError as error:
            raise WorkflowTransitionError(f"required artifact is missing: {key}") from error
        return model.model_validate(payload)

    @staticmethod
    def _attempt_number(state: RunState, node_id: str) -> int:
        return 1 + sum(step.node_id == node_id for step in state.steps)

    def _record_step(
        self,
        state: RunState,
        node_id: str,
        status: str,
        *,
        input_value: Any = None,
        output_value: Any = None,
        prompts: list[dict[str, Any]] | None = None,
        logs: list[str] | None = None,
        error: str | None = None,
    ) -> StepAttempt:
        now = utc_now()
        step = StepAttempt(
            node_id=node_id,
            attempt=self._attempt_number(state, node_id),
            status=status,
            started_at=now,
            ended_at=None if status in ("running", "waiting_human") else now,
            prompts=[PromptContent.model_validate(prompt) for prompt in (prompts or [])],
            input=_plain(input_value),
            output=_plain(output_value),
            logs=logs or [],
            error=error,
        )
        state.steps.append(step)
        state.current_node_id = node_id
        self._event(
            state,
            f"step.{status}",
            logs[-1] if logs else f"{node_id}: {status}",
            node_id=node_id,
            status=status,
        )
        return step

    async def _llm_step(
        self,
        state: RunState,
        node_id: str,
        prompt_key: str,
        payload: dict[str, Any],
        output_model: type[BaseModel],
        gateway: ModelGateway | None = None,
    ) -> BaseModel:
        prompt = get_prompt(prompt_key)
        rendered = prompt.render(payload)
        try:
            output = await (gateway or self._gateway(state)).generate(
                prompt_key, payload, output_model
            )
        except Exception as error:
            self._record_step(
                state,
                node_id,
                "failed",
                input_value=payload,
                prompts=rendered,
                logs=[f"{prompt.title}未通过 Schema 校验或模型调用失败。"],
                error=str(error),
            )
            raise
        self._record_step(
            state,
            node_id,
            "succeeded",
            input_value=payload,
            output_value=output,
            prompts=rendered,
            logs=[f"{prompt.title}完成；Prompt {prompt.version}；输出已通过 {output_model.__name__} 校验。"],
        )
        return output

    def _pause_at_gate(self, state: RunState, gate: str, input_value: Any) -> None:
        node_id = f"{gate.lower()}_gate"
        state.status = "waiting_human"
        state.current_gate = gate
        self._record_step(
            state,
            node_id,
            "waiting_human",
            input_value=input_value,
            logs=[f"{gate} 已暂停，等待服务端记录人工决定。"],
        )
        self._event(state, "gate.waiting", f"{gate} 等待人工决定。", node_id=node_id, status="waiting_human")

    def _profile(self, package: ResearchPackage) -> DataProfile:
        entity_keys = _names(package, "id")
        time_keys = _names(package, "time")
        spatial_keys = _names(package, "spatial_id")
        event_keys = _names(package, "event_date")
        has_refs = bool(package.dataset_refs)
        supported = {
            "panel": ["policy_causal", "panel_association", "mechanism_boundary"],
            "spatial_panel": ["spatial", "panel_association"],
            "event": ["market_event"],
            "time_series": ["structural_macro"],
            "cross_section": ["panel_association", "measurement_efficiency"],
            "unknown": [],
        }[package.data_structure_hint]
        if not has_refs:
            return DataProfile(
                profile_execution_status="not_executed",
                data_structure=package.data_structure_hint,
                unit_of_observation=package.unit_of_analysis,
                entity_key=entity_keys,
                time_key=time_keys[0] if time_keys else None,
                spatial_key=spatial_keys[0] if spatial_keys else None,
                event_date_key=event_keys[0] if event_keys else None,
                confirmed_facts=[
                    f"案例声明的数据结构为 {package.data_structure_hint}。",
                    f"变量字典包含 {len(package.variables)} 个字段。",
                ],
                measurement_risks=["尚未接入可执行数据资产。"],
                supported_method_families=supported,
                readiness="blocked",
                blocking_reasons=["没有可执行数据资产；仅允许形成研究计划。"],
            )

        selected_columns = list(dict.fromkeys(variable.name for variable in package.variables))
        key_columns = [*entity_keys, *time_keys]
        try:
            main_ref = next(
                (item for item in package.dataset_refs if item.role == "main"),
                package.dataset_refs[0],
            )
            source = self.dataset_registry.resolve(main_ref)
            _verify_dataset_hash(source, main_ref.sha256)
            frame, column_count = _read_profile_csv(source, selected_columns)
        except (CaseImportError, OSError, ValueError) as error:
            return DataProfile(
                profile_execution_status="failed",
                data_structure=package.data_structure_hint,
                unit_of_observation=package.unit_of_analysis,
                entity_key=entity_keys,
                time_key=time_keys[0] if time_keys else None,
                spatial_key=spatial_keys[0] if spatial_keys else None,
                event_date_key=event_keys[0] if event_keys else None,
                confirmed_facts=[f"变量字典包含 {len(package.variables)} 个字段。"],
                measurement_risks=[f"数据画像读取失败：{error}"],
                supported_method_families=supported,
                readiness="blocked",
                blocking_reasons=["实际数据无法在 H2 前完成确定性画像。"],
            )

        missing_columns = [name for name in selected_columns if name not in frame.columns]
        missingness = [
            {
                "variable": name,
                "missing_count": int(frame[name].isna().sum()),
                "missing_rate": float(frame[name].isna().mean()) if len(frame) else 0.0,
            }
            for name in selected_columns
            if name in frame.columns
        ]
        duplicate_key_count = (
            int(frame.duplicated(subset=key_columns, keep=False).sum())
            if key_columns and all(name in frame.columns for name in key_columns)
            else None
        )
        risks: list[str] = []
        if missing_columns:
            risks.append("变量字典字段未出现在数据中：" + "、".join(missing_columns))
        if duplicate_key_count:
            risks.append(f"发现 {duplicate_key_count} 行处于重复实体—时间主键中，执行前必须按冻结规则处理。")
        variables_with_missing = [
            item["variable"] for item in missingness if item["missing_count"]
        ]
        if variables_with_missing:
            risks.append("以下建模字段存在缺失值：" + "、".join(variables_with_missing))
        missing_definitions = [
            variable.name for variable in package.variables if not variable.definition
        ]
        if missing_definitions:
            risks.append("以下变量缺少定义：" + "、".join(missing_definitions))

        blocking_reasons: list[str] = []
        if missing_columns:
            blocking_reasons.append("冻结计划所需字段与实际数据表不一致。")
        if package.data_structure_hint in {"panel", "spatial_panel"} and (
            not entity_keys or not time_keys
        ):
            blocking_reasons.append("面板数据缺少实体或时间主键。")

        spatial_facts: list[str] = []
        if package.data_structure_hint == "spatial_panel":
            if not spatial_keys:
                blocking_reasons.append("空间面板缺少 spatial_id 字段。")
            weights_ref = next(
                (
                    item
                    for item in package.dataset_refs
                    if item.role == "supplementary"
                    and is_spatial_weights_filename(item.filename)
                ),
                None,
            )
            if weights_ref is None:
                blocking_reasons.append("空间面板缺少 spatial_weights.csv 权重资产。")
            elif spatial_keys and spatial_keys[0] in frame.columns:
                try:
                    weights_path = self.dataset_registry.resolve(weights_ref)
                    _verify_dataset_hash(weights_path, weights_ref.sha256)
                    weights = SpatialWeights.from_csv(weights_path)
                    weights.aligned(
                        sorted(frame[spatial_keys[0]].dropna().astype(str).unique())
                    )
                    spatial_facts.extend(
                        [
                            f"空间权重矩阵包含 {len(weights.labels)} 个唯一空间单元。",
                            "空间权重矩阵行列标签一致、对角线为 0、行和为 1。",
                            f"空间权重资产 SHA256 已核验：{weights_ref.sha256}。",
                        ]
                    )
                except (CaseImportError, OSError, ValueError) as error:
                    blocking_reasons.append(f"空间权重资产校验失败：{error}")

        return DataProfile(
            profile_execution_status="succeeded",
            data_structure=package.data_structure_hint,
            unit_of_observation=package.unit_of_analysis,
            entity_key=entity_keys,
            time_key=time_keys[0] if time_keys else None,
            spatial_key=spatial_keys[0] if spatial_keys else None,
            event_date_key=event_keys[0] if event_keys else None,
            row_count=len(frame),
            column_count=column_count,
            duplicate_key_count=duplicate_key_count,
            missingness=missingness,
            confirmed_facts=[
                f"案例声明的数据结构为 {package.data_structure_hint}。",
                f"变量字典包含 {len(package.variables)} 个字段。",
                f"实际 CSV 共 {len(frame)} 行、{column_count} 列。",
                *spatial_facts,
            ],
            measurement_risks=risks,
            merge_risks=[],
            supported_method_families=supported,
            unsupported_method_families=[],
            readiness=(
                "blocked"
                if blocking_reasons
                else ("partially_ready" if risks else "ready")
            ),
            blocking_reasons=blocking_reasons,
        )

    def _normalize_case(self, state: RunState, case: CaseSubmission) -> ResearchPackage:
        package = ResearchPackage(
            **case.model_dump(),
            input_conflicts=[],
            missing_required_information=(
                [] if case.dataset_refs else ["尚未接入可执行数据资产；本次只能形成研究设计。"]
            ),
        )
        self._record_step(
            state,
            "intake_agent",
            "succeeded",
            input_value={"case": case.model_dump(mode="json")},
            output_value=package,
            prompts=[
                {
                    "id": "intake:code",
                    "role": "code",
                    "template": "CaseSubmission → ResearchPackage deterministic normalization",
                    "rendered": "H1 前不调用外部模型；只按严格 Schema 规范化用户可见输入。",
                }
            ],
            logs=["案例规范化由确定性代码完成；H1 前未调用千问。"],
        )
        return package

    async def create_run(self, request: CreateRunRequest) -> RunState:
        if request.preset_case_id:
            try:
                case = PRESET_CASES[request.preset_case_id]
            except KeyError as error:
                raise ValueError(f"unknown preset case: {request.preset_case_id}") from error
        elif request.case:
            case = request.case
        else:
            raise ValueError("preset_case_id or case is required")

        provider = request.model_provider or ("qwen" if request.mode == "research" else "fixture")
        execution_mode = request.execution_mode or ("external" if request.mode == "research" else "fixture")
        state = RunState(
            definition_version=DEFINITION_VERSION,
            case_id=case.case_id,
            case_name=case.title,
            mode=request.mode,
            model_provider=provider,
            execution_mode=execution_mode,
            status="running",
            current_node_id="case_input",
            case_submission=case,
        )
        self._event(state, "run.created", "代码工作流 Run 已创建。", node_id="case_input")
        self._record_step(
            state,
            "case_input",
            "succeeded",
            output_value=case,
            logs=["输入已通过 CaseSubmission 严格 Schema；额外隐藏字段会被拒绝。"],
        )
        package = self._normalize_case(state, case)
        self._put_artifact(state, "research_package", package)
        validation = {
            "valid": not package.input_conflicts,
            "errors": package.input_conflicts,
            "warnings": package.missing_required_information,
            "hidden_reference_access": "denied_by_schema",
        }
        self._record_step(
            state,
            "input_validation",
            "succeeded" if validation["valid"] else "blocked",
            input_value=package,
            output_value=validation,
            prompts=[
                {
                    "id": "input_validation:code",
                    "role": "code",
                    "template": "Pydantic strict schema + deterministic research-boundary rules",
                    "rendered": "No LLM call; hidden reference fields are rejected before persistence.",
                }
            ],
            logs=["确定性输入校验完成；App A 未读取任何隐藏参考材料。"],
        )
        if not validation["valid"]:
            state.status = "blocked"
            state.last_error = "输入存在冲突，需要人工修订。"
        else:
            self._pause_at_gate(state, "H1", package)
        return self.repository.create(state)

    def get_run(self, run_id: str) -> RunState:
        return self.repository.get(run_id)

    def list_runs(self) -> list[RunState]:
        return self.repository.list()

    def delete_run(self, run_id: str) -> None:
        self.repository.delete(run_id)

    async def advance(self, run_id: str) -> RunState:
        state = self.repository.get(run_id)
        if state.status == "waiting_human":
            return state
        if (
            state.status == "failed"
            and state.current_node_id == "scientific_writer"
            and "approved_claim_ledger" in state.artifacts
            and "research_run" in state.artifacts
        ):
            return await self.retry_writing(run_id)
        if state.status in ("completed", "stopped"):
            return state
        raise WorkflowTransitionError(
            "该 Run 不能无条件继续；请在当前人工闸门作决定，或修订导致阻塞的输入。"
        )

    async def retry_writing(self, run_id: str) -> RunState:
        state = self.repository.get(run_id)
        if state.mode != "research" or state.plan_only:
            raise WorkflowTransitionError("只有已有真实执行结果的研究 Run 可以重试论文写作。")
        if "approved_claim_ledger" not in state.artifacts or "research_run" not in state.artifacts:
            raise WorkflowTransitionError("缺少 H3 授权结论或 ResearchRun，不能重试论文写作。")
        failed_writer = (
            state.status == "failed"
            and state.current_node_id == "scientific_writer"
        )
        completed_draft = (
            state.status == "completed"
            and "manuscript_package" in state.artifacts
        )
        waiting_h4 = state.status == "waiting_human" and state.current_gate == "H4"
        if not failed_writer and not completed_draft and not waiting_h4:
            raise WorkflowTransitionError("当前 Run 不在可重试的论文写作状态。")

        current_version = state.version
        transition_key = f"retry-writer-{uuid4()}"
        self.repository.claim_transition(
            run_id,
            expected_version=current_version,
            idempotency_key=transition_key,
        )
        try:
            human_review_feedback = None
            if (
                state.decisions
                and state.decisions[-1].gate == "H4"
                and state.decisions[-1].action == "revise"
            ):
                h4_decision = state.decisions[-1]
                feedback_already_attempted = any(
                    step.node_id == "scientific_writer"
                    and bool(step.started_at)
                    and step.started_at >= h4_decision.created_at
                    for step in state.steps
                )
                if not feedback_already_attempted:
                    human_review_feedback = h4_decision.comment.strip() or None
            previous_version = int(
                state.artifacts.get("manuscript_package", {})
                .get("payload", {})
                .get("version", 0)
                or 0
            )
            existing_sections: list[ManuscriptSection] | None = None
            existing_payload = state.artifacts.get("manuscript_package", {}).get(
                "payload"
            )
            if existing_payload:
                existing_sections = ManuscriptPackage.model_validate(
                    existing_payload
                ).manuscript_sections
            latest_generated: dict[str, ManuscriptSection] = {}
            last_completed_step = max(
                (
                    index
                    for index, step in enumerate(state.steps)
                    if step.node_id == "complete" and step.status == "succeeded"
                ),
                default=-1,
            )
            failed_draft_steps = (
                state.steps[last_completed_step + 1:]
                if failed_writer
                else []
            )
            for step in failed_draft_steps:
                if step.node_id != "scientific_writer" or step.status != "succeeded":
                    continue
                try:
                    section = ManuscriptSection.model_validate(step.output)
                except (TypeError, ValueError):
                    continue
                latest_generated[section.section_id] = section
            if existing_sections and latest_generated:
                sections_by_id = {
                    section.section_id: section
                    for section in existing_sections
                }
                sections_by_id.update(latest_generated)
                if set(FULL_MANUSCRIPT_SECTION_IDS).issubset(sections_by_id):
                    existing_sections = [
                        sections_by_id[section_id]
                        for section_id in FULL_MANUSCRIPT_SECTION_IDS
                    ]
            state.status = "running"
            state.current_node_id = "scientific_writer"
            state.current_gate = None
            state.last_error = None
            package = self._artifact(state, "research_package", ResearchPackage)
            plan = self._artifact(state, "analysis_plan", AnalysisPlan)
            research_run = self._artifact(state, "research_run", ResearchRun)
            ledger = self._artifact(state, "approved_claim_ledger", ClaimLedger)
            approved_claims = [
                claim.model_dump(mode="json")
                for claim in ledger.claims
                if claim.approval_status in ("approved", "downgraded")
            ]
            try:
                await self._finalize_manuscript(
                    state,
                    package,
                    plan,
                    research_run,
                    approved_claims,
                    manuscript_version=previous_version + 1,
                    existing_sections=existing_sections,
                    reuse_existing_if_valid=failed_writer,
                    human_review_feedback=human_review_feedback,
                )
            except Exception as error:
                state.status = "failed"
                state.current_node_id = "scientific_writer"
                state.last_error = str(error)
                self._event(
                    state,
                    "run.failed",
                    f"论文写作失败：{error}",
                    node_id="scientific_writer",
                    status="failed",
                )
            return self.repository.save(state, expected_version=current_version)
        finally:
            self.repository.release_transition(run_id, transition_key)

    async def retry_design(self, run_id: str) -> RunState:
        state = self.repository.get(run_id)
        if (
            state.status != "failed"
            or state.current_node_id is None
            or not (
                state.current_node_id.startswith("design_")
                or state.current_node_id.startswith("critic_")
            )
        ):
            raise WorkflowTransitionError(
                "只有候选设计或 Reviewer 失败的 Run 可以重试设计阶段。"
            )
        required_artifacts = (
            "research_package",
            "testable_hypotheses",
            "data_profile",
            "method_route",
            "design_envelope",
        )
        if any(key not in state.artifacts for key in required_artifacts):
            raise WorkflowTransitionError("设计阶段恢复所需 Artifact 不完整。")

        current_version = state.version
        transition_key = f"retry-design-{uuid4()}"
        self.repository.claim_transition(
            run_id,
            expected_version=current_version,
            idempotency_key=transition_key,
        )
        try:
            state.status = "running"
            state.last_error = None
            package = self._artifact(state, "research_package", ResearchPackage)
            hypotheses = self._artifact(
                state, "testable_hypotheses", TestableHypotheses
            )
            profile = self._artifact(state, "data_profile", DataProfile)
            route = self._artifact(state, "method_route", MethodRoute)
            envelope = self._artifact(state, "design_envelope", DesignEnvelope)
            selected_node = f"design_{route.primary_route}"
            try:
                if "candidate_design_set" in state.artifacts:
                    candidate_set = self._artifact(
                        state, "candidate_design_set", CandidateDesignSet
                    )
                else:
                    retry_gateway = (
                        QwenModelGateway(model_override=DESIGN_RETRY_MODEL)
                        if state.model_provider == "qwen"
                        else None
                    )
                    candidate_set = await self._generate_design_candidates(
                        state,
                        selected_node,
                        package,
                        hypotheses,
                        profile,
                        route,
                        envelope,
                        gateway=retry_gateway,
                    )
                    self._put_artifact(
                        state, "candidate_design_set", candidate_set
                    )
                await self._review_design_arena(
                    state,
                    package,
                    profile,
                    route,
                    envelope,
                    candidate_set,
                )
            except Exception as error:
                state.status = "failed"
                state.last_error = str(error)
                self._event(
                    state,
                    "run.failed",
                    f"设计阶段恢复失败：{error}",
                    node_id=state.current_node_id,
                    status="failed",
                )
            return self.repository.save(state, expected_version=current_version)
        finally:
            self.repository.release_transition(run_id, transition_key)

    @staticmethod
    def _has_quality_manuscript(state: RunState) -> bool:
        payload = state.artifacts.get("manuscript_package", {}).get("payload")
        if not payload:
            return False
        try:
            manuscript = ManuscriptPackage.model_validate(payload)
        except (ValueError, TypeError):
            return False
        return manuscript.mode == "full_manuscript" and manuscript.audit_result == "pass_with_no_critical_issues"

    async def decide_gate(
        self, run_id: str, gate: str, request: GateDecisionRequest
    ) -> RunState:
        state = self.repository.get(run_id)
        if request.idempotency_key in state.processed_idempotency_keys:
            return state
        expected = request.expected_run_version
        if expected is not None and expected != state.version:
            raise VersionConflictError(
                f"run {run_id} changed; expected version {expected}, actual {state.version}"
            )
        normalized_gate = gate.upper()
        if normalized_gate not in ("H1", "H2", "H3", "H4"):
            raise WorkflowTransitionError(f"unknown gate: {gate}")
        if state.status != "waiting_human" or state.current_gate != normalized_gate:
            raise WorkflowTransitionError(
                f"run is not waiting at {normalized_gate}; current gate is {state.current_gate}"
            )
        if (
            normalized_gate == "H4"
            and request.action == "revise"
            and not request.comment.strip()
        ):
            raise WorkflowTransitionError("H4 revise requires a concrete review comment")

        current_version = state.version
        self.repository.claim_transition(
            run_id,
            expected_version=current_version,
            idempotency_key=request.idempotency_key,
        )
        try:
            reviewed_hashes = self._gate_artifact_hashes(state, normalized_gate)
            if (
                request.reviewed_artifact_hashes
                and request.reviewed_artifact_hashes != reviewed_hashes
            ):
                raise WorkflowTransitionError(
                    "reviewed artifact hashes do not match the current gate artifacts"
                )
            state.processed_idempotency_keys.append(request.idempotency_key)
            decision = DecisionRecord(
                gate=normalized_gate,
                action=request.action,
                actor=request.actor,
                comment=request.comment,
                selected_candidate_id=request.selected_candidate_id,
                reviewed_hashes=reviewed_hashes,
                claim_decisions={item.claim_id: item.decision for item in request.claims},
            )
            state.decisions.append(decision)
            self._record_step(
                state,
                f"{normalized_gate.lower()}_gate",
                "succeeded",
                input_value={"reviewed_artifacts": reviewed_hashes},
                output_value=decision,
                logs=[f"{normalized_gate} 决定已记录：{request.action}。"],
            )
            state.current_gate = None

            if request.action == "reject":
                state.status = "stopped"
                state.last_error = f"{normalized_gate} 被人工拒绝。"
                self._event(state, "run.stopped", state.last_error)
                return self.repository.save(state, expected_version=current_version)
            if request.action == "revise":
                state.status = "failed" if normalized_gate == "H4" else "blocked"
                state.current_node_id = {
                    "H1": "input_validation",
                    "H2": "analysis_plan_merge",
                    "H3": "claim_ledger",
                    "H4": "scientific_writer",
                }[normalized_gate]
                state.last_error = (
                    f"{normalized_gate} 已退回；需要通过 revisions API 提交修订 Artifact。"
                )
                self._event(
                    state,
                    "run.returned",
                    state.last_error,
                    node_id=state.current_node_id,
                    status="blocked",
                )
                return self.repository.save(state, expected_version=current_version)

            try:
                if normalized_gate == "H1":
                    if request.action != "approve":
                        raise WorkflowTransitionError("H1 only accepts approve, revise or reject")
                    await self._after_h1(state)
                elif normalized_gate == "H2":
                    if request.action != "approve":
                        raise WorkflowTransitionError("H2 only accepts approve, revise or reject")
                    await self._after_h2(state, decision)
                elif normalized_gate == "H3":
                    await self._after_h3(state, request)
                else:
                    if request.action != "approve":
                        raise WorkflowTransitionError(
                            "H4 only accepts approve, revise or reject"
                        )
                    self._after_h4(state)
            except WorkflowTransitionError:
                raise
            except Exception as error:
                state.status = "failed"
                state.last_error = str(error)
                self._event(
                    state,
                    "run.failed",
                    f"运行失败：{error}",
                    node_id=state.current_node_id,
                    status="failed",
                )
            return self.repository.save(state, expected_version=current_version)
        finally:
            self.repository.release_transition(run_id, request.idempotency_key)

    @staticmethod
    def _gate_artifact_hashes(state: RunState, gate: str) -> dict[str, str]:
        keys = {
            "H1": ("research_package",),
            "H2": ("design_arena", "analysis_plan", "critic_report"),
            "H3": ("claim_ledger", "research_run"),
            "H4": ("manuscript_package",),
        }[gate]
        return {
            key: state.artifacts[key]["sha256"]
            for key in keys
            if key in state.artifacts
        }

    async def submit_revision(self, run_id: str, request: RevisionRequest) -> RunState:
        state = self.repository.get(run_id)
        if request.idempotency_key in state.processed_idempotency_keys:
            return state
        if request.expected_run_version != state.version:
            raise VersionConflictError(
                f"run {run_id} changed; expected version {request.expected_run_version}, actual {state.version}"
            )
        returned_revision = (
            state.status == "blocked"
            and bool(state.decisions)
            and state.decisions[-1].action == "revise"
            and state.decisions[-1].gate == request.gate
        )
        critic_revision = (
            state.status == "blocked"
            and request.gate == "H2"
            and state.current_node_id in {"critic_merge", "design_arena_merge"}
            and "analysis_plan" in state.artifacts
            and "critic_report" in state.artifacts
        )
        if not returned_revision and not critic_revision:
            raise WorkflowTransitionError(
                f"run is not waiting for a {request.gate} revision"
            )

        current_version = state.version
        self.repository.claim_transition(
            run_id,
            expected_version=current_version,
            idempotency_key=request.idempotency_key,
        )
        try:
            state.processed_idempotency_keys.append(request.idempotency_key)
            state.last_error = None
            state.status = "running"
            if request.gate == "H1":
                assert request.case is not None
                case = request.case
                state.case_submission = case
                state.case_id = case.case_id
                state.case_name = case.title
                self._record_step(
                    state,
                    "case_input",
                    "succeeded",
                    output_value=case,
                    logs=["H1 修订案例已提交并通过严格 CaseSubmission Schema。"],
                )
                package = self._normalize_case(state, case)
                self._put_artifact(state, "research_package", package)
                validation = {
                    "valid": not package.input_conflicts,
                    "errors": package.input_conflicts,
                    "warnings": package.missing_required_information,
                    "hidden_reference_access": "denied_by_schema",
                }
                self._record_step(
                    state,
                    "input_validation",
                    "succeeded" if validation["valid"] else "blocked",
                    input_value=package,
                    output_value=validation,
                    logs=["H1 修订输入已重新执行确定性校验。"],
                )
                if validation["valid"]:
                    self._pause_at_gate(state, "H1", package)
                else:
                    state.status = "blocked"
                    state.last_error = "修订输入仍存在冲突。"
            else:
                assert request.analysis_plan is not None
                previous = self._artifact(state, "analysis_plan", AnalysisPlan)
                plan = request.analysis_plan
                if plan.plan_version <= previous.plan_version:
                    raise WorkflowTransitionError(
                        "H2 revision must increment AnalysisPlan.plan_version"
                    )
                if plan.method_family != previous.method_family:
                    raise WorkflowTransitionError(
                        "changing method family requires returning to H1/method routing"
                    )
                self._record_step(
                    state,
                    "plan_revision",
                    "succeeded",
                    input_value={"returned_plan": previous, "human_revision": plan},
                    output_value=plan,
                    logs=["人工修订 AnalysisPlan 已提交；重新进入四类 Critic。"],
                )
                if "design_arena" in state.artifacts:
                    state.artifacts["superseded_design_arena"] = state.artifacts.pop(
                        "design_arena"
                    )
                self._put_artifact(state, "analysis_plan", plan)
                await self._review_plan(
                    state,
                    self._artifact(state, "research_package", ResearchPackage),
                    self._artifact(state, "data_profile", DataProfile),
                    self._artifact(state, "method_route", MethodRoute),
                    plan,
                )
            self._event(
                state,
                "revision.submitted",
                f"{request.gate} 修订 Artifact 已提交。",
                node_id=state.current_node_id,
            )
            return self.repository.save(state, expected_version=current_version)
        finally:
            self.repository.release_transition(run_id, request.idempotency_key)

    async def _after_h1(self, state: RunState) -> None:
        state.status = "running"
        package = self._artifact(state, "research_package", ResearchPackage)
        llm_package = self._llm_research_package(package)
        hypotheses = await self._llm_step(
            state,
            "hypothesis_decomposition",
            "hypothesis_decomposition",
            {"research_package": llm_package},
            TestableHypotheses,
        )
        self._put_artifact(state, "testable_hypotheses", hypotheses)
        profile = self._profile(package)
        self._record_step(
            state,
            "data_profile",
            "succeeded",
            input_value=package,
            output_value=profile,
            prompts=[
                {
                    "id": "data_profile:code",
                    "role": "code",
                    "template": "Deterministic dataset-reference and variable-role profiling",
                    "rendered": "Server-side code reads only the registered analysis CSV and computes descriptive integrity checks; no model is estimated.",
                }
            ],
            logs=["实际数据画像完成；样本量、字段数、主键重复与缺失率均由确定性代码计算。"],
        )
        self._put_artifact(state, "data_profile", profile)
        route_input = {
            "research_package": llm_package,
            "testable_hypotheses": hypotheses.model_dump(mode="json"),
            "data_profile": profile.model_dump(mode="json"),
        }
        route = MethodRoute.model_validate(FixtureModelGateway._route(route_input))
        self._record_step(
            state,
            "method_route",
            "succeeded",
            input_value=route_input,
            output_value=route,
            prompts=[
                {
                    "id": "method_route:code",
                    "role": "code",
                    "template": "Deterministic method-family routing from research goal and data structure",
                    "rendered": "方法路由由确定性规则完成；不会因模型输出自相矛盾而使 Run 失败。",
                }
            ],
            logs=["确定性方法路由完成；输出已通过 MethodRoute 校验。"],
        )
        self._put_artifact(state, "method_route", route)
        if route.route_status != "routed" or route.primary_route is None:
            state.status = "blocked"
            state.last_error = "方法路由没有满足条件，禁止静默回退。"
            self._event(state, "run.blocked", state.last_error, node_id="method_route", status="blocked")
            return

        selected = f"design_{route.primary_route}"
        for family in (
            "policy_causal",
            "panel_association",
            "mechanism_boundary",
            "market_event",
            "spatial",
            "measurement_efficiency",
            "structural_macro",
        ):
            node_id = f"design_{family}"
            if node_id != selected:
                self._record_step(
                    state,
                    node_id,
                    "skipped",
                    input_value=route,
                    logs=[f"互斥路由未选择 {family}。"],
                )
        envelope = self._derive_design_envelope(package, route)
        self._put_artifact(state, "design_envelope", envelope)
        candidate_set = await self._generate_design_candidates(
            state,
            selected,
            package,
            hypotheses,
            profile,
            route,
            envelope,
        )
        self._put_artifact(state, "candidate_design_set", candidate_set)
        await self._review_design_arena(
            state,
            package,
            profile,
            route,
            envelope,
            candidate_set,
        )

    @staticmethod
    def _derive_design_envelope(
        package: ResearchPackage,
        route: MethodRoute,
    ) -> DesignEnvelope:
        if package.design_envelope is not None:
            return package.design_envelope
        text = " ".join(
            [
                package.research_question,
                *package.known_policy_facts,
                *package.constraints,
            ]
        ).casefold()
        target_estimands = ["主假设对应的核心效应或关联参数"]
        if route.primary_route == "spatial":
            target_estimands = []
            if any(term in text for term in ("本地", "本省", "直接")):
                target_estimands.append("本地直接效应")
            if any(term in text for term in ("跨省", "跨地区", "邻近", "溢出", "间接")):
                target_estimands.append("跨地区间接效应")
            if any(term in text for term in ("总效应", "合计", "总体")):
                target_estimands.append("直接与间接效应合计的总效应")
            if not target_estimands:
                target_estimands.append("空间关联参数")
        allowed_strength = (
            "causal"
            if route.research_goal == "causal"
            else "associational"
            if route.research_goal in ("associational", "mechanism", "mixed")
            else "descriptive"
        )
        return DesignEnvelope(
            benchmark_track="strict_blind",
            research_goal=route.research_goal,
            target_estimands=target_estimands,
            design_constraints=package.constraints,
            required_diagnostics=route.testable_assumptions,
            allowed_claim_strength=allowed_strength,
        )

    @staticmethod
    def _llm_research_package(package: ResearchPackage) -> dict[str, Any]:
        payload = package.model_dump(mode="json")
        payload["variables"] = [
            variable.model_dump(mode="json")
            for variable in package.variables
            if variable.role != "unknown"
        ]
        return payload

    async def _generate_design_candidates(
        self,
        state: RunState,
        selected_node: str,
        package: ResearchPackage,
        hypotheses: TestableHypotheses,
        profile: DataProfile,
        route: MethodRoute,
        envelope: DesignEnvelope,
        *,
        gateway: ModelGateway | None = None,
    ) -> CandidateDesignSet:
        compact_package = {
            "case_id": package.case_id,
            "title": package.title,
            "research_question": package.research_question,
            "hypotheses": [
                item.model_dump(mode="json") for item in package.hypotheses
            ],
            "unit_of_analysis": package.unit_of_analysis,
            "sample_period": package.sample_period,
            "data_structure_hint": package.data_structure_hint,
            "variables": [
                item.model_dump(mode="json")
                for item in package.variables
                if item.role != "unknown"
            ],
            "dataset_refs": [
                item.model_dump(mode="json") for item in package.dataset_refs
            ],
            "known_policy_facts": package.known_policy_facts,
            "constraints": package.constraints,
        }
        compact_hypotheses = {
            "items": [
                {
                    "hypothesis_id": item.hypothesis_id,
                    "theoretical_claim": item.theoretical_claim,
                    "observable_prediction": item.observable_prediction,
                    "boundary_conditions": item.boundary_conditions,
                    "competing_explanations": item.competing_explanations,
                    "falsification_conditions": item.falsification_conditions,
                }
                for item in hypotheses.items
            ]
        }
        compact_profile = {
            "profile_execution_status": profile.profile_execution_status,
            "data_structure": profile.data_structure,
            "unit_of_observation": profile.unit_of_observation,
            "entity_key": profile.entity_key,
            "time_key": profile.time_key,
            "spatial_key": profile.spatial_key,
            "event_date_key": profile.event_date_key,
            "row_count": profile.row_count,
            "column_count": profile.column_count,
            "duplicate_key_count": profile.duplicate_key_count,
            "missingness": [
                item.model_dump(mode="json")
                for item in profile.missingness
                if item.missing_rate
                and any(
                    variable.name == item.variable and variable.role != "unknown"
                    for variable in package.variables
                )
            ],
            "confirmed_facts": profile.confirmed_facts,
            "measurement_risks": profile.measurement_risks,
            "merge_risks": profile.merge_risks,
            "supported_method_families": profile.supported_method_families,
            "readiness": profile.readiness,
            "blocking_reasons": profile.blocking_reasons,
        }

        async def generate(strategy: str, rationale: str) -> DesignCandidate:
            candidate_id = f"candidate-{strategy}"
            existing_step = next(
                (
                    step
                    for step in reversed(state.steps)
                    if step.node_id == selected_node
                    and step.status == "succeeded"
                    and isinstance(step.input, dict)
                    and step.input.get("candidate_strategy") == strategy
                ),
                None,
            )
            if existing_step is not None:
                plan = AnalysisPlan.model_validate(existing_step.output)
                self._record_step(
                    state,
                    "design_candidate_reuse",
                    "succeeded",
                    input_value={
                        "source_step_id": existing_step.id,
                        "candidate_strategy": strategy,
                    },
                    output_value={"candidate_id": candidate_id},
                    logs=[f"复用已通过 Schema 的候选 {candidate_id}。"],
                )
            else:
                plan = await self._llm_step(
                    state,
                    selected_node,
                    "analysis_design",
                    {
                        "candidate_id": candidate_id,
                        "candidate_strategy": strategy,
                        "candidate_rationale": rationale,
                        "design_envelope": envelope.model_dump(mode="json"),
                        "research_package": compact_package,
                        "testable_hypotheses": compact_hypotheses,
                        "data_profile": compact_profile,
                        "method_route": route.model_dump(mode="json"),
                        "design_model_policy": (
                            {
                                "tier": "escalated_after_transport_failure",
                                "model": getattr(
                                    gateway, "model", DESIGN_RETRY_MODEL
                                ),
                            }
                            if gateway is not None
                            else {
                                "tier": "default",
                                "model": state.model_provider,
                            }
                        ),
                    },
                    AnalysisPlan,
                    gateway=gateway,
                )
            plan = plan.model_copy(
                update={
                    "plan_id": f"plan-{state.case_id}-{strategy}",
                    "plan_version": 1,
                    "revision_round": 0,
                }
            )
            plan = self._bind_spatial_assets(package, plan)
            probe = self._probe_candidate(
                state,
                package,
                profile,
                route,
                envelope,
                candidate_id,
                plan,
            )
            return DesignCandidate(
                candidate_id=candidate_id,
                strategy=strategy,
                rationale=rationale,
                plan=plan,
                probe_report=probe,
            )

        candidates: list[DesignCandidate] = []
        candidate_errors: list[str] = []
        for strategy, rationale in DESIGN_STRATEGIES:
            try:
                candidates.append(await generate(strategy, rationale))
            except Exception as error:
                candidate_errors.append(f"{strategy}: {error}")
        if len(candidates) < 2:
            raise RuntimeError(
                "候选研究设计不足两个，无法进入 Reviewer Arena："
                + "；".join(candidate_errors)
            )
        candidate_set = CandidateDesignSet(
            candidate_set_id=f"candidate-set-{uuid4()}",
            candidates=candidates,
        )
        self._record_step(
            state,
            "candidate_design_set",
            "succeeded",
            input_value={"selected_method_node": selected_node},
            output_value=candidate_set,
            prompts=[
                {
                    "id": "candidate_design_set:code",
                    "role": "code",
                    "template": "Bounded candidate set with three prespecified design strategies",
                    "rendered": "三种候选策略在查看任何统计结果前生成；不按系数方向或 p 值筛选。",
                }
            ],
            logs=[
                f"{len(candidates)} 个候选研究设计已形成，进入无结果可见的 Probe 与 Reviewer Arena。"
                + (
                    f" 另有 {len(candidate_errors)} 个候选调用失败，失败记录已保留。"
                    if candidate_errors
                    else ""
                )
            ],
        )
        self._record_step(
            state,
            "probe_run",
            "succeeded",
            input_value={
                "candidate_ids": [candidate.candidate_id for candidate in candidates],
                "forbidden_inputs": ["estimate", "coefficient", "p_value", "significance"],
            },
            output_value={
                candidate.candidate_id: candidate.probe_report
                for candidate in candidates
            },
            prompts=[
                {
                    "id": "probe_run:code",
                    "role": "code",
                    "template": "Deterministic pre-result feasibility probe",
                    "rendered": "仅检查字段、结构、资产、识别条件与执行器能力；used_outcome_results 固定为 false。",
                }
            ],
            logs=["三个候选均已完成无结果可见 Probe。"],
        )
        return candidate_set

    @staticmethod
    def _spatial_model_type(model: Any) -> str | None:
        declared = str(model.parameters.get("spatial_model", "")).casefold()
        if declared in {"sdm", "sar", "sem"}:
            return declared
        estimator = model.estimator.casefold()
        if any(term in estimator for term in ("sdm", "durbin", "杜宾")):
            return "sdm"
        if any(term in estimator for term in ("sar", "spatial lag", "空间滞后")):
            return "sar"
        if any(term in estimator for term in ("sem", "spatial error", "空间误差")):
            return "sem"
        return None

    def _probe_candidate(
        self,
        state: RunState,
        package: ResearchPackage,
        profile: DataProfile,
        route: MethodRoute,
        envelope: DesignEnvelope,
        candidate_id: str,
        plan: AnalysisPlan,
    ) -> ProbeReport:
        checks: list[ProbeCheck] = []

        def add(
            check_id: str,
            status: str,
            evidence: str,
            required_follow_up: str | None = None,
        ) -> None:
            checks.append(
                ProbeCheck(
                    check_id=check_id,
                    status=status,
                    evidence=evidence,
                    required_follow_up=required_follow_up,
                )
            )

        visible_fields = {variable.name for variable in package.variables}
        missing = sorted(set(plan.required_data_fields) - visible_fields)
        add(
            "required_fields",
            "fail" if missing else "pass",
            "缺少字段：" + "、".join(missing) if missing else "计划所需字段均在安全变量字典中。",
            "删除不可用字段或补充安全可见数据。" if missing else None,
        )
        design_only_probe = plan.design_only or state.execution_mode == "fixture"
        add(
            "data_readiness",
            (
                "warn"
                if profile.readiness == "blocked" and design_only_probe
                else "fail"
                if profile.readiness == "blocked"
                else "warn"
                if profile.readiness == "partially_ready"
                else "pass"
            ),
            f"DataProfile.readiness={profile.readiness}；仅使用结构、主键、缺失与资产信息。",
            (
                "当前仅形成研究计划；接入真实数据后必须重新执行 Probe。"
                if profile.readiness == "blocked" and design_only_probe
                else "先修复 DataProfile 阻塞项。"
                if profile.readiness == "blocked"
                else None
            ),
        )
        add(
            "method_route",
            "fail" if plan.method_family != route.primary_route else "pass",
            f"候选方法家族={plan.method_family}；路由家族={route.primary_route}。",
            "候选方案不得越过 H1 后的确定性方法家族路由。"
            if plan.method_family != route.primary_route
            else None,
        )
        if not plan.baseline_models:
            add("baseline_model", "fail", "候选方案没有基准模型。", "补充可执行基准模型。")
        else:
            model = plan.baseline_models[0]
            add(
                "core_variables",
                "fail" if not model.outcome or not model.treatments_or_exposures else "pass",
                f"outcome={model.outcome or 'missing'}；exposures={model.treatments_or_exposures}。",
                "必须绑定一个结果变量和至少一个处理或暴露变量。"
                if not model.outcome or not model.treatments_or_exposures
                else None,
            )
            if profile.data_structure in ("panel", "spatial_panel"):
                fixed_effects = set(model.fixed_effects)
                entity_effect_recorded = bool(
                    fixed_effects.intersection(profile.entity_key)
                    or (
                        profile.data_structure == "spatial_panel"
                        and profile.spatial_key in fixed_effects
                    )
                )
                missing_fixed_effects = set()
                if profile.entity_key and not entity_effect_recorded:
                    missing_fixed_effects.update(profile.entity_key)
                if profile.time_key and profile.time_key not in fixed_effects:
                    missing_fixed_effects.add(profile.time_key)
                add(
                    "panel_effects",
                    "warn" if missing_fixed_effects else "pass",
                    (
                        "尚未控制面板键对应的固定效应："
                        + "、".join(sorted(missing_fixed_effects))
                        if missing_fixed_effects
                        else "候选模型已显式记录面板层级固定效应。"
                    ),
                    "由 H2 判断是否接受该识别风险。" if missing_fixed_effects else None,
                )

        executor_ready = state.execution_mode == "fixture"
        if state.execution_mode == "external":
            executor_ready = plan.method_family in {
                "panel_association",
                "mechanism_boundary",
            }
        if plan.method_family == "spatial" and plan.baseline_models:
            model = plan.baseline_models[0]
            spatial_model = self._spatial_model_type(model)
            weights_ref = next(
                (
                    item
                    for item in package.dataset_refs
                    if item.role == "supplementary"
                    and is_spatial_weights_filename(item.filename)
                ),
                None,
            )
            spatial_keys = _names(package, "spatial_id")
            add(
                "spatial_assets",
                "fail" if weights_ref is None or not spatial_keys else "pass",
                "空间权重资产或空间标识缺失。"
                if weights_ref is None or not spatial_keys
                else "空间权重 SHA256 与空间标识均已登记。",
                "补充安全可见的空间权重和对齐标识。"
                if weights_ref is None or not spatial_keys
                else None,
            )
            target_text = " ".join(
                [*envelope.target_estimands, *envelope.design_constraints]
            ).casefold()
            requires_covariate_lags = any(
                term in target_text
                for term in (
                    "解释变量空间滞后",
                    "协变量空间滞后",
                    "spatially lagged covariate",
                    "exposure spillover",
                )
            )
            requires_indirect = any(
                term in target_text
                for term in ("间接", "跨地区", "跨省", "溢出", "indirect")
            )
            spatial_status = "pass"
            spatial_follow_up = None
            if spatial_model is None:
                spatial_status = "fail"
                spatial_follow_up = "明确空间依赖来源和可识别的空间模型。"
            elif spatial_model == "sem" and requires_indirect:
                spatial_status = "fail"
                spatial_follow_up = "该目标需要可分解的跨地区效应，空间误差模型不能单独承担。"
            elif spatial_model != "sdm" and requires_covariate_lags:
                spatial_status = "fail"
                spatial_follow_up = "目标要求区分解释变量的空间滞后项，当前候选未覆盖。"
            elif spatial_model == "sar" and requires_indirect:
                spatial_status = "warn"
                spatial_follow_up = "确认仅由结果变量空间反馈产生的间接效应是否满足目标。"
            add(
                "spatial_estimands",
                spatial_status,
                f"声明的空间模型={spatial_model or 'unknown'}；目标估计量={envelope.target_estimands}。",
                spatial_follow_up,
            )
            executor_ready = state.execution_mode == "fixture" or spatial_model == "sdm"

        add(
            "executor_capability",
            "pass" if executor_ready else "fail",
            (
                "当前执行器能够执行该候选的冻结基准模型。"
                if executor_ready
                else "当前方法库尚无该候选的可审计执行器，不能在本轮静默替换方法。"
            ),
            None if executor_ready else "由 Task2 补充执行器，或选择已有能力覆盖的科学可行候选。",
        )
        verdict = (
            "fail"
            if any(check.status == "fail" for check in checks)
            else "warn"
            if any(check.status == "warn" for check in checks)
            else "pass"
        )
        return ProbeReport(
            report_id=f"probe-{candidate_id}",
            candidate_id=candidate_id,
            verdict=verdict,
            checks=checks,
            executor_ready=executor_ready,
            used_outcome_results=False,
        )

    async def _review_design_arena(
        self,
        state: RunState,
        package: ResearchPackage,
        profile: DataProfile,
        route: MethodRoute,
        envelope: DesignEnvelope,
        candidate_set: CandidateDesignSet,
    ) -> None:
        semaphore = asyncio.Semaphore(2)
        compact_package = self._llm_research_package(package)
        compact_profile = profile.model_dump(mode="json")
        visible_names = {
            variable.name for variable in package.variables if variable.role != "unknown"
        }
        compact_profile["missingness"] = [
            item
            for item in compact_profile.get("missingness", [])
            if item.get("variable") in visible_names
        ]

        async def review_dimension(dimension: str) -> DesignReviewerReport:
            node_id = f"critic_{dimension}"
            existing_step = next(
                (
                    step
                    for step in reversed(state.steps)
                    if step.node_id == node_id
                    and step.status == "succeeded"
                    and isinstance(step.input, dict)
                    and step.input.get("dimension") == dimension
                ),
                None,
            )
            if existing_step is not None:
                report = DesignReviewerReport.model_validate(existing_step.output)
                self._record_step(
                    state,
                    "design_reviewer_reuse",
                    "succeeded",
                    input_value={
                        "source_step_id": existing_step.id,
                        "dimension": dimension,
                    },
                    output_value={"dimension": dimension},
                    logs=[f"复用已通过 Schema 的 {dimension} Reviewer 报告。"],
                )
            else:
                async with semaphore:
                    report = await self._llm_step(
                        state,
                        node_id,
                        "design_reviewer",
                        {
                            "dimension": dimension,
                            "reviewer_policy": (
                                f"qwen:{REVIEWER_MODEL}:isolated-context"
                                if state.model_provider == "qwen"
                                else "fixture:isolated-context"
                            ),
                            "research_package": compact_package,
                            "design_envelope": envelope.model_dump(mode="json"),
                            "data_profile": compact_profile,
                            "method_route": route.model_dump(mode="json"),
                            "candidates": [
                                candidate.model_dump(mode="json")
                                for candidate in candidate_set.candidates
                            ],
                        },
                        DesignReviewerReport,
                        gateway=self._reviewer_gateway(state),
                    )
            if report.dimension != dimension:
                raise WorkflowTransitionError(
                    f"Reviewer dimension mismatch: expected {dimension}, got {report.dimension}"
                )
            return report

        reports = list(
            await asyncio.gather(
                *(review_dimension(dimension) for dimension in (
                    "measurement",
                    "causal",
                    "statistical",
                    "reproducibility",
                ))
            )
        )
        candidate_ids = {candidate.candidate_id for candidate in candidate_set.candidates}
        recommended: list[DesignCandidate] = []
        for candidate in candidate_set.candidates:
            reviews = [
                item
                for report in reports
                for item in report.candidate_reviews
                if item.candidate_id == candidate.candidate_id
            ]
            if len(reviews) != len(reports):
                raise WorkflowTransitionError(
                    f"Reviewer did not assess every candidate: {candidate.candidate_id}"
                )
            has_reject = any(review.verdict == "reject" for review in reviews)
            has_critical = any(
                issue.severity == "critical" and issue.status == "open"
                for review in reviews
                for issue in review.issues
            )
            if (
                candidate.probe_report.verdict != "fail"
                and candidate.probe_report.executor_ready
                and not has_reject
                and not has_critical
            ):
                recommended.append(candidate)
        if candidate_ids != {
            item.candidate_id
            for report in reports
            for item in report.candidate_reviews
        }:
            raise WorkflowTransitionError("Reviewer candidate ids do not match the candidate set")
        strategy_order = {
            strategy: index for index, (strategy, _rationale) in enumerate(DESIGN_STRATEGIES)
        }
        recommended.sort(
            key=lambda candidate: (
                candidate.probe_report.verdict != "pass",
                sum(
                    len(item.issues)
                    for report in reports
                    for item in report.candidate_reviews
                    if item.candidate_id == candidate.candidate_id
                ),
                strategy_order[candidate.strategy],
            )
        )
        provisional = recommended[0] if recommended else None
        arena = DesignArena(
            arena_id=f"design-arena-{uuid4()}",
            candidates=candidate_set.candidates,
            reviewer_reports=reports,
            recommended_candidate_ids=[item.candidate_id for item in recommended],
            provisional_candidate_id=(
                provisional.candidate_id if provisional is not None else None
            ),
            selection_rationale=[
                "Probe 只检查字段、结构、资产、识别条件与执行器能力，不读取模型结果。",
                "Reviewer 不投票决定真理；任何硬失败或 critical 问题都会淘汰候选。",
                "多个可行候选保留到 H2，由人工选择后冻结。",
            ],
        )
        self._put_artifact(state, "design_arena", arena)
        self._record_step(
            state,
            "design_arena_merge",
            "succeeded" if provisional is not None else "blocked",
            input_value={"candidate_design_set": candidate_set, "reviewer_reports": reports},
            output_value=arena,
            prompts=[
                {
                    "id": "design_arena_merge:code",
                    "role": "code",
                    "template": "Eliminate hard failures and preserve all viable candidates",
                    "rendered": "无总分、无多数投票；依据 Probe 硬约束与结构化 Reviewer 问题形成候选集。",
                }
            ],
            logs=[
                f"Reviewer Arena 完成；保留 {len(recommended)} 个可行候选。"
            ],
        )
        fallback = provisional or candidate_set.candidates[0]
        critic = self._critic_report_for_candidate(arena, fallback.candidate_id)
        self._put_artifact(state, "analysis_plan", fallback.plan)
        self._put_artifact(state, "critic_report", critic)
        self._record_step(
            state,
            "analysis_plan_merge",
            "succeeded" if provisional is not None else "blocked",
            input_value={"design_arena_id": arena.arena_id},
            output_value=fallback.plan,
            logs=[
                "已形成 H2 暂定计划；人工仍可在可行候选中选择。"
                if provisional is not None
                else "没有候选同时通过 Probe 与 Reviewer，暂定首个方案仅供人工修订。"
            ],
        )
        if provisional is None:
            state.status = "blocked"
            state.current_node_id = "design_arena_merge"
            state.last_error = "候选研究设计均存在硬失败或 critical 问题，H2 未开放。"
            self._event(
                state,
                "run.blocked",
                state.last_error,
                node_id="design_arena_merge",
                status="blocked",
            )
            return
        self._pause_at_gate(
            state,
            "H2",
            {
                "design_arena": arena.model_dump(mode="json"),
                "analysis_plan": provisional.plan.model_dump(mode="json"),
                "critic_report": critic.model_dump(mode="json"),
            },
        )

    @staticmethod
    def _critic_report_for_candidate(
        arena: DesignArena,
        candidate_id: str,
    ) -> CriticReport:
        issues = [
            issue
            for report in arena.reviewer_reports
            for review in report.candidate_reviews
            if review.candidate_id == candidate_id
            for issue in review.issues
        ]
        open_issues = [issue for issue in issues if issue.status == "open"]
        verdict = (
            "blocked"
            if any(issue.severity == "critical" for issue in open_issues)
            else "revise"
            if open_issues
            else "pass"
        )
        return CriticReport(
            report_id=f"arena-critic-{candidate_id}",
            review_round=1,
            verdict=verdict,
            issues=issues,
            approved_elements=[
                strength
                for report in arena.reviewer_reports
                for review in report.candidate_reviews
                if review.candidate_id == candidate_id
                for strength in review.strengths
            ],
            remaining_risks=[
                *[
                    risk
                    for report in arena.reviewer_reports
                    for risk in report.remaining_risks
                ],
                *[
                    follow_up
                    for report in arena.reviewer_reports
                    for review in report.candidate_reviews
                    if review.candidate_id == candidate_id
                    for follow_up in review.required_follow_ups
                ],
            ],
        )

    @staticmethod
    def _bind_spatial_assets(
        package: ResearchPackage,
        plan: AnalysisPlan,
    ) -> AnalysisPlan:
        if plan.method_family != "spatial" or not plan.baseline_models:
            return plan
        weights_ref = next(
            (
                item
                for item in package.dataset_refs
                if item.role == "supplementary"
                and is_spatial_weights_filename(item.filename)
            ),
            None,
        )
        spatial_keys = _names(package, "spatial_id")
        if weights_ref is None or not spatial_keys:
            return plan

        model = plan.baseline_models[0]
        spatial_model = WorkflowEngine._spatial_model_type(model)
        parameters = {
            **model.parameters,
            "spatial_weights_dataset_id": weights_ref.dataset_id,
            "spatial_weights_sha256": weights_ref.sha256,
            "spatial_id": spatial_keys[0],
        }
        if spatial_model is not None:
            parameters["spatial_model"] = spatial_model
        if spatial_model in {"sdm", "sar"}:
            parameters["effect_decomposition"] = ["direct", "indirect", "total"]
        if spatial_model == "sdm":
            parameters.update(
                {
                    "spatially_lagged_covariates": [
                        *model.treatments_or_exposures,
                        *model.controls,
                    ],
                }
            )
        baseline_models = [
            model.model_copy(update={"parameters": parameters}),
            *plan.baseline_models[1:],
        ]
        return plan.model_copy(
            update={
                "baseline_models": baseline_models,
                "required_data_fields": list(
                    dict.fromkeys([*plan.required_data_fields, spatial_keys[0]])
                ),
            }
        )

    @staticmethod
    def _validate_spatial_plan(
        package: ResearchPackage,
        plan: AnalysisPlan,
    ) -> None:
        if plan.method_family != "spatial":
            return
        problems: list[str] = []
        weights_ref = next(
            (
                item
                for item in package.dataset_refs
                if item.role == "supplementary"
                and is_spatial_weights_filename(item.filename)
            ),
            None,
        )
        spatial_keys = _names(package, "spatial_id")
        if weights_ref is None:
            problems.append("缺少冻结的 spatial_weights.csv")
        if not spatial_keys:
            problems.append("缺少 spatial_id 字段")
        if not plan.baseline_models:
            problems.append("缺少空间基准模型")
        else:
            model = plan.baseline_models[0]
            parameters = model.parameters
            spatial_model = WorkflowEngine._spatial_model_type(model)
            if spatial_model is None:
                problems.append("空间模型没有明确声明为 SDM、SAR 或 SEM")
            if weights_ref is not None and (
                parameters.get("spatial_weights_dataset_id") != weights_ref.dataset_id
                or parameters.get("spatial_weights_sha256") != weights_ref.sha256
            ):
                problems.append("空间权重资产 ID 或 SHA256 未绑定到 H2 合同")
            if spatial_keys and parameters.get("spatial_id") != spatial_keys[0]:
                problems.append("空间标识字段未绑定到权重矩阵")
            if spatial_model in {"sdm", "sar"} and set(
                parameters.get("effect_decomposition", [])
            ) != {"direct", "indirect", "total"}:
                problems.append("可分解空间模型未冻结直接、间接和总效应")
            if spatial_model == "sdm":
                regressors = {
                    *model.treatments_or_exposures,
                    *model.controls,
                }
                if set(parameters.get("spatially_lagged_covariates", [])) != regressors:
                    problems.append("SDM 未冻结全部解释变量的空间滞后项")
        if problems:
            raise WorkflowTransitionError(
                "H2 空间合同不完整：" + "；".join(problems)
            )

    async def _review_plan(
        self,
        state: RunState,
        package: ResearchPackage,
        profile: DataProfile,
        route: MethodRoute,
        initial_plan: AnalysisPlan,
    ) -> None:
        plan = initial_plan
        compact_package = self._llm_research_package(package)
        for round_number in (1, 2):
            gateway = self._gateway(state)

            async def review_dimension(dimension: str) -> CriticReport:
                return await self._llm_step(
                    state,
                    f"critic_{dimension}",
                    "method_critic",
                    {
                        "dimension": dimension,
                        "review_round": round_number,
                        "research_package": compact_package,
                        "data_profile": profile.model_dump(mode="json"),
                        "method_route": route.model_dump(mode="json"),
                        "analysis_plan": plan.model_dump(mode="json"),
                    },
                    CriticReport,
                    gateway=gateway,
                )

            reports = list(
                await asyncio.gather(
                    *(review_dimension(dimension) for dimension in (
                        "measurement",
                        "causal",
                        "statistical",
                        "reproducibility",
                    ))
                )
            )
            merged = _merge_critics(reports, round_number)
            self._record_step(
                state,
                "critic_merge",
                "succeeded" if merged.verdict == "pass" else "blocked",
                input_value=reports,
                output_value=merged,
                logs=[f"四类 Critic 已汇合：{merged.verdict}。"],
            )
            self._put_artifact(state, "critic_report", merged)
            open_issues = [issue for issue in merged.issues if issue.status == "open"]
            if not open_issues and merged.verdict == "pass":
                self._put_artifact(state, "analysis_plan", plan)
                self._pause_at_gate(
                    state,
                    "H2",
                    {
                        "analysis_plan": plan.model_dump(mode="json"),
                        "critic_report": merged.model_dump(mode="json"),
                    },
                )
                return
            if any(issue.severity == "critical" and issue.repair_type == "human_required" for issue in open_issues):
                state.status = "blocked"
                state.last_error = "Critic 发现必须由人工处理的 critical 问题，H2 未开放。"
                self._event(state, "run.blocked", state.last_error, node_id="critic_merge", status="blocked")
                return
            if round_number == 2:
                state.status = "blocked"
                state.last_error = "两轮有限修复后仍有未解决问题，H2 未开放。"
                self._event(state, "run.blocked", state.last_error, node_id="critic_merge", status="blocked")
                return
            plan = await self._llm_step(
                state,
                "plan_revision",
                "plan_revision",
                {
                    "analysis_plan": plan.model_dump(mode="json"),
                    "critic_report": merged.model_dump(mode="json"),
                },
                AnalysisPlan,
            )
            self._put_artifact(state, "analysis_plan", plan)

    async def _after_h2(self, state: RunState, decision: DecisionRecord) -> None:
        state.status = "running"
        package = self._artifact(state, "research_package", ResearchPackage)
        if "design_arena" in state.artifacts:
            arena = self._artifact(state, "design_arena", DesignArena)
            selected_candidate_id = (
                decision.selected_candidate_id or arena.provisional_candidate_id
            )
            if selected_candidate_id not in arena.recommended_candidate_ids:
                raise WorkflowTransitionError(
                    "H2 must select one of the Reviewer Arena recommended candidates"
                )
            selected_candidate = next(
                candidate
                for candidate in arena.candidates
                if candidate.candidate_id == selected_candidate_id
            )
            decision.selected_candidate_id = selected_candidate_id
            plan = selected_candidate.plan
            critic = self._critic_report_for_candidate(arena, selected_candidate_id)
            self._put_artifact(state, "analysis_plan", plan)
            self._put_artifact(state, "critic_report", critic)
            self._record_step(
                state,
                "design_selection",
                "succeeded",
                input_value={
                    "design_arena_id": arena.arena_id,
                    "recommended_candidate_ids": arena.recommended_candidate_ids,
                    "human_decision": decision,
                },
                output_value=selected_candidate,
                prompts=[
                    {
                        "id": "design_selection:code",
                        "role": "code",
                        "template": "Human selects one viable candidate before contract freeze",
                        "rendered": "H2 只能选择 Probe 与 Reviewer 均未淘汰的候选；选择结果写入决策记录。",
                    }
                ],
                logs=[f"H2 选择并冻结候选 {selected_candidate_id}。"],
            )
        else:
            plan = self._artifact(state, "analysis_plan", AnalysisPlan)
            critic = self._artifact(state, "critic_report", CriticReport)
        if any(issue.severity == "critical" and issue.status == "open" for issue in critic.issues):
            raise WorkflowTransitionError("H2 cannot freeze a plan with unresolved critical issues")
        self._validate_spatial_plan(package, plan)
        contract = FormalResearchContract(
            contract_id=f"contract-{uuid4()}",
            case_id=state.case_id,
            approved_at=utc_now(),
            approved_by=decision.actor,
            decision_record_id=decision.decision_id,
            research_package_hash=_hash(package),
            data_hashes=[item.sha256 for item in package.dataset_refs],
            dataset_refs=package.dataset_refs,
            approved_plan_hash=_hash(plan),
            approved_plan=plan,
            prohibited_deviations=[
                "因结果不显著而删除样本或更换因变量",
                "不留记录地修改政策时间、变量口径或主模型",
                "隐藏失败、空结果或反向结果",
            ],
            allowed_technical_repairs=["路径、类型、编码和明确的程序错误修复"],
            unresolved_risks=critic.remaining_risks,
        )
        self._record_step(
            state,
            "contract_freeze",
            "succeeded",
            input_value={"analysis_plan": plan, "decision": decision},
            output_value=contract,
            prompts=[
                {
                    "id": "contract_freeze:code",
                    "role": "code",
                    "template": "Canonical JSON SHA-256 + immutable approved plan",
                    "rendered": "The approved AnalysisPlan is embedded in FormalResearchContract.",
                }
            ],
            logs=["研究包与 H2 获批计划已计算哈希并冻结。"],
        )
        self._put_artifact(state, "formal_research_contract", contract)

        use_fixture = state.execution_mode == "fixture" or plan.design_only
        executor: ResearchExecutor = FixtureExecutor() if use_fixture else HttpResearchExecutor()
        selected_node = "fixture_executor" if use_fixture else "external_executor"
        skipped_node = "external_executor" if use_fixture else "fixture_executor"
        self._record_step(
            state,
            "execution_router",
            "succeeded",
            input_value={"execution_mode": state.execution_mode, "design_only": plan.design_only},
            output_value={"selected": selected_node},
            logs=[f"执行器路由选择 {selected_node}。"],
        )
        self._record_step(
            state,
            skipped_node,
            "skipped",
            input_value=contract,
            logs=["互斥执行器未被选择。"],
        )
        try:
            research_run = await executor.execute(contract)
        except Exception as error:
            self._record_step(
                state,
                selected_node,
                "failed",
                input_value=contract,
                logs=["执行器调用失败；没有生成或补造任何统计结果。"],
                error=str(error),
            )
            raise
        self._validate_research_run_binding(research_run, contract)
        self._record_step(
            state,
            selected_node,
            "succeeded",
            input_value=contract,
            output_value=research_run,
            logs=[f"{executor.executor_name} 返回通过 ResearchRun Schema 的结果。"],
        )
        self._record_step(
            state,
            "research_run_merge",
            "succeeded",
            input_value={selected_node: research_run},
            output_value=research_run,
            logs=["执行状态与科学状态已分别保留。"],
        )
        reproduction_audit: ReproductionAudit
        if use_fixture:
            reproduction_audit = ReproductionAudit(
                audit_id=f"reproduction-{uuid4()}",
                primary_run_id=research_run.research_run_id,
                status="not_applicable",
                differences=["Fixture 不含可复现的统计结果。"],
            )
        else:
            try:
                replication_run = await executor.execute(contract)
                self._validate_research_run_binding(replication_run, contract)
                differences = self._research_run_differences(
                    research_run,
                    replication_run,
                )
                reproduction_audit = ReproductionAudit(
                    audit_id=f"reproduction-{uuid4()}",
                    primary_run_id=research_run.research_run_id,
                    replication_run_id=replication_run.research_run_id,
                    status="diverged" if differences else "matched",
                    compared_fields=[
                        "execution_status",
                        "scientific_status",
                        "executions",
                        "deviations",
                        "failed_runs",
                        "warnings",
                    ],
                    differences=differences,
                )
                self._put_artifact(state, "replication_run", replication_run)
                self._record_step(
                    state,
                    "replication_executor",
                    "succeeded",
                    input_value=contract,
                    output_value=replication_run,
                    logs=["独立重新调用同一冻结合同；未复用主运行结果。"],
                )
            except Exception as error:
                reproduction_audit = ReproductionAudit(
                    audit_id=f"reproduction-{uuid4()}",
                    primary_run_id=research_run.research_run_id,
                    status="failed",
                    differences=[str(error)],
                )
                self._record_step(
                    state,
                    "replication_executor",
                    "failed",
                    input_value=contract,
                    error=str(error),
                    logs=["独立复现执行失败；没有用主运行结果代替。"],
                )
        self._put_artifact(state, "reproduction_audit", reproduction_audit)
        self._record_step(
            state,
            "reproduction_audit",
            (
                "succeeded"
                if reproduction_audit.status in {"matched", "not_applicable"}
                else "blocked"
            ),
            input_value={
                "primary_run_id": research_run.research_run_id,
                "replication_run_id": reproduction_audit.replication_run_id,
            },
            output_value=reproduction_audit,
            prompts=[
                {
                    "id": "reproduction_audit:code",
                    "role": "code",
                    "template": "Deterministic comparison of two executions of one frozen contract",
                    "rendered": "忽略运行 UUID；比较状态、估计、诊断、警告与偏离，数值容差为 1e-8。",
                }
            ],
            logs=[f"独立复现审计结果：{reproduction_audit.status}。"],
        )
        if reproduction_audit.status in {"diverged", "failed"}:
            state.status = "blocked"
            state.current_node_id = "reproduction_audit"
            state.last_error = "独立复现未通过，禁止进入结论生成。"
            self._event(
                state,
                "run.blocked",
                state.last_error,
                node_id="reproduction_audit",
                status="blocked",
            )
            return
        state.execution_status = research_run.execution_status
        state.scientific_status = research_run.scientific_status
        state.plan_only = research_run.fixture_only or research_run.execution_status in (
            "not_executed",
            "fixture_only",
        )
        self._put_artifact(state, "research_run", research_run)

        assessment = await self._llm_step(
            state,
            "evidence_assessment",
            "evidence_assessment",
            {
                "research_run": research_run.model_dump(mode="json"),
                "reproduction_audit": reproduction_audit.model_dump(mode="json"),
            },
            EvidenceAssessment,
        )
        self._put_artifact(state, "evidence_assessment", assessment)
        audit = await self._llm_step(
            state,
            "scientific_audit",
            "scientific_audit",
            {
                "contract": contract.model_dump(mode="json"),
                "research_run": research_run.model_dump(mode="json"),
                "reproduction_audit": reproduction_audit.model_dump(mode="json"),
                "evidence_assessment": assessment.model_dump(mode="json"),
            },
            ScientificAudit,
        )
        state.scientific_status = audit.verdict
        if audit.verdict in ("invalid", "not_evaluated"):
            state.plan_only = True
        self._put_artifact(state, "scientific_audit", audit)
        ledger = await self._llm_step(
            state,
            "claim_ledger",
            "claim_ledger",
            {
                "research_package": self._llm_research_package(package),
                "research_run": research_run.model_dump(mode="json"),
                "evidence_assessment": assessment.model_dump(mode="json"),
                "scientific_audit": audit.model_dump(mode="json"),
                "reproduction_audit": reproduction_audit.model_dump(mode="json"),
            },
            ClaimLedger,
        )
        if research_run.fixture_only:
            for claim in ledger.claims:
                if claim.evidence_status != "not_tested" or claim.allowed_strength != "prohibited":
                    raise WorkflowTransitionError("Fixture Claim must be not_tested/prohibited")
        state.claims = ledger.claims
        self._put_artifact(state, "claim_ledger", ledger)
        self._pause_at_gate(state, "H3", ledger)

    async def _after_h3(self, state: RunState, request: GateDecisionRequest) -> None:
        ledger = self._artifact(state, "claim_ledger", ClaimLedger)
        run = self._artifact(state, "research_run", ResearchRun)
        package = self._artifact(state, "research_package", ResearchPackage)
        plan = self._artifact(state, "analysis_plan", AnalysisPlan)
        decisions = {item.claim_id: item for item in request.claims}
        expected_ids = {claim.claim_id for claim in ledger.claims}

        if run.fixture_only or state.plan_only:
            if request.action != "generate_plan_only":
                raise WorkflowTransitionError("Fixture/no-execution H3 only allows generate_plan_only")
            if set(decisions) != expected_ids:
                raise WorkflowTransitionError("H3 requires one decision for every Claim")
            if any(item.decision not in ("reject", "hold") for item in decisions.values()):
                raise WorkflowTransitionError("Fixture Claim can only be rejected or held")
        elif request.action != "approve":
            raise WorkflowTransitionError("Executed research H3 requires approve or an explicit return/reject")

        approved_claims: list[dict[str, Any]] = []
        for claim in ledger.claims:
            item = decisions.get(claim.claim_id)
            if run.fixture_only or state.plan_only:
                claim.approval_status = "rejected" if item and item.decision == "reject" else "hold"
            elif item is None:
                raise WorkflowTransitionError(f"missing H3 decision for {claim.claim_id}")
            elif item.decision == "approve":
                if claim.allowed_strength == "prohibited":
                    raise WorkflowTransitionError(
                        f"prohibited Claim cannot be approved: {claim.claim_id}"
                    )
                claim.approval_status = "approved"
                claim.final_text = item.final_text or claim.claim_text
                approved_claims.append(claim.model_dump(mode="json"))
            elif item.decision == "downgrade":
                if claim.allowed_strength == "prohibited":
                    raise WorkflowTransitionError(
                        f"prohibited Claim cannot be downgraded into the manuscript: {claim.claim_id}"
                    )
                claim.approval_status = "downgraded"
                claim.final_text = item.final_text or claim.claim_text
                approved_claims.append(claim.model_dump(mode="json"))
            elif item.decision == "reject":
                claim.approval_status = "rejected"
            else:
                claim.approval_status = "hold"
            claim.human_decision_reason = item.reason if item else request.comment
        state.claims = ledger.claims
        self._put_artifact(state, "approved_claim_ledger", ledger)

        await self._finalize_manuscript(
            state,
            package,
            plan,
            run,
            approved_claims,
        )

    async def _finalize_manuscript(
        self,
        state: RunState,
        package: ResearchPackage,
        plan: AnalysisPlan,
        run: ResearchRun,
        approved_claims: list[dict[str, Any]],
        *,
        manuscript_version: int = 1,
        existing_sections: list[ManuscriptSection] | None = None,
        reuse_existing_if_valid: bool = False,
        human_review_feedback: str | None = None,
    ) -> None:
        if state.plan_only:
            writer_payload = {
                "research_package": self._llm_research_package(package),
                "analysis_plan": plan.model_dump(mode="json"),
                "research_run": run.model_dump(mode="json"),
                "approved_claims": approved_claims,
            }
            manuscript = await self._llm_step(
                state,
                "scientific_writer",
                "scientific_writer",
                writer_payload,
                ManuscriptPackage,
                gateway=FixtureModelGateway(),
            )
        else:
            writer_payload = self._writing_evidence_pack(
                state, package, plan, run, approved_claims
            )
            manuscript = await self._generate_full_manuscript(
                state,
                package,
                plan,
                run,
                approved_claims,
                writer_payload["writing_evidence_pack"],
                existing_sections=existing_sections,
                reuse_existing_if_valid=reuse_existing_if_valid,
                human_review_feedback=human_review_feedback,
            )
        manuscript.version = max(manuscript.version, manuscript_version)

        problems: list[str] = []
        if (run.fixture_only or state.plan_only) and manuscript.mode != "research_plan_only":
            problems.append("无真实执行时成果模式必须为 research_plan_only")
        if run.fixture_only and manuscript.empirical_findings_status != "prohibited_fixture":
            problems.append("Fixture 必须声明 prohibited_fixture")
        approved_ids = {item["claim_id"] for item in approved_claims}
        used_ids = {
            claim_id
            for section in manuscript.manuscript_sections
            for claim_id in section.claim_ids
        }
        if not used_ids.issubset(approved_ids):
            problems.append("成果包含未经 H3 授权的 Claim")
        allowed_run_ids = {
            run.research_run_id,
            *[execution.execution_id for execution in run.executions],
        }
        used_run_ids = {
            run_id
            for section in manuscript.manuscript_sections
            for run_id in section.run_ids
        }
        if not used_run_ids.issubset(allowed_run_ids):
            problems.append("成果引用了不存在的 ResearchRun/Execution")
        if manuscript.mode == "full_manuscript" and not approved_ids:
            problems.append("没有 H3 获批 Claim 时不得生成完整实证论文")
        for section in manuscript.manuscript_sections:
            if section.status == "not_generated":
                continue
            requires_trace = (
                manuscript.mode == "full_manuscript"
                and section.section_id in TRACEABLE_MANUSCRIPT_SECTION_IDS
            )
            if requires_trace and not section.claim_ids:
                problems.append(f"实证章节 {section.section_id} 没有 Claim 追踪信息")
            if requires_trace and not section.run_ids:
                problems.append(f"实证章节 {section.section_id} 没有 Run 追踪信息")
        manuscript.audit_result = "revise" if problems else "pass_with_no_critical_issues"
        if problems:
            manuscript.status = "needs_revision"
            manuscript.unresolved_issues.extend(problems)
        self._record_step(
            state,
            "consistency_audit",
            "blocked" if problems else "succeeded",
            input_value={"manuscript": manuscript, "approved_claim_ids": sorted(approved_ids)},
            output_value={"problems": problems, "audit_result": manuscript.audit_result},
            prompts=[
                {
                    "id": "consistency_audit:code",
                    "role": "code",
                    "template": "Deterministic claim authorization and fixture-boundary checks",
                    "rendered": "Every used claim_id must be H3-approved; fixture output must be plan-only.",
                }
            ],
            logs=["写作一致性确定性审计完成。"],
        )
        if problems:
            state.status = "failed"
            state.current_node_id = "scientific_writer"
            state.last_error = "论文初稿未通过一致性审计；可以调整写作后重试。"
            return
        self._put_artifact(state, "manuscript_package", manuscript)
        self._pause_at_gate(state, "H4", manuscript)

    def _after_h4(self, state: RunState) -> None:
        if not self._has_quality_manuscript(state) and not state.plan_only:
            raise WorkflowTransitionError(
                "H4 cannot approve a manuscript that has not passed the quality audit"
            )
        self._seal_output(state)

    def _seal_output(self, state: RunState) -> None:
        sealed = {
            "run_id": state.id,
            "seal_algorithm": "hmac-sha256",
            "contract_sha256": state.artifacts["formal_research_contract"]["sha256"],
            "analysis_plan_sha256": state.artifacts["analysis_plan"]["sha256"],
            "research_run_sha256": state.artifacts["research_run"]["sha256"],
            "claim_ledger_sha256": state.artifacts["approved_claim_ledger"]["sha256"],
            "manuscript_sha256": state.artifacts["manuscript_package"]["sha256"],
        }
        sealed["seal_sha256"] = sign_manifest(sealed)
        self._record_step(
            state,
            "complete",
            "succeeded",
            input_value=sealed,
            output_value=sealed,
            logs=["主 Run 已封存；隐藏 ReferencePackage 不在本进程中。"],
        )
        self._put_artifact(state, "sealed_output", sealed)
        state.status = "completed"
        state.current_gate = None
        state.current_node_id = "complete"
        self._event(state, "run.completed", "代码工作流已完成并封存。", node_id="complete", status="succeeded")

    async def _generate_full_manuscript(
        self,
        state: RunState,
        package: ResearchPackage,
        plan: AnalysisPlan,
        run: ResearchRun,
        approved_claims: list[dict[str, Any]],
        evidence_pack: dict[str, Any],
        *,
        existing_sections: list[ManuscriptSection] | None = None,
        reuse_existing_if_valid: bool = False,
        human_review_feedback: str | None = None,
    ) -> ManuscriptPackage:
        gateway = self._gateway(state)
        escalated_gateway: ModelGateway | None = None
        semaphore = asyncio.Semaphore(4)
        control_variable_names = [
            str(variable.get("name", ""))
            for variable in evidence_pack.get("research_context", {}).get(
                "variables", []
            )
            if variable.get("role") == "control" and variable.get("name")
        ]

        def normalize_section_text(value: str) -> str:
            normalized = (
                value.replace("残分布", "残差分布")
                .replace("SDL A", "SDLA")
                .replace("\x08eta", "β")
                .replace("回归元", "回归变量")
                .replace("极易被误判", "可能被误判")
                .replace("极易被模型误判", "可能被模型误判")
                .replace("极度接近", "接近")
                .replace("极为谨慎", "谨慎")
                .replace("极度谨慎", "谨慎")
                .replace("appropriateness", "适用性")
                .replace(
                    "不存在统计上显著的",
                    "未发现达到常用统计显著性阈值的",
                )
                .replace(
                    "在控制变量取值相同且去除个体与时间均值后",
                    "在控制企业特征并吸收企业与年份固定效应后",
                )
            )
            for variable_name in control_variable_names:
                normalized = re.sub(
                    rf"核心解释变量\s*{re.escape(variable_name)}",
                    f"控制变量 {variable_name}",
                    normalized,
                )
            return re.sub(
                r"去除个体均值(?:和|及)时间均值后",
                "去除个体均值后",
                normalized,
            )

        async def write_section(
            spec: dict[str, str],
            revision_feedback: list[str] | None = None,
        ) -> ManuscriptSection:
            nonlocal escalated_gateway
            evidence_keys = spec["evidence_keys"].split(",")
            section_spec = {
                key: value
                for key, value in spec.items()
                if key != "evidence_keys"
            }
            if not evidence_pack["writing_requirements"].get(
                "literature_evidence_provided"
            ):
                section_spec["forbidden_phrases"] = (
                    "现有研究、现有文献、参考文献惯例、参照文献、"
                    "弥补空白、鲜有研究、尚缺乏研究"
                )
            if not evidence_pack.get("research_context", {}).get(
                "known_policy_facts"
            ):
                section_spec["unsupported_background_phrases"] = [
                    "普遍存在",
                    "普遍面临",
                    "随着某项制度或关注度变化",
                    "日益成为",
                    "备受关注",
                ]
            if not evidence_pack["writing_requirements"].get("tables_provided"):
                section_spec["unavailable_assets"] = [
                    "table",
                    "figure",
                    "appendix",
                ]
            frozen_design = evidence_pack.get("frozen_design", {})
            frozen_categories = {
                "diagnostics": frozen_design.get("planned_diagnostics", []),
                "robustness": frozen_design.get("planned_robustness", []),
                "falsification": frozen_design.get("planned_falsification", []),
                "mechanisms": frozen_design.get("planned_mechanisms", []),
                "heterogeneity": frozen_design.get("planned_heterogeneity", []),
            }
            section_spec["empty_frozen_plan_categories"] = [
                category
                for category, steps in frozen_categories.items()
                if not steps
            ]
            section_spec["frozen_plan_steps"] = frozen_categories
            if not frozen_categories["mechanisms"]:
                section_spec["mechanism_evidence_status"] = (
                    "未冻结也未执行实证机制检验；可以讨论条件性的理论路径，"
                    "但基准系数方向不能验证机制，也不能把机制检验写成后续计划。"
                )
            frozen_plan_text = json.dumps(
                frozen_categories,
                ensure_ascii=False,
            )
            if "内生性" not in frozen_plan_text:
                section_spec["endogeneity_plan_status"] = (
                    "冻结计划没有单列内生性处理步骤；可以把内生性写成未解决风险，"
                    "但不能声称已有对应的冻结步骤。"
                )
            measurement_risks = evidence_pack.get("data_profile", {}).get(
                "measurement_risks",
                [],
            )
            section_spec["allowed_measurement_risks"] = measurement_risks
            if not measurement_risks:
                section_spec["measurement_risk_policy"] = (
                    "输入没有提供测量口径变迁风险；不得自行推测评级方法、"
                    "数据库口径或数据提供方在年份间发生变化。"
                )
            scientific_audit = evidence_pack.get("executed_evidence", {}).get(
                "scientific_audit",
                {},
            )
            evidence_assessment = evidence_pack.get("executed_evidence", {}).get(
                "evidence_assessment",
                {},
            )
            section_spec["allowed_unresolved_risks"] = [
                *measurement_risks,
                *frozen_design.get("alternative_explanations", []),
                *scientific_audit.get("unresolved_risks", []),
                *evidence_assessment.get("limitations", []),
                *[
                    risk
                    for claim in evidence_pack.get("authorized_claims", [])
                    for risk in claim.get("unresolved_risks", [])
                ],
            ]
            section_spec["executed_run_types"] = [
                execution.get("run_type")
                for execution in evidence_pack.get("executed_evidence", {}).get(
                    "executions", []
                )
                if execution.get("execution_status") == "succeeded"
            ]
            completed_run_counts = {
                run_type: section_spec["executed_run_types"].count(run_type)
                for run_type in (
                    "diagnostic",
                    "robustness",
                    "falsification",
                    "mechanism",
                    "heterogeneity",
                )
            }
            category_run_types = {
                "diagnostics": "diagnostic",
                "robustness": "robustness",
                "falsification": "falsification",
                "mechanisms": "mechanism",
                "heterogeneity": "heterogeneity",
            }
            section_spec["completed_frozen_plan_categories"] = [
                category
                for category, steps in frozen_categories.items()
                if steps
                and completed_run_counts[category_run_types[category]] >= len(steps)
            ]
            section_spec["pending_frozen_plan_categories"] = [
                category
                for category, steps in frozen_categories.items()
                if steps
                and completed_run_counts[category_run_types[category]] < len(steps)
            ]
            if not section_spec["pending_frozen_plan_categories"]:
                section_spec["execution_completion_policy"] = (
                    "所有非空的冻结检验类别均已完成；不得把其中任何步骤写成"
                    "尚待执行、后续执行或未来计划。未来工作只能说明超出冻结计划的"
                    "新分析需要新数据、新识别设计和另行审批。"
                )
            if not any(
                run_type in {"data_preparation", "data_cleaning", "data_merge"}
                for run_type in section_spec["executed_run_types"]
            ):
                section_spec["input_data_status"] = (
                    "输入案例包已提供预处理后的分析数据；"
                    "本系统没有数据清洗、跨库匹配、合并或变量构造的成功执行记录，"
                    "只能把成功的模型运行写成实际完成工作。"
                )
            payload = {
                "section_spec": section_spec,
                "evidence": {
                    key: evidence_pack[key]
                    for key in evidence_keys
                },
            }
            if revision_feedback:
                payload["revision_feedback"] = {
                    "instruction": "上一版未通过通用内容质量审计。请重写本节，不要只做表面替换。",
                    "problems": revision_feedback,
                }
            section_gateway = gateway
            if (
                revision_feedback
                and spec["section_id"] in WRITER_ESCALATION_SECTION_IDS
                and getattr(gateway, "provider_name", None) == "qwen"
            ):
                if escalated_gateway is None:
                    escalated_gateway = QwenModelGateway(
                        model_override=WRITER_ESCALATION_MODEL
                    )
                section_gateway = escalated_gateway
                payload["section_spec"]["writer_model_policy"] = {
                    "tier": "escalated_after_quality_failure",
                    "model": WRITER_ESCALATION_MODEL,
                }
            async with semaphore:
                section = await self._llm_step(
                    state,
                    "scientific_writer",
                    "scientific_writer_section",
                    payload,
                    ManuscriptSection,
                    gateway=section_gateway,
                )
            assert isinstance(section, ManuscriptSection)
            if section.section_id != spec["section_id"]:
                raise ValueError(
                    f"writer returned section_id={section.section_id}; "
                    f"expected {spec['section_id']}"
                )
            traceable = section.section_id in TRACEABLE_MANUSCRIPT_SECTION_IDS
            return section.model_copy(
                update={
                    "title": spec["title"],
                    "status": "generated",
                    "content_markdown": normalize_section_text(
                        section.content_markdown
                    ),
                    "claim_ids": (
                        [claim["claim_id"] for claim in approved_claims]
                        if traceable
                        else []
                    ),
                    "run_ids": [run.research_run_id] if traceable else [],
                }
            )

        approved_claim_ids = [
            claim["claim_id"]
            for claim in approved_claims
        ]
        sections = [
            section.model_copy(
                update={
                    "content_markdown": normalize_section_text(
                        section.content_markdown
                    ),
                    "claim_ids": (
                        approved_claim_ids
                        if section.section_id in TRACEABLE_MANUSCRIPT_SECTION_IDS
                        else []
                    ),
                    "run_ids": (
                        [run.research_run_id]
                        if section.section_id in TRACEABLE_MANUSCRIPT_SECTION_IDS
                        else []
                    ),
                }
            )
            for section in (existing_sections or [])
        ]
        content_problems = (
            self._manuscript_content_problems(sections, evidence_pack)
            if sections
            else []
        )
        if sections and human_review_feedback:
            content_problems.extend(
                f"{section_id} H4 人工审稿意见：{human_review_feedback}"
                for section_id in self._human_review_target_sections(
                    human_review_feedback
                )
            )
        if not sections or (not content_problems and not reuse_existing_if_valid):
            results = await asyncio.gather(
                *(write_section(spec) for spec in MANUSCRIPT_SECTION_SPECS),
                return_exceptions=True,
            )
            failures = [
                result for result in results if isinstance(result, BaseException)
            ]
            if failures:
                raise RuntimeError(
                    "论文分节写作未完成："
                    + "；".join(str(error) for error in failures)
                )
            sections = [
                result
                for result in results
                if isinstance(result, ManuscriptSection)
            ]
            content_problems = self._manuscript_content_problems(
                sections,
                evidence_pack,
            )
        for _repair_round in range(MAX_MANUSCRIPT_REPAIR_ROUNDS):
            if not content_problems:
                break
            problem_ids = {
                problem.split(" ", 1)[0]
                for problem in content_problems
            }
            repair_specs = [
                spec
                for spec in MANUSCRIPT_SECTION_SPECS
                if spec["section_id"] in problem_ids
            ]
            repairs = await asyncio.gather(
                *(
                    write_section(
                        spec,
                        [
                            problem
                            for problem in content_problems
                            if problem.startswith(spec["section_id"] + " ")
                        ],
                    )
                    for spec in repair_specs
                ),
                return_exceptions=True,
            )
            repair_failures = [
                result for result in repairs if isinstance(result, BaseException)
            ]
            if repair_failures:
                raise RuntimeError(
                    "论文分节修订未完成："
                    + "；".join(str(error) for error in repair_failures)
                )
            repaired_by_id = {
                section.section_id: section
                for section in repairs
                if isinstance(section, ManuscriptSection)
            }
            sections = [
                repaired_by_id.get(section.section_id, section)
                for section in sections
            ]
            content_problems = self._manuscript_content_problems(
                sections,
                evidence_pack,
            )
        if content_problems:
            error = ValueError("；".join(content_problems))
            self._record_step(
                state,
                "scientific_writer",
                "failed",
                input_value={
                    "generated_sections": [section.section_id for section in sections]
                },
                logs=["论文章节已生成，但通用内容质量规则未通过。"],
                error=str(error),
            )
            raise error
        scientific_audit = evidence_pack.get("executed_evidence", {}).get(
            "scientific_audit", {}
        )
        try:
            return ManuscriptPackage(
                package_id=f"manuscript-{package.case_id}",
                case_id=package.case_id,
                mode="full_manuscript",
                status="ready_for_human_review",
                research_plan_markdown=self._research_plan_markdown(package, plan),
                manuscript_sections=sections,
                empirical_findings_status="included",
                disclosures=[
                    "文献证据与正式引文待补充；当前初稿未编造参考文献。",
                    "稳健性、证伪、机制与异质性分析如未出现在 ResearchRun 中，均只是待执行计划。",
                    "实证结论仅使用 H3 授权的 Claim，并保留 execution_status 与 scientific_status 的区分。",
                ],
                unresolved_issues=list(
                    scientific_audit.get("unresolved_risks", [])
                ),
            )
        except Exception as error:
            self._record_step(
                state,
                "scientific_writer",
                "failed",
                input_value={
                    "generated_sections": [
                        {
                            "section_id": section.section_id,
                            "character_count": len(section.content_markdown.strip()),
                        }
                        for section in sections
                    ]
                },
                logs=["论文分节均已返回，但整体完整度门槛未通过。"],
                error=str(error),
            )
            raise

    @staticmethod
    def _manuscript_content_problems(
        sections: list[ManuscriptSection],
        evidence_pack: dict[str, Any],
    ) -> list[str]:
        requirements = evidence_pack.get("writing_requirements", {})
        literature_provided = bool(
            requirements.get("literature_evidence_provided")
        )
        tables_provided = bool(requirements.get("tables_provided"))
        background_facts = evidence_pack.get("research_context", {}).get(
            "known_policy_facts", []
        )
        measurement_risks = evidence_pack.get("data_profile", {}).get(
            "measurement_risks", []
        )
        panel_balance = evidence_pack.get("data_profile", {}).get(
            "panel_balance", "unknown"
        )
        frozen_design = evidence_pack.get("frozen_design", {})
        planned_mechanisms = frozen_design.get("planned_mechanisms", [])
        frozen_design_text = str(frozen_design)
        planned_falsification_text = " ".join(
            str(value)
            for value in frozen_design.get("planned_falsification", [])
        ).lower()
        research_goal = frozen_design.get("research_goal")
        scientific_status = evidence_pack.get("executed_evidence", {}).get(
            "scientific_status"
        )
        executed_records = evidence_pack.get("executed_evidence", {}).get(
            "executions", []
        )
        entity_fixed_effects = any(
            bool(record.get("diagnostic_results", {}).get("entity_fixed_effects"))
            for record in executed_records
            if isinstance(record, dict)
        )
        data_preparation_executed = any(
            record.get("execution_status") == "succeeded"
            and str(record.get("run_type", "")).lower()
            in {"data_preparation", "data_cleaning", "data_merge"}
            for record in executed_records
            if isinstance(record, dict)
        )
        robustness_executed = any(
            record.get("execution_status") == "succeeded"
            and record.get("run_type") == "robustness"
            for record in executed_records
            if isinstance(record, dict)
        )
        baseline_record = next(
            (
                record
                for record in executed_records
                if isinstance(record, dict)
                and record.get("run_type") == "baseline"
                and record.get("execution_status") == "succeeded"
            ),
            {},
        )
        baseline_diagnostics = baseline_record.get("diagnostic_results", {})
        input_row_count = evidence_pack.get("data_profile", {}).get("row_count")
        baseline_rows_used = baseline_diagnostics.get("rows_used")
        baseline_controls = {
            str(control)
            for model in frozen_design.get("baseline_models", [])
            for control in model.get("controls", [])
        }
        declared_control_fields = {
            str(variable.get("name"))
            for variable in evidence_pack.get("research_context", {}).get("variables", [])
            if variable.get("role") == "control" and variable.get("name")
        }
        baseline_control_roots = {
            field[:-2] if field.endswith("_w") else field
            for field in baseline_controls
        }
        unsupported_control_labels = {
            str(variable.get("label"))
            for variable in evidence_pack.get("research_context", {}).get("variables", [])
            if variable.get("role") == "control"
            and variable.get("name")
            and variable.get("label")
            and (
                str(variable.get("name"))[:-2]
                if str(variable.get("name")).endswith("_w")
                else str(variable.get("name"))
            )
            not in baseline_control_roots
        }
        derived_targets = {
            str(step.get("parameters", {}).get("target"))
            for step in frozen_design.get("variable_construction", [])
            if step.get("parameters", {}).get("target")
        }
        executed_run_types = {
            str(record.get("run_type"))
            for record in executed_records
            if isinstance(record, dict)
            and record.get("execution_status") == "succeeded"
        }
        planned_steps_by_run_type = {
            "diagnostic": frozen_design.get("planned_diagnostics", []),
            "robustness": frozen_design.get("planned_robustness", []),
            "falsification": frozen_design.get("planned_falsification", []),
            "mechanism": frozen_design.get("planned_mechanisms", []),
            "heterogeneity": frozen_design.get("planned_heterogeneity", []),
        }
        successful_run_counts = {
            run_type: sum(
                1
                for record in executed_records
                if isinstance(record, dict)
                and record.get("execution_status") == "succeeded"
                and record.get("run_type") == run_type
            )
            for run_type in planned_steps_by_run_type
        }
        all_frozen_steps_executed = all(
            not steps or successful_run_counts[run_type] >= len(steps)
            for run_type, steps in planned_steps_by_run_type.items()
        )
        problems: list[str] = []
        withheld_estimate_terms = requirements.get(
            "withheld_estimate_terms", []
        )
        explicit_plan_absence_markers = (
            "为空",
            "未预设",
            "未纳入",
            "未被纳入",
            "未包含",
            "未列入",
            "未单列",
            "未提供",
            "缺乏",
            "未在",
            "未冻结",
            "没有单列",
            "没有计划",
            "不存在",
            "不预设",
            "不执行",
            "不涉及",
            "不讨论",
            "不包含",
            "不会",
            "不在冻结",
            "不属于冻结",
            "不在本研究计划",
            "超出冻结计划",
            "另行审批",
            "推测性质",
        )
        for section in sections:
            content = section.content_markdown
            if (
                section.section_id == "data_variables"
                and isinstance(input_row_count, int)
                and isinstance(baseline_rows_used, int)
                and input_row_count != baseline_rows_used
            ):
                input_formats = {
                    str(input_row_count),
                    f"{input_row_count:,}",
                }
                sample_sentences = [
                    sentence
                    for sentence in re.split(r"[。！？；\n]", content)
                    if any(value in sentence for value in input_formats)
                    and re.search(r"(?:最终|进入|用于).{0,18}(?:基准|回归|有效样本)", sentence)
                ]
                if sample_sentences:
                    problems.append(
                        "data_variables 将输入总行数误写为基准模型有效样本量"
                    )
                used_formats = {
                    str(baseline_rows_used),
                    f"{baseline_rows_used:,}",
                }
                if not any(value in content for value in used_formats):
                    problems.append(
                        "data_variables 未报告基准模型删除缺失、重复键和单例后的实际样本量"
                    )
            for target in derived_targets:
                if not target or target not in content:
                    continue
                target_sentences = [
                    sentence
                    for sentence in re.split(r"[。！？；\n]", content)
                    if target in sentence
                    and re.search(r"输入(?:数据|案例包).{0,20}(?:生成|已有|保留)", sentence)
                ]
                if target_sentences:
                    problems.append(
                        f"{section.section_id} 将执行时构造字段 {target} 错写为输入数据已有"
                    )
            for run_type, marker in (
                ("diagnostic", "诊断"),
                ("robustness", "稳健性"),
                ("falsification", "证伪"),
                ("mechanism", "机制"),
                ("heterogeneity", "异质性"),
            ):
                if run_type not in executed_run_types:
                    continue
                pending_sentences = [
                    sentence
                    for sentence in re.split(r"[。！？；\n]", content)
                    if marker in sentence
                    and re.search(
                        r"(?:尚待执行|待执行|尚未执行|需完成|后续执行|计划使用|计划执行|后续.{0,18}(?:执行|使用|完成))",
                        sentence,
                    )
                    and not any(
                        negation in sentence
                        for negation in (
                            "没有尚待",
                            "并非尚待",
                            "不再待执行",
                            "暂无待执行",
                            "暂无额外待执行",
                            "无额外待执行",
                            "另行审批",
                            "新设计",
                            "新的",
                        )
                    )
                ]
                if pending_sentences:
                    problems.append(
                        f"{section.section_id} 将已成功执行的{marker}步骤写成待执行"
                    )
            controlled_terms = re.findall(
                r"(?:模型|回归).{0,16}(?:已)?控制\s*([A-Za-z][A-Za-z0-9_]*)",
                content,
            )
            for term in controlled_terms:
                if term not in baseline_controls:
                    problems.append(
                        f"{section.section_id} 声称基准模型控制了冻结计划之外的 {term}"
                    )
            for term in declared_control_fields - baseline_controls:
                escaped_term = rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])"
                unsupported_control_sentences = [
                    sentence
                    for sentence in re.split(r"[。！？；\n]", content)
                    if re.search(escaped_term, sentence)
                    and re.search(
                        r"(?:模型|回归).{0,30}(?:控制|纳入).{0,50}" + escaped_term,
                        sentence,
                    )
                    and not any(
                        marker in sentence
                        for marker in ("未控制", "没有控制", "未纳入", "不纳入", "移除")
                    )
                ]
                if unsupported_control_sentences:
                    problems.append(
                        f"{section.section_id} 声称基准模型控制了冻结计划之外的 {term}"
                    )
            for label in unsupported_control_labels:
                label_pattern = (
                    rf"(?<![A-Za-z0-9_]){re.escape(label)}(?![A-Za-z0-9_])"
                    if label.isascii()
                    else re.escape(label)
                )
                unsupported_label_sentences = [
                    sentence
                    for sentence in re.split(r"[。！？；\n]", content)
                    if re.search(label_pattern, sentence)
                    and re.search(r"(?:模型|回归).{0,40}(?:纳入|控制).{0,80}", sentence)
                    and "控制变量" in sentence
                    and not any(
                        marker in sentence
                        for marker in ("未控制", "没有控制", "未纳入", "不纳入", "移除")
                    )
                ]
                if unsupported_label_sentences:
                    problems.append(
                        f"{section.section_id} 声称基准模型控制了冻结计划之外的变量标签 {label}"
                    )
            interaction_main_effect_sentences = [
                sentence
                for sentence in re.split(r"[。！？；\n]", content)
                if "交互边界" in sentence
                and re.search(r"(?:主效应|核心解释变量).{0,24}不显著", sentence)
                and not re.search(r"交互项.{0,24}显著", sentence)
            ]
            if interaction_main_effect_sentences:
                problems.append(
                    f"{section.section_id} 用主效应显著性代替交互项判断调节边界"
                )
            contradictory_interaction_paragraphs = [
                paragraph
                for paragraph in re.split(r"\n\s*\n", content)
                if re.search(r"交互项.{0,50}显著", paragraph)
                and re.search(
                    r"(?:主效应|核心解释变量).{0,40}(?:不显著|失去.{0,8}显著性)",
                    paragraph,
                )
                and re.search(
                    r"(?:无法确认|不足以支持).{0,45}(?:调节|交互).{0,10}边界",
                    paragraph,
                )
            ]
            if contradictory_interaction_paragraphs:
                problems.append(
                    f"{section.section_id} 已承认交互项显著，却因主效应不显著否认调节边界"
                )
            if (
                section.section_id == "discussion_limitations"
                and all_frozen_steps_executed
                and re.search(r"后续可执行的检验(?:步骤)?包括", content)
            ):
                problems.append(
                    "discussion_limitations 将已完成的冻结检验整体列为后续执行"
                )
            if (
                section.section_id in TRACEABLE_MANUSCRIPT_SECTION_IDS
                and (research_goal == "associational" or scientific_status == "limited")
                and re.search(r"对应(?:提高|降低|上升|下降|减少)", content)
            ):
                problems.append(
                    f"{section.section_id} 将关联系数写成方向性变化而非对应差异"
                )
            for term in withheld_estimate_terms:
                escaped_term = rf"(?<![A-Za-z0-9_]){re.escape(str(term))}(?![A-Za-z0-9_])"
                result_marker = (
                    r"(?:系数|估计值|标准误|p\s*[=<]|p\s*值|显著|"
                    r"置信区间|直接效应|间接效应|总效应)"
                )
                unauthorized_result_pattern = re.compile(
                    rf"(?:{escaped_term}.{{0,20}}{result_marker}|"
                    rf"{result_marker}.{{0,20}}{escaped_term})",
                    flags=re.IGNORECASE,
                )
                unauthorized_result_sentences = [
                    sentence
                    for sentence in re.split(r"[。！？；\n]", content)
                    if unauthorized_result_pattern.search(sentence)
                ]
                if unauthorized_result_sentences:
                    problems.append(
                        f"{section.section_id} 写入了 H3 未授权估计项 {term}"
                    )
            certainty_content = re.sub(
                r"(?:不能|无法|未能)(?:彻底|完全)排除",
                "",
                content,
            )
            certainty_content = re.sub(
                r"(?:并非|并不|而非|不是).{0,12}必然(?:结果|关系|影响)?",
                "",
                certainty_content,
            )
            if not tables_provided and re.search(
                r"(?:表|图)\s*[0-9一二三四五六七八九十]+",
                content,
            ):
                problems.append(
                    f"{section.section_id} 引用了未提供的图表"
                )
            if not literature_provided and (
                re.search(
                    r"现有研究.{0,30}(?:多|主要|集中|缺乏|鲜有|尚未|空白)",
                    content,
                )
                or re.search(r"(?:参照|参考).{0,8}文献", content)
                or re.search(
                    r"(?:缺乏|尚无).{0,24}(?:针对|关于).{0,16}(?:直接)?(?:经验|实证|文献)证据",
                    content,
                )
            ):
                problems.append(
                    f"{section.section_id} 声称了未提供证据的文献状况"
                )
            if (
                not literature_provided
                and re.search(
                    r"遵循(?:常规|主流|通行).{0,16}(?:研究|实证).{0,12}做法",
                    content,
                )
            ):
                problems.append(
                    f"{section.section_id} 声称了未提供证据的研究惯例"
                )
            if re.search(
                r"(?:frozen_design|executed_evidence|authorized_claims|scientific_status|ResearchRun|ClaimLedger)",
                content,
                flags=re.IGNORECASE,
            ):
                problems.append(
                    f"{section.section_id} 泄露了工作流内部字段名"
                )
            if (
                section.section_id
                in {"introduction", "theory_hypotheses", "data_variables"}
                and re.search(
                    r"(?:极易|必然|一定会|保证了|有效避免|彻底排除|完全排除|显著(?:增加|降低|提升).{0,12}(?:风险|压力|可能性))",
                    certainty_content,
                )
            ):
                problems.append(
                    f"{section.section_id} 使用了无证据支撑的强确定性表述"
                )
            if re.search(r"(?:具有|达到|呈现).{0,8}较高(?:的)?精度", content):
                problems.append(
                    f"{section.section_id} 对统计精度作了无比较基准的判断"
                )
            invented_measurement_risk_sentences = [
                sentence
                for sentence in re.split(r"[。！？\n]", content)
                if re.search(
                    r"(?:评级体系|评级方法|评分体系|评分方法|数据库口径|统计口径|数据提供方|底层数据).{0,60}(?:调整|变迁|变化|改变|更新频率)|(?:得分|评分).{0,60}权重.{0,12}(?:调整|变化|改变)",
                    sentence,
                )
                and not any(
                    marker in sentence
                    for marker in (
                        "不涉及",
                        "不推断",
                        "不得推断",
                        "未提供",
                        "没有提供",
                    )
                )
            ]
            if not measurement_risks and invented_measurement_risk_sentences:
                problems.append(
                    f"{section.section_id} 增加了输入未提供的数据口径变迁风险"
                )
            if re.search(
                r"(?:企业|个体)和(?:年份|时间)层面的不随时间变化的异质性",
                content,
            ):
                problems.append(
                    f"{section.section_id} 混淆了个体固定效应与年份固定效应的含义"
                )
            if (
                not planned_mechanisms
                and re.search(
                    r"(?:(?:暂不|不再|不对).{0,8}(?:传导路径|作用渠道|理论机制).{0,4}(?:讨论|推测|分析)|(?:暂不|不再|不对).{0,8}(?:讨论|推测|分析).{0,12}(?:传导路径|作用渠道|理论机制)|机制(?:分析|路径)?.{0,30}不在.{0,20}讨论范围)",
                    content,
                )
            ):
                problems.append(
                    f"{section.section_id} 将未执行机制检验误写为不讨论理论机制"
                )
            if (
                not planned_mechanisms
                and re.search(
                    r"(?:显著为正|显著为负|系数.{0,20}(?:为正|为负)).{0,70}(?:支持|验证|证明).{0,28}(?:机制|路径|解释)",
                    content,
                )
            ):
                problems.append(
                    f"{section.section_id} 将系数方向误写为机制得到支持"
                )
            if entity_fixed_effects and re.search(
                r"(?:两家|不同)企业.{0,65}(?:相差|比较|对应)",
                content,
            ):
                problems.append(
                    f"{section.section_id} 将个体固定效应系数误写为企业间比较"
                )
            if (
                entity_fixed_effects
                and section.section_id in TRACEABLE_MANUSCRIPT_SECTION_IDS
            ):
                between_entity_sentences = [
                    sentence
                    for sentence in re.split(r"[。！？\n]", content)
                    if re.search(
                        r"(?:得分|评分|表现|水平|取值).{0,10}(?:较高|更高)的(?:企业|个体|地区).{0,50}(?:对应|具有|表现为).{0,20}(?:较低|更低|较高|更高)",
                        sentence,
                    )
                ]
                unqualified_unit_sentences = [
                    sentence
                    for sentence in re.split(r"[。！？\n]", content)
                    if re.search(
                        r"相差.{0,12}(?:单位).{0,35}(?:对应|平均相差)",
                        sentence,
                    )
                    and not any(
                        marker in sentence
                        for marker in (
                            "同一企业内",
                            "同一家",
                            "同一个体",
                            "同一主体",
                            "企业内",
                            "个体内",
                            "随时间",
                            "不同时点",
                        )
                    )
                ]
                if between_entity_sentences or unqualified_unit_sentences:
                    problems.append(
                        f"{section.section_id} 未按个体内随时间变化解释固定效应系数"
                    )
            if section.section_id == "theory_hypotheses" and not literature_provided:
                unconditional_mechanism_sentences = [
                    sentence
                    for sentence in re.split(r"[。！？\n]", content)
                    if re.search(
                        r"(?:是.{0,20}(?:关键|主要).{0,8}(?:路径|机制)|(?:减少了|降低了|提升了|改善了).{0,60}(?:使|从而|进而)|(?:使得|导致).{0,45}(?:增加|降低|减少|提升|改善))",
                        sentence,
                    )
                    and not any(
                        marker in sentence
                        for marker in ("可能", "或许", "若", "如果", "假设", "理论上")
                    )
                ]
                if unconditional_mechanism_sentences:
                    problems.append(
                        "theory_hypotheses 将无文献支持的理论机制写成既定事实"
                    )
            endogeneity_plan_claims = [
                sentence
                for sentence in re.split(r"[。！？\n]", content)
                if (
                    re.search(
                        r"(?:冻结|预设|计划).{0,55}内生性(?:处理|检验)步骤",
                        sentence,
                    )
                    or re.search(
                        r"(?:依据|按照).{0,12}冻结计划.{0,35}(?:处理|解决|缓解).{0,16}内生性",
                        sentence,
                    )
                )
                and not any(
                    marker in sentence
                    for marker in explicit_plan_absence_markers
                )
            ]
            if endogeneity_plan_claims and "内生性" not in frozen_design_text:
                problems.append(
                    f"{section.section_id} 声称冻结计划包含不存在的内生性步骤"
                )
            if "残分布" in content:
                problems.append(
                    f"{section.section_id} 存在残差分布术语缺字"
                )
            if re.search(
                r"(?:R.?\s*平方|R²|R\^2).{0,24}(?:合理|较高|较低|理想)",
                content,
                flags=re.IGNORECASE,
            ):
                problems.append(
                    f"{section.section_id} 对拟合指标作了无比较基准的价值判断"
                )
            unearned_robustness_sentences = [
                sentence
                for sentence in re.split(r"[。！？\n]", content)
                if re.search(
                    r"(?:(?:稳定|稳健|可靠).{0,6}(?:关联|结果|系数|发现|证据)|(?:关联|结果|系数|发现|证据).{0,6}(?:稳定|稳健|可靠)(?!性))",
                    sentence,
                )
                and not any(
                    marker in sentence
                    for marker in (
                        "尚不能",
                        "不能",
                        "无法",
                        "未能",
                        "尚未",
                        "未验证",
                        "待检验",
                        "待执行",
                        "有待",
                        "不得",
                        "不代表",
                    )
                )
            ]
            if not robustness_executed and unearned_robustness_sentences:
                problems.append(
                    f"{section.section_id} 在未执行稳健性检验时声称结果稳定"
                )
            overstated_within_fit_sentences = [
                sentence
                for sentence in re.split(r"[。！？\n]", content)
                if re.search(
                    r"(?:组内\s*R|Within\s+R).{0,80}(?:控制变量与固定效应|固定效应.{0,20}(?:解释|贡献))",
                    sentence,
                    flags=re.IGNORECASE,
                )
                and not any(
                    marker in sentence
                    for marker in ("不代表", "不应归因", "不能归因", "并非")
                )
            ]
            if overstated_within_fit_sentences:
                problems.append(
                    f"{section.section_id} 错误解释了固定效应模型的组内拟合指标"
                )
            if re.search(
                r"(?:组内\s*R|Within\s+R|R²).{0,80}(?:去除|扣除).{0,24}时间趋势",
                content,
                flags=re.IGNORECASE,
            ):
                problems.append(
                    f"{section.section_id} 错误扩大了组内拟合指标的含义"
                )
            if re.search(
                r"(?:组内\s*R|Within\s+R|R²).{0,90}(?:去除|扣除).{0,30}(?:时间均值|年份均值|时间效应)",
                content,
                flags=re.IGNORECASE,
            ):
                problems.append(
                    f"{section.section_id} 将组内拟合指标误写为同时去除时间均值"
                )
            overstated_residual_sentences = [
                sentence
                for sentence in re.split(r"[。！？\n]", content)
                if re.search(
                    r"残差分布.{0,70}(?:验证|确认).{0,24}模型(?:设定|假设).{0,12}(?:合理|有效|正确)",
                    sentence,
                )
                and not any(
                    marker in sentence
                    for marker in (
                        "不能",
                        "无法",
                        "不得",
                        "不应",
                        "未解决",
                        "尚未解决",
                    )
                )
            ]
            if overstated_residual_sentences:
                problems.append(
                    f"{section.section_id} 夸大了残差分布检查的诊断能力"
                )
            if re.search(
                r"(?:确保|保证).{0,24}(?:推断|检验).{0,16}(?:可靠|有效|正确)",
                content,
            ):
                problems.append(
                    f"{section.section_id} 将标准误处理写成保证推断可靠"
                )
            overstated_endogeneity_sentences = [
                sentence
                for sentence in re.split(r"[。！？\n]", content)
                if re.search(
                    r"(?:稳健性|证伪|检验).{0,55}(?:剥离|消除|解决).{0,16}内生性",
                    sentence,
                )
                and not any(
                    marker in sentence
                    for marker in (
                        "不能",
                        "无法",
                        "不得",
                        "不应",
                        "未解决",
                        "尚未解决",
                    )
                )
            ]
            if overstated_endogeneity_sentences:
                problems.append(
                    f"{section.section_id} 夸大了稳健性或证伪检验对内生性的作用"
                )
            if not background_facts and re.search(
                r"(?:评级|评分).{0,28}(?:用作|作为).{0,12}(?:抵押|信用增级)",
                content,
            ):
                problems.append(
                    f"{section.section_id} 增加了输入未提供的融资工具安排"
                )
            if re.search(
                r"(?:剔除|删除|排除).{0,24}(?:个体|企业|年份|时间).{0,12}固定效应",
                content,
            ):
                problems.append(
                    f"{section.section_id} 将控制固定效应错写为剔除固定效应"
                )
            if (
                section.section_id == "introduction"
                and re.search(
                    r"(?:引言|本节)(?:部分)?的(?:核心)?任务(?:在于|是)",
                    content,
                )
            ):
                problems.append(
                    "introduction 泄露了写作任务元叙述"
                )
            execution_claim_content = re.sub(
                r"基于(?:已)?预处理后的",
                "基于输入的",
                content,
            )
            invented_data_preparation_sentences = [
                sentence
                for sentence in re.split(r"[。！？；\n]", execution_claim_content)
                if re.search(
                    r"(?:本稿|本文|本研究|本系统).{0,40}(?:完成|已(?:经)?).{0,70}(?:清理|清洗|匹配|合并|预处理|变量构造|缩尾)",
                    sentence,
                )
            ]
            if not data_preparation_executed and invented_data_preparation_sentences:
                problems.append(
                    f"{section.section_id} 将输入数据准备误写为本系统已执行"
                )
            if not data_preparation_executed and re.search(
                r"本系统.{0,24}(?:验证|核验).{0,45}(?:原始值|处理值|缩尾边界|对应关系)",
                content,
            ):
                problems.append(
                    f"{section.section_id} 声称执行了没有运行记录的数据核验"
                )
            if not planned_mechanisms:
                future_mechanism_sentences = [
                    sentence
                    for sentence in re.split(r"[。！？\n]", content)
                    if "机制" in sentence
                    and any(
                        marker in sentence
                        for marker in (
                            "后续",
                            "计划",
                            "冻结",
                            "待执行",
                            "尚未执行",
                            "优先执行",
                            "将进一步",
                        )
                    )
                    and not any(
                        marker in sentence
                        for marker in explicit_plan_absence_markers
                    )
                ]
                if future_mechanism_sentences:
                    problems.append(
                        f"{section.section_id} 把未冻结的机制分析写成后续计划"
                    )
            unplanned_method_patterns = (
                ("工具变量", r"(?:工具变量|instrumental\s+variables?|\b2SLS\b|\bIV\b)"),
                ("倾向得分", r"(?:倾向得分|\bPSM\b)"),
                ("双重差分", r"(?:双重差分|\bDID\b)"),
                ("广义矩估计", r"(?:广义矩|\bGMM\b)"),
                ("断点回归", r"(?:断点回归|\bRDD\b)"),
                ("合成控制", r"合成控制"),
                ("空间计量", r"(?:空间计量|空间杜宾|\bSDM\b|\bSAR\b)"),
                ("中介检验", r"(?:中介检验|中介效应)"),
                ("门槛模型", r"门槛模型"),
                ("安慰剂检验", r"安慰剂"),
            )
            for method_name, pattern in unplanned_method_patterns:
                if re.search(pattern, frozen_design_text, flags=re.IGNORECASE):
                    continue
                for sentence in re.split(r"[。！？\n]", content):
                    if not re.search(pattern, sentence, flags=re.IGNORECASE):
                        continue
                    if any(
                        marker in sentence
                        for marker in explicit_plan_absence_markers + (
                            "另行审批",
                            "新设计获批",
                            "不报告",
                        )
                    ):
                        continue
                    problems.append(
                        f"{section.section_id} 擅自加入冻结计划之外的{method_name}"
                    )
                    break
            has_planned_lead = (
                "lead" in planned_falsification_text
                or "领先" in planned_falsification_text
                or "超前" in planned_falsification_text
            )
            has_planned_lag = (
                "lag" in planned_falsification_text
                or "滞后" in planned_falsification_text
            )
            if "滞后项" in content and not has_planned_lag:
                problems.append(
                    f"{section.section_id} 擅自把冻结的时间检验扩展为滞后项"
                )
            if (
                ("领先项" in content or "超前项" in content)
                and not has_planned_lead
            ):
                problems.append(
                    f"{section.section_id} 擅自把冻结的时间检验扩展为领先项"
                )
            if (
                section.section_id in TRACEABLE_MANUSCRIPT_SECTION_IDS
                and (research_goal == "associational" or scientific_status == "limited")
                and re.search(
                    r"(?:每|当).{0,20}(?:提高|提升|增加|上升|下降|降低).{0,55}(?:提高|提升|增加|上升|下降|降低|减少).{0,24}(?:单位|%|百分点)",
                    content,
                )
            ):
                problems.append(
                    f"{section.section_id} 将关联系数写成了单位变化的因果效果"
                )
            if (
                section.section_id == "introduction"
                and not background_facts
                and re.search(
                    r"(?:普遍存在|普遍面临|日益成为|备受.{0,8}关注|随着.{0,20}(?:强化|发展|提高|增加|提升|完善|推进|演进|普及))",
                    content,
                )
            ):
                problems.append(
                    "introduction 声称了未提供证据的市场或学界趋势"
                )
            if "不随时间变化但随时间演变" in content:
                problems.append(
                    f"{section.section_id} 对时变因素作了自相矛盾的描述"
                )
            if (
                panel_balance == "unbalanced"
                and re.search(r"(?<!非)平衡面板", content)
            ):
                problems.append(
                    f"{section.section_id} 将真实非平衡面板错写为平衡面板"
                )
            if panel_balance == "balanced" and "非平衡面板" in content:
                problems.append(
                    f"{section.section_id} 将真实平衡面板错写为非平衡面板"
                )
        return problems

    @staticmethod
    def _human_review_target_sections(comment: str) -> list[str]:
        normalized = comment.casefold()
        keywords = {
            "abstract": ("摘要", "abstract"),
            "introduction": ("引言", "introduction"),
            "theory_hypotheses": ("理论", "假设", "theory", "hypoth"),
            "data_variables": ("数据", "变量", "data", "variable"),
            "research_design": ("研究设计", "方法", "design", "method"),
            "empirical_results": ("实证", "结果", "result"),
            "discussion_limitations": ("讨论", "局限", "discussion", "limitation"),
            "conclusion": ("结论", "conclusion"),
        }
        targets = [
            section_id
            for section_id, markers in keywords.items()
            if any(marker in normalized for marker in markers)
        ]
        return targets or list(FULL_MANUSCRIPT_SECTION_IDS)

    @staticmethod
    def _research_plan_markdown(
        package: ResearchPackage,
        plan: AnalysisPlan,
    ) -> str:
        def names(items: list[Any]) -> str:
            values = [item.name for item in items]
            return "、".join(values) if values else "本轮未预设"

        return (
            f"# {package.title}：后续研究计划\n\n"
            f"## 研究问题\n{package.research_question}\n\n"
            f"## 冻结基准设计\n方法家族：{plan.method_family}。"
            f"基准模型：{names(plan.baseline_models)}。\n\n"
            f"## 待执行检验\n诊断：{names(plan.diagnostics)}。\n"
            f"稳健性：{names(plan.robustness_tests)}。\n"
            f"证伪：{names(plan.falsification_tests)}。\n"
            f"机制：{names(plan.mechanism_tests)}。\n"
            f"异质性：{names(plan.heterogeneity_tests)}。\n\n"
            "## 执行原则\n保持 H2 冻结的样本、变量和模型定义；"
            "任何偏离都需要记录，不得因显著性改变分析。"
        )

    @staticmethod
    def _writing_evidence_pack(
        state: RunState,
        package: ResearchPackage,
        plan: AnalysisPlan,
        run: ResearchRun,
        approved_claims: list[dict[str, Any]],
    ) -> dict[str, Any]:
        data_profile = state.artifacts.get("data_profile", {}).get("payload", {})
        method_route = state.artifacts.get("method_route", {}).get("payload", {})
        evidence_assessment = state.artifacts.get("evidence_assessment", {}).get("payload", {})
        scientific_audit = state.artifacts.get("scientific_audit", {}).get("payload", {})

        def remove_legacy_warning(values: list[Any]) -> list[Any]:
            return [
                value
                for value in values
                if value != LEGACY_OVERBROAD_EXECUTION_WARNING
            ]

        evidence_assessment = {
            **evidence_assessment,
            "limitations": remove_legacy_warning(
                evidence_assessment.get("limitations", [])
            ),
        }
        scientific_audit = {
            **scientific_audit,
            "unresolved_risks": remove_legacy_warning(
                scientific_audit.get("unresolved_risks", [])
            ),
        }
        approved_claims = [
            {
                **claim,
                "unresolved_risks": remove_legacy_warning(
                    claim.get("unresolved_risks", [])
                ),
            }
            for claim in approved_claims
        ]
        diagnostics = (
            run.executions[0].diagnostic_results
            if run.executions
            else {}
        )
        authorized_claim_text = "\n".join(
            str(claim.get("final_text") or claim.get("claim_text") or "")
            for claim in approved_claims
        )

        variable_labels = {
            variable.name: variable.label
            for variable in package.variables
        }

        def mentions_term(term: str, *, allow_label: bool = True) -> bool:
            exact_match = bool(
                re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
                    authorized_claim_text,
                    flags=re.IGNORECASE,
                )
            )
            if exact_match or not allow_label:
                return exact_match
            base_term = term.split(":", 1)[-1]
            label = variable_labels.get(base_term, "").strip()
            return bool(label and label in authorized_claim_text)

        marginal_terms = {
            str(estimate.get("term", ""))
            for execution in run.executions
            for estimate in execution.estimates
            if estimate.get("estimate_type") == "average_marginal_effect"
        }
        effect_markers = {
            "direct": ("直接", "direct"),
            "indirect": ("间接", "indirect", "溢出"),
            "total": ("总效应", "总关联", "direct and indirect", "total"),
        }

        def estimate_is_authorized(estimate: dict[str, Any]) -> bool:
            term = str(estimate.get("term", ""))
            if not term:
                return False
            if term.casefold() == "rho":
                return True
            if estimate.get("estimate_type") == "average_marginal_effect":
                effect_type = str(estimate.get("effect_type", ""))
                return mentions_term(term) and any(
                    marker.casefold() in authorized_claim_text.casefold()
                    for marker in effect_markers.get(effect_type, (effect_type,))
                    if marker
                )
            if term in marginal_terms:
                return False
            if term.startswith("W:"):
                return mentions_term(term, allow_label=False)
            return mentions_term(term)

        execution_payloads: list[dict[str, Any]] = []
        all_estimate_terms: set[str] = set()
        authorized_estimate_terms: set[str] = set()
        for execution in run.executions:
            payload = execution.model_dump(mode="json")
            estimates = payload.get("estimates", [])
            all_estimate_terms.update(
                str(estimate.get("term", ""))
                for estimate in estimates
                if estimate.get("term")
            )
            payload["estimates"] = [
                estimate
                for estimate in estimates
                if estimate_is_authorized(estimate)
            ]
            authorized_estimate_terms.update(
                str(estimate.get("term", ""))
                for estimate in payload["estimates"]
                if estimate.get("term")
            )
            execution_payloads.append(payload)
        entity_count = diagnostics.get("entity_count")
        time_count = diagnostics.get("time_period_count")
        rows_used = diagnostics.get("rows_used")
        panel_balance = "unknown"
        if all(isinstance(value, int) for value in (entity_count, time_count, rows_used)):
            panel_balance = (
                "balanced"
                if rows_used == entity_count * time_count
                else "unbalanced"
            )
        return {
            "writing_evidence_pack": {
                "research_context": {
                    "case_id": package.case_id,
                    "title": package.title,
                    "research_question": package.research_question,
                    "hypotheses": [item.model_dump(mode="json") for item in package.hypotheses],
                    "unit_of_analysis": package.unit_of_analysis,
                    "sample_period": package.sample_period,
                    "data_structure": package.data_structure_hint,
                    "variables": [
                        variable.model_dump(mode="json")
                        for variable in package.variables
                        if variable.role != "unknown"
                    ],
                    "field_inventory": {
                        "total_fields_registered": len(package.variables),
                        "fields_sent_to_writer": len(
                            [
                                variable
                                for variable in package.variables
                                if variable.role != "unknown"
                            ]
                        ),
                    },
                    "known_policy_facts": package.known_policy_facts,
                    "constraints": package.constraints,
                },
                "data_profile": {
                    **{
                        key: data_profile.get(key)
                        for key in (
                            "profile_execution_status",
                            "row_count",
                            "column_count",
                            "entity_key",
                            "time_key",
                            "duplicate_key_count",
                            "missingness",
                            "confirmed_facts",
                            "measurement_risks",
                            "readiness",
                        )
                    },
                    "panel_balance": panel_balance,
                },
                "frozen_design": {
                    "plan_id": plan.plan_id,
                    "plan_version": plan.plan_version,
                    "method_family": plan.method_family,
                    "research_goal": method_route.get("research_goal"),
                    "sample_rules": [step.model_dump(mode="json") for step in plan.sample_rules],
                    "variable_construction": [
                        step.model_dump(mode="json") for step in plan.variable_construction
                    ],
                    "baseline_models": [
                        model.model_dump(mode="json") for model in plan.baseline_models
                    ],
                    "planned_diagnostics": [step.name for step in plan.diagnostics],
                    "planned_robustness": [step.name for step in plan.robustness_tests],
                    "planned_falsification": [step.name for step in plan.falsification_tests],
                    "planned_mechanisms": [step.name for step in plan.mechanism_tests],
                    "planned_heterogeneity": [step.name for step in plan.heterogeneity_tests],
                    "identification_assumptions": plan.identification_assumptions,
                    "alternative_explanations": plan.alternative_explanations,
                    "unsupported_analyses": plan.unsupported_requested_analyses,
                },
                "executed_evidence": {
                    "research_run_id": run.research_run_id,
                    "execution_status": run.execution_status,
                    "scientific_status": run.scientific_status,
                    "executions": [
                        execution for execution in execution_payloads
                    ],
                    "deviations": run.deviations,
                    "failed_runs": run.failed_runs,
                    "warnings": remove_legacy_warning(run.warnings),
                    "evidence_assessment": evidence_assessment,
                    "scientific_audit": scientific_audit,
                    "reproduction_audit": state.artifacts.get(
                        "reproduction_audit", {}
                    ).get("payload"),
                },
                "authorized_claims": approved_claims,
                "writing_requirements": {
                    "language": "zh-CN",
                    "required_section_ids": list(FULL_MANUSCRIPT_SECTION_IDS),
                    "target_total_characters": "4000-7000",
                    "literature_evidence_provided": False,
                    "tables_provided": False,
                    "forbid_unverified_citations": True,
                    "forbid_unexecuted_results": True,
                    "authorized_estimate_terms": sorted(
                        authorized_estimate_terms
                    ),
                    "withheld_estimate_terms": sorted(
                        all_estimate_terms - authorized_estimate_terms
                    ),
                },
            }
        }

    @staticmethod
    def _validate_research_run_binding(
        research_run: ResearchRun,
        contract: FormalResearchContract,
    ) -> None:
        mismatches: list[str] = []
        if research_run.case_id != contract.case_id:
            mismatches.append("case_id")
        if research_run.contract_hash != contract.approved_plan_hash:
            mismatches.append("contract_hash")
        if research_run.plan_version != contract.approved_plan.plan_version:
            mismatches.append("plan_version")
        if mismatches:
            raise ValueError(
                "ResearchRun does not match the frozen contract: "
                + ", ".join(mismatches)
            )

    @staticmethod
    def _research_run_differences(
        primary: ResearchRun,
        replication: ResearchRun,
        *,
        tolerance: float = 1e-8,
    ) -> list[str]:
        def comparable(run: ResearchRun) -> dict[str, Any]:
            payload = run.model_dump(mode="json")
            payload.pop("research_run_id", None)
            for execution in payload.get("executions", []):
                execution.pop("execution_id", None)
            return payload

        differences: list[str] = []

        def compare(left: Any, right: Any, path: str) -> None:
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                if abs(float(left) - float(right)) > tolerance:
                    differences.append(f"{path}: {left} != {right}")
                return
            if type(left) is not type(right):
                differences.append(
                    f"{path}: type {type(left).__name__} != {type(right).__name__}"
                )
                return
            if isinstance(left, dict):
                if set(left) != set(right):
                    differences.append(f"{path}: keys differ")
                    return
                for key in sorted(left):
                    compare(left[key], right[key], f"{path}.{key}")
                return
            if isinstance(left, list):
                if len(left) != len(right):
                    differences.append(f"{path}: length {len(left)} != {len(right)}")
                    return
                for index, (left_item, right_item) in enumerate(zip(left, right)):
                    compare(left_item, right_item, f"{path}[{index}]")
                return
            if left != right:
                differences.append(f"{path}: {left!r} != {right!r}")

        compare(comparable(primary), comparable(replication), "research_run")
        return differences


def _names(package: ResearchPackage, role: str) -> list[str]:
    return [variable.name for variable in package.variables if variable.role == role]


def _read_profile_csv(path: Any, selected_columns: list[str]) -> tuple[pd.DataFrame, int]:
    last_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            header = pd.read_csv(path, encoding=encoding, nrows=0)
            frame = pd.read_csv(
                path,
                encoding=encoding,
                usecols=lambda name: name in selected_columns,
            )
            return frame, len(header.columns)
        except UnicodeDecodeError as error:
            last_error = error
    raise ValueError("CSV 编码必须是 UTF-8 或 GB18030。") from last_error


def _verify_dataset_hash(path: Path, expected_sha256: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise ValueError("数据资产 SHA256 与登记值不一致。")


def _merge_critics(reports: list[CriticReport], round_number: int) -> CriticReport:
    issues: list[CriticIssue] = [issue for report in reports for issue in report.issues]
    open_issues = [issue for issue in issues if issue.status == "open"]
    if any(issue.severity == "critical" for issue in open_issues):
        verdict = "blocked"
    elif open_issues:
        verdict = "revise"
    else:
        verdict = "pass"
    return CriticReport(
        report_id=f"critic-merged-{round_number}",
        review_round=round_number,
        verdict=verdict,
        issues=issues,
        approved_elements=[item for report in reports for item in report.approved_elements],
        remaining_risks=[item for report in reports for item in report.remaining_risks],
    )

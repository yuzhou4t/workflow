from __future__ import annotations

import asyncio
import json
import re
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
    ClaimLedger,
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
    ResearchPackage,
    ResearchRun,
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
        "evidence_keys": "research_context,data_profile,frozen_design,writing_requirements",
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
            source = self.dataset_registry.resolve(package.dataset_refs[0])
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
        if package.data_structure_hint == "panel" and (not entity_keys or not time_keys):
            blocking_reasons.append("面板数据缺少实体或时间主键。")

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
        if not failed_writer and not completed_draft:
            raise WorkflowTransitionError("当前 Run 不在可重试的论文写作状态。")

        current_version = state.version
        transition_key = f"retry-writer-{uuid4()}"
        self.repository.claim_transition(
            run_id,
            expected_version=current_version,
            idempotency_key=transition_key,
        )
        try:
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
        if normalized_gate not in ("H1", "H2", "H3"):
            raise WorkflowTransitionError(f"unknown gate: {gate}")
        if state.status != "waiting_human" or state.current_gate != normalized_gate:
            raise WorkflowTransitionError(
                f"run is not waiting at {normalized_gate}; current gate is {state.current_gate}"
            )

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
                state.status = "blocked"
                state.current_node_id = {
                    "H1": "input_validation",
                    "H2": "analysis_plan_merge",
                    "H3": "claim_ledger",
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
                else:
                    await self._after_h3(state, request)
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
            "H2": ("analysis_plan", "critic_report"),
            "H3": ("claim_ledger", "research_run"),
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
            and state.current_node_id == "critic_merge"
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
        hypotheses = await self._llm_step(
            state,
            "hypothesis_decomposition",
            "hypothesis_decomposition",
            {"research_package": package.model_dump(mode="json")},
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
            "research_package": package.model_dump(mode="json"),
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
        plan = await self._llm_step(
            state,
            selected,
            "analysis_design",
            {
                "research_package": package.model_dump(mode="json"),
                "testable_hypotheses": hypotheses.model_dump(mode="json"),
                "data_profile": profile.model_dump(mode="json"),
                "method_route": route.model_dump(mode="json"),
            },
            AnalysisPlan,
        )
        self._record_step(
            state,
            "analysis_plan_merge",
            "succeeded",
            input_value={selected: plan.model_dump(mode="json")},
            output_value=plan,
            logs=["唯一命中的方法分支已汇合为 AnalysisPlan。"],
        )
        self._put_artifact(state, "analysis_plan", plan)
        await self._review_plan(state, package, profile, route, plan)

    async def _review_plan(
        self,
        state: RunState,
        package: ResearchPackage,
        profile: DataProfile,
        route: MethodRoute,
        initial_plan: AnalysisPlan,
    ) -> None:
        plan = initial_plan
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
                        "research_package": package.model_dump(mode="json"),
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
        plan = self._artifact(state, "analysis_plan", AnalysisPlan)
        critic = self._artifact(state, "critic_report", CriticReport)
        if any(issue.severity == "critical" and issue.status == "open" for issue in critic.issues):
            raise WorkflowTransitionError("H2 cannot freeze a plan with unresolved critical issues")
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
            {"research_run": research_run.model_dump(mode="json")},
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
                "research_package": package.model_dump(mode="json"),
                "research_run": research_run.model_dump(mode="json"),
                "evidence_assessment": assessment.model_dump(mode="json"),
                "scientific_audit": audit.model_dump(mode="json"),
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
    ) -> None:
        if state.plan_only:
            writer_payload = {
                "research_package": package.model_dump(mode="json"),
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
    ) -> ManuscriptPackage:
        gateway = self._gateway(state)
        escalated_gateway: ModelGateway | None = None
        semaphore = asyncio.Semaphore(4)

        def normalize_section_text(value: str) -> str:
            normalized = (
                value.replace("残分布", "残差分布")
                .replace("SDL A", "SDLA")
                .replace("\x08eta", "β")
                .replace("回归元", "回归变量")
                .replace(
                    "在控制变量取值相同且去除个体与时间均值后",
                    "在控制企业特征并吸收企业与年份固定效应后",
                )
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
        problems: list[str] = []
        explicit_plan_absence_markers = (
            "为空",
            "未预设",
            "未纳入",
            "未被纳入",
            "未包含",
            "未列入",
            "未单列",
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
            "推测性质",
        )
        for section in sections:
            content = section.content_markdown
            certainty_content = re.sub(
                r"(?:不能|无法|未能)(?:彻底|完全)排除",
                "",
                content,
            )
            certainty_content = re.sub(
                r"(?:并非|而非|不是).{0,12}必然(?:结果|关系|影响)?",
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
                    r"(?:缺乏|尚无).{0,24}(?:直接)?(?:经验|实证|文献)证据",
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
            endogeneity_plan_claims = [
                sentence
                for sentence in re.split(r"[。！？\n]", content)
                if re.search(
                    r"(?:冻结|预设|计划).{0,55}内生性(?:处理|检验)步骤",
                    sentence,
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
            if re.search(
                r"(?:组内\s*R|Within\s+R).{0,80}(?:控制变量与固定效应|固定效应.{0,20}(?:解释|贡献))",
                content,
                flags=re.IGNORECASE,
            ):
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
                    for marker in ("不能", "无法", "不得", "不应")
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
                    for marker in ("不能", "无法", "不得", "不应")
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
                    "variables": [variable.model_dump(mode="json") for variable in package.variables],
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
                        execution.model_dump(mode="json") for execution in run.executions
                    ],
                    "deviations": run.deviations,
                    "failed_runs": run.failed_runs,
                    "warnings": remove_legacy_warning(run.warnings),
                    "evidence_assessment": evidence_assessment,
                    "scientific_audit": scientific_audit,
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

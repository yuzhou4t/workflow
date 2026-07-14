from __future__ import annotations

from typing import Any
from uuid import uuid4

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
    GateDecisionRequest,
    ManuscriptPackage,
    MethodRoute,
    PromptContent,
    ResearchPackage,
    ResearchRun,
    RevisionRequest,
    RunEvent,
    RunState,
    ScientificAudit,
    StepAttempt,
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


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _hash(value: Any) -> str:
    return canonical_sha256(_plain(value))


class WorkflowEngine:
    def __init__(self, repository: RunRepository) -> None:
        self.repository = repository

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

    @staticmethod
    def _profile(package: ResearchPackage) -> DataProfile:
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
        return DataProfile(
            profile_execution_status="partially_succeeded" if has_refs else "not_executed",
            data_structure=package.data_structure_hint,
            unit_of_observation=package.unit_of_analysis,
            entity_key=entity_keys,
            time_key=time_keys[0] if time_keys else None,
            spatial_key=spatial_keys[0] if spatial_keys else None,
            event_date_key=event_keys[0] if event_keys else None,
            row_count=None,
            column_count=None,
            duplicate_key_count=None,
            missingness=[],
            confirmed_facts=[
                f"案例声明的数据结构为 {package.data_structure_hint}。",
                f"变量字典包含 {len(package.variables)} 个字段。",
            ],
            measurement_risks=["尚未读取实际数据，缺失率、重复键与异常值仍待外部执行器诊断。"],
            merge_risks=[],
            supported_method_families=supported,
            unsupported_method_families=[],
            readiness="partially_ready" if has_refs else "partially_ready",
            blocking_reasons=([] if has_refs else ["没有可执行数据资产；仅允许形成研究计划。"]),
        )

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
        package = await self._llm_step(
            state,
            "intake_agent",
            "intake",
            {"case": case.model_dump(mode="json")},
            ResearchPackage,
        )
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

    async def advance(self, run_id: str) -> RunState:
        state = self.repository.get(run_id)
        if state.status == "waiting_human":
            return state
        if state.status in ("completed", "stopped"):
            return state
        raise WorkflowTransitionError(
            "该 Run 不能无条件继续；请在当前人工闸门作决定，或修订导致阻塞的输入。"
        )

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
        if state.status != "blocked" or not state.decisions:
            raise WorkflowTransitionError("run is not waiting for a returned revision")
        returned = state.decisions[-1]
        if returned.action != "revise" or returned.gate != request.gate:
            raise WorkflowTransitionError(
                f"latest return is not a {request.gate} revision request"
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
                package = await self._llm_step(
                    state,
                    "intake_agent",
                    "intake",
                    {"case": case.model_dump(mode="json")},
                    ResearchPackage,
                )
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
                    "rendered": "No statistical values are inferred without opening an actual dataset.",
                }
            ],
            logs=["数据画像完成；无法确认的统计量保持 null/not_executed。"],
        )
        self._put_artifact(state, "data_profile", profile)
        route = await self._llm_step(
            state,
            "method_route",
            "method_route",
            {
                "research_package": package.model_dump(mode="json"),
                "testable_hypotheses": hypotheses.model_dump(mode="json"),
                "data_profile": profile.model_dump(mode="json"),
            },
            MethodRoute,
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
            reports: list[CriticReport] = []
            for dimension in ("measurement", "causal", "statistical", "reproducibility"):
                report = await self._llm_step(
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
                )
                reports.append(report)
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

        manuscript = await self._llm_step(
            state,
            "scientific_writer",
            "scientific_writer",
            {
                "research_package": package.model_dump(mode="json"),
                "analysis_plan": plan.model_dump(mode="json"),
                "research_run": run.model_dump(mode="json"),
                "approved_claims": approved_claims,
            },
            ManuscriptPackage,
            gateway=FixtureModelGateway() if state.plan_only else None,
        )
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
            if section.section_id == "research_plan" or section.status == "not_generated":
                continue
            if manuscript.mode == "full_manuscript" and not section.claim_ids:
                problems.append(f"实证章节 {section.section_id} 没有 Claim 追踪信息")
            if manuscript.mode == "full_manuscript" and not section.run_ids:
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
        self._put_artifact(state, "manuscript_package", manuscript)
        if problems:
            state.status = "blocked"
            state.last_error = "成果一致性审计未通过。"
            return
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

from __future__ import annotations

import unittest

from hypoweaver.manuscript_ir import (
    ManuscriptIRError,
    allowed_writer_year_literals,
    audit_manuscript_ir,
    build_statement_registry,
    compile_section_draft,
    format_protected_value,
    rebuild_ir1_package,
    render_statement,
    reproduction_scope_overclaim,
    required_statements_by_section,
    scrub_writer_numbers,
    verify_statement_sources,
    writer_statement_catalog,
)
from hypoweaver.models import (
    AnalysisPlan,
    ClaimLedger,
    ClaimRecord,
    ExecutionRecord,
    ManuscriptPackage,
    ManuscriptSection,
    ManuscriptSectionDraft,
    ModelSpec,
    ResearchPackage,
    ResearchRun,
    ReproductionAudit,
    VerifiedPassageRef,
)


def _ledger() -> ClaimLedger:
    return ClaimLedger(
        ledger_id="ledger-1",
        case_id="case-1",
        research_run_id="run-1",
        claims=[
            ClaimRecord(
                claim_id="claim-H1",
                hypothesis_id="H1",
                claim_text="原始主张",
                final_text="在冻结样本内，核心变量与结果变量呈负向关联。",
                evidence_status="supported",
                allowed_strength="associational",
                supporting_runs=["execution-baseline"],
                opposing_runs=[],
                scope="冻结样本",
                robustness_status="completed",
                unresolved_risks=[],
                approval_status="approved",
            ),
            ClaimRecord(
                claim_id="claim-H2",
                hypothesis_id="H2",
                claim_text="未经批准的主张",
                evidence_status="inconclusive",
                allowed_strength="insufficient",
                supporting_runs=[],
                opposing_runs=[],
                scope="冻结样本",
                robustness_status="pending",
                unresolved_risks=[],
            ),
        ],
        excluded_findings=[],
        unresolved_issues=[],
    )


def _run() -> ResearchRun:
    return ResearchRun(
        research_run_id="run-1",
        case_id="case-1",
        contract_hash="contract-hash",
        plan_version=1,
        execution_status="succeeded",
        scientific_status="valid",
        fixture_only=False,
        executions=[
            ExecutionRecord(
                execution_id="execution-baseline",
                run_type="baseline",
                plan_step_id="baseline-1",
                execution_status="succeeded",
                estimates=[
                    {
                        "term": "exposure",
                        "coefficient": -0.123456,
                        "standard_error": 0.012345,
                        "p_value": 0.0004,
                        "confidence_interval_95": [-0.147654, -0.099258],
                    }
                ],
                diagnostic_results={
                    "rows_used": 29919,
                    "r_squared_within": 0.45678,
                },
            )
        ],
    )


def _analysis_plan() -> AnalysisPlan:
    return AnalysisPlan.model_construct(
        plan_id="plan-1",
        plan_version=1,
        method_family="panel_fe",
        design_only=False,
        estimands=[],
        sample_rules=[],
        variable_construction=[],
        baseline_models=[
            ModelSpec.model_construct(
                step_id="baseline-1",
                name="冻结基准模型",
                priority="required",
                execution_status="planned",
                rationale="测试",
            )
        ],
        diagnostics=[],
        robustness_tests=[],
        falsification_tests=[],
        mechanism_tests=[],
        heterogeneity_tests=[],
        identification_assumptions=[],
        alternative_explanations=[],
        failure_conditions=[],
        stop_conditions=[],
        required_data_fields=[],
        unsupported_requested_analyses=[],
    )


def _package(section: ManuscriptSection, *, ir_version: int = 1) -> ManuscriptPackage:
    return ManuscriptPackage.model_construct(
        package_id="manuscript-case-1",
        case_id="case-1",
        version=1,
        mode="full_manuscript",
        status="ready_for_human_review",
        research_plan_markdown="plan",
        manuscript_sections=[section],
        empirical_findings_status="included",
        disclosures=[],
        unresolved_issues=[],
        audit_result="not_run",
        ir_version=ir_version,
    )


class ManuscriptIRTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = _ledger()
        self.run = _run()
        self.registry = build_statement_registry(self.ledger, self.run)
        self.required = required_statements_by_section(self.registry)

    def _draft(self, section_id: str = "empirical_results") -> ManuscriptSectionDraft:
        anchors = "\n".join(
            f"[[STATEMENT:{statement_id}]]"
            for statement_id in self.required[section_id]
        )
        return ManuscriptSectionDraft(
            section_id=section_id,
            content_template="本节只陈述代码核验后的事实。\n" + anchors,
        )

    def test_protected_value_formatting_is_fixed(self) -> None:
        self.assertEqual(format_protected_value("coefficient", -0.123456), "-0.1235")
        self.assertEqual(format_protected_value("standard_error", 0.012345), "0.0123")
        self.assertEqual(format_protected_value("p_value", 0.0004), "<0.001")
        self.assertEqual(format_protected_value("p_value", 0.0126), "0.013")
        self.assertEqual(format_protected_value("count", 29919), "29919")
        self.assertEqual(format_protected_value("fit_statistic", 0.45678), "0.457")

    def test_visible_input_years_are_preserved_but_unsourced_years_are_rejected(self) -> None:
        package = ResearchPackage.model_construct(
            sample_period="1998—2009、2011—2013（2010 年无观测）",
            research_question="2007 年政策实施前后是否存在差异变化？",
            known_policy_facts=["政策于 2007-07-18 发布。"],
        )
        plan = _analysis_plan()
        plan.baseline_models[0].parameters = {
            "policy_start_year": 2007,
            "event_reference_year": 2006,
            "placebo_repetitions": 199,
        }

        allowed_years = allowed_writer_year_literals(package, plan)

        self.assertEqual(
            allowed_years,
            {"1998", "2006", "2007", "2009", "2010", "2011", "2013"},
        )
        scrubbed = scrub_writer_numbers(
            {
                "sample_period": package.sample_period,
                "policy": "2007 年 7 月发布，重复 199 次",
            },
            allowed_numeric_literals=allowed_years,
        )
        self.assertIn("1998—2009", scrubbed["sample_period"])
        self.assertIn("2007 年", scrubbed["policy"])
        self.assertNotIn(" 7 ", scrubbed["policy"])
        self.assertNotIn("199 次", scrubbed["policy"])

        section = compile_section_draft(
            ManuscriptSectionDraft(
                section_id="data_variables",
                content_template="样本覆盖 1998—2009、2011—2013，2010 年无观测。",
            ),
            self.registry,
            title="数据与变量",
            required_statement_ids=[],
            allowed_numeric_literals=allowed_years,
        )
        self.assertIn("1998—2009", section.content_markdown)
        audit_section = compile_section_draft(
            ManuscriptSectionDraft(
                section_id="introduction",
                content_template="研究窗口始于 1998 年，政策发生于 2007 年。",
            ),
            self.registry,
            title="引言",
            required_statement_ids=[],
            allowed_numeric_literals=allowed_years,
        )
        self.assertEqual(
            audit_manuscript_ir(
                _package(audit_section),
                self.ledger,
                self.run,
                analysis_plan=plan,
                allowed_numeric_literals=allowed_years,
            ),
            [],
        )
        with self.assertRaisesRegex(ManuscriptIRError, "bare numeric"):
            compile_section_draft(
                ManuscriptSectionDraft(
                    section_id="data_variables",
                    content_template="隐藏参考答案声称 2017 年发生变化。",
                ),
                self.registry,
                title="数据与变量",
                required_statement_ids=[],
                allowed_numeric_literals=allowed_years,
            )

    def test_registry_renders_frozen_step_label_not_execution_uuid(self) -> None:
        run = _run().model_copy(deep=True)
        execution_uuid = "execution-72563d0a-f635-4df5-bd11-75fd0dc6eced"
        run.executions[0].execution_id = execution_uuid
        ledger = _ledger()
        ledger.claims[0].supporting_runs = [execution_uuid]

        registry = build_statement_registry(
            ledger,
            run,
            analysis_plan=_analysis_plan(),
        )
        rendered = "\n".join(render_statement(statement) for statement in registry)

        self.assertNotIn(execution_uuid, rendered)
        self.assertIn("冻结基准模型", rendered)
        self.assertTrue(
            any(execution_uuid in statement.execution_ids for statement in registry)
        )

    def test_registry_deduplicates_identical_sample_counts(self) -> None:
        run = _run().model_copy(deep=True)
        run.executions.append(
            ExecutionRecord(
                execution_id="execution-robustness",
                run_type="robustness",
                plan_step_id="robustness-1",
                execution_status="succeeded",
                diagnostic_results={"rows_used": 29919},
            )
        )
        ledger = _ledger()
        ledger.claims[0].supporting_runs = [run.research_run_id]

        registry = build_statement_registry(ledger, run)
        sample_facts = [
            statement
            for statement in registry
            if statement.statement_kind == "sample_fact"
        ]

        self.assertEqual(len(sample_facts), 1)
        self.assertEqual(render_statement(sample_facts[0]).count("29919"), 1)

    def test_estimator_only_reproduction_is_a_required_limitation(self) -> None:
        reproduction_audit = ReproductionAudit(
            audit_id="reproduction-policy-1",
            primary_run_id=self.run.research_run_id,
            replication_run_id="replication-policy-1",
            status="matched",
            mode="independent_implementation",
            independence_scope="estimator_only",
            shared_components=[
                "policy_causal analysis-table preparation",
                "policy event/placebo regressor construction",
            ],
        )

        registry = build_statement_registry(
            self.ledger,
            self.run,
            reproduction_audit=reproduction_audit,
        )
        scope_statement = next(
            statement
            for statement in registry
            if statement.statement_id.startswith("statement-reproduction-scope-")
        )
        rendered = render_statement(scope_statement)
        requirements = required_statements_by_section(registry)

        self.assertIn("仅覆盖估计器与协方差实现", rendered)
        self.assertIn("分析表准备", rendered)
        self.assertIn("事件研究和安慰剂变量构造", rendered)
        self.assertIn("不得将该复算表述为端到端独立复现", rendered)
        self.assertIn(
            scope_statement.statement_id,
            requirements["discussion_limitations"],
        )
        self.assertNotIn(
            scope_statement.statement_id,
            requirements["empirical_results"],
        )
        self.assertTrue(
            reproduction_scope_overclaim(
                "本研究已完成端到端独立复现。",
                reproduction_audit,
            )
        )
        self.assertFalse(
            reproduction_scope_overclaim(rendered, reproduction_audit)
        )

        anchors = "\n".join(
            f"[[STATEMENT:{statement_id}]]"
            for statement_id in requirements["discussion_limitations"]
        )
        section = compile_section_draft(
            ManuscriptSectionDraft(
                section_id="discussion_limitations",
                content_template="复算边界如下。\n" + anchors,
            ),
            registry,
            title="讨论与局限",
            required_statement_ids=requirements["discussion_limitations"],
            research_run_id=self.run.research_run_id,
        )
        self.assertEqual(
            audit_manuscript_ir(
                _package(section),
                self.ledger,
                self.run,
                reproduction_audit=reproduction_audit,
            ),
            [],
        )

    def test_compile_normalizes_punctuation_at_statement_boundaries(self) -> None:
        sample = next(
            statement
            for statement in self.registry
            if statement.statement_kind == "sample_fact"
        )
        claim = next(
            statement
            for statement in self.registry
            if statement.statement_kind == "authorized_claim"
        )
        section = compile_section_draft(
            ManuscriptSectionDraft(
                section_id="empirical_results",
                content_template=(
                    f"样本口径：[[STATEMENT:{sample.statement_id}]]，"
                    f"结论边界：[[STATEMENT:{claim.statement_id}]]。"
                ),
            ),
            self.registry,
            title="实证结果",
            required_statement_ids=[sample.statement_id, claim.statement_id],
        )

        self.assertNotIn("。，", section.content_markdown)
        self.assertNotIn("。。", section.content_markdown)
        self.assertIn("29919，结论边界", section.content_markdown)

    def test_registry_has_json_pointers_and_writer_catalog_hides_values(self) -> None:
        paths = {
            value.source_path
            for statement in self.registry
            for value in statement.protected_values
        }
        self.assertIn("/claims/0/final_text", paths)
        self.assertIn("/executions/0/estimates/0/coefficient", paths)
        self.assertIn("/executions/0/diagnostic_results/rows_used", paths)
        catalog = writer_statement_catalog(self.registry)
        rendered = str(catalog)
        self.assertNotIn("-0.1235", rendered)
        self.assertNotIn("29919", rendered)
        self.assertNotIn("负向关联", rendered)

    def test_policy_diagnostics_are_whitelisted_protected_facts(self) -> None:
        ledger = _ledger()
        ledger.claims[0].supporting_runs = ["execution-baseline"]
        run = _run().model_copy(deep=True)
        run.executions[0].execution_id = "execution-baseline"
        run.executions.extend(
            [
                ExecutionRecord(
                    execution_id="execution-policy-support",
                    run_type="diagnostic",
                    plan_step_id="check-policy-support",
                    check_id="check-policy-support",
                    execution_status="succeeded",
                    diagnostic_results={
                        "group_switcher_entities": 2333,
                        "unreviewed_numeric_metric": 999,
                        "unreviewed_text": "伪造诊断 777",
                    },
                ),
                ExecutionRecord(
                    execution_id="execution-policy-event",
                    run_type="falsification",
                    plan_step_id="check-policy-event-study",
                    check_id="check-policy-event-study",
                    execution_status="succeeded",
                    diagnostic_results={"joint_pretrend_p_value": 0.052394},
                ),
                ExecutionRecord(
                    execution_id="execution-policy-placebo",
                    run_type="falsification",
                    plan_step_id="check-policy-permutation-placebo",
                    check_id="check-policy-permutation-placebo",
                    execution_status="succeeded",
                    diagnostic_results={
                        "repetitions_completed": 500,
                        "empirical_p_value": 0.001996,
                    },
                ),
            ]
        )

        registry = build_statement_registry(ledger, run)
        policy_diagnostics = [
            statement
            for statement in registry
            if statement.statement_kind == "diagnostic_fact"
            and statement.execution_ids
            and statement.execution_ids[0].startswith("execution-policy-")
        ]
        paths = {
            value.source_path
            for statement in policy_diagnostics
            for value in statement.protected_values
        }

        self.assertEqual(
            paths,
            {
                "/executions/1/diagnostic_results/group_switcher_entities",
                "/executions/2/diagnostic_results/joint_pretrend_p_value",
                "/executions/3/diagnostic_results/repetitions_completed",
                "/executions/3/diagnostic_results/empirical_p_value",
            },
        )
        self.assertEqual(len(policy_diagnostics), 4)
        self.assertTrue(
            all("[[VALUE:" in statement.text_template for statement in policy_diagnostics)
        )
        rendered = "\n".join(render_statement(statement) for statement in policy_diagnostics)
        self.assertIn("组别变化的实体数为 2333", rendered)
        self.assertIn("联合零假设检验的 p 值为 0.052", rendered)
        self.assertIn("随机置换安慰剂实际完成的置换次数为 500", rendered)
        self.assertIn("随机置换安慰剂的双侧经验 p 值为 0.002", rendered)
        self.assertNotIn("999", rendered)
        self.assertNotIn("伪造诊断", rendered)
        verify_statement_sources(policy_diagnostics, ledger, run)

    def test_policy_diagnostic_whitelist_rejects_non_numeric_source(self) -> None:
        ledger = _ledger()
        ledger.claims[0].supporting_runs = ["run-1"]
        run = _run().model_copy(deep=True)
        run.executions.append(
            ExecutionRecord(
                execution_id="execution-policy-event",
                run_type="falsification",
                plan_step_id="check-policy-event-study",
                check_id="check-policy-event-study",
                execution_status="succeeded",
                diagnostic_results={
                    "joint_pretrend_p_value": "0.052；伪造结论"
                },
            )
        )

        with self.assertRaisesRegex(ManuscriptIRError, "must be numeric"):
            build_statement_registry(ledger, run)

    def test_policy_composite_disclosures_are_required_and_source_bound(self) -> None:
        ledger = _ledger()
        ledger.claims[0].supporting_runs = ["run-1"]
        run = _run().model_copy(deep=True)
        run.scientific_status = "limited"
        run.executions.extend(
            [
                ExecutionRecord(
                    execution_id="execution-policy-fixed-pre",
                    run_type="robustness",
                    plan_step_id="check-policy-group-fixed-pre",
                    check_id="check-policy-group-fixed-pre",
                    execution_status="succeeded",
                    diagnostic_results={
                        "group_assignment_mode": "fixed_last_pre_policy",
                        "rows_input": 249504,
                        "rows_used": 155909,
                        "rows_dropped_for_group_assignment": 93595,
                        "entities_dropped_no_pre_policy_group": 45261,
                    },
                ),
                ExecutionRecord(
                    execution_id="execution-policy-event-structured",
                    run_type="falsification",
                    plan_step_id="check-policy-event-study",
                    check_id="check-policy-event-study",
                    execution_status="succeeded",
                    diagnostic_results={
                        "joint_pretrend_p_value": 1.7294493966319626e-7,
                        "remote_pre_requested": True,
                        "remote_pre_status": "complete",
                        "remote_pre_complete": True,
                        "remote_pre_term": "event_remote_pre",
                        "requested_remote_pre_years": [1998, 1999, 2000, 2001],
                        "generated_remote_pre_years": [1998, 1999, 2000, 2001],
                        "unavailable_remote_pre_years": [],
                        "collinear_remote_pre": False,
                        "policy_year_event_requested": True,
                        "policy_start_year": 2007,
                        "event_term_scaling": "binary_group_year_contrast",
                        "policy_year_event_term": "event_2007",
                        "policy_year_event_coefficient_directly_comparable_to_baseline": False,
                    },
                ),
                ExecutionRecord(
                    execution_id="execution-policy-fake-time",
                    run_type="falsification",
                    plan_step_id="check-policy-placebo-time",
                    check_id="check-policy-placebo-time",
                    execution_status="succeeded",
                    diagnostic_results={
                        "status": "succeeded",
                        "sample_start_year": 1998,
                        "sample_end_year": 2006,
                        "policy_start_year": 2007,
                        "rows_used": 93405,
                        "rows_excluded_at_or_after_true_policy": 156099,
                        "true_policy_contamination_rows": 0,
                        "pseudo_pre_support": True,
                        "pseudo_post_support": True,
                    },
                ),
                ExecutionRecord(
                    execution_id="execution-policy-permutation-structured",
                    run_type="falsification",
                    plan_step_id="check-policy-permutation-placebo",
                    check_id="check-policy-permutation-placebo",
                    execution_status="succeeded",
                    diagnostic_results={
                        "status": "succeeded",
                        "scheme": "assignment_unit_label",
                        "group_assignment_mode": "fixed_last_pre_policy",
                        "permutation_unit_field": "idcode",
                        "rows_input": 249504,
                        "rows_used": 155909,
                        "permutation_unit_count": 36596,
                        "treated_permutation_unit_count": 8671,
                        "repetitions_requested": 199,
                        "repetitions_completed": 199,
                        "extreme_count": 0,
                        "empirical_p_value": 0.005,
                    },
                ),
            ]
        )

        registry = build_statement_registry(ledger, run)
        disclosures = [
            statement
            for statement in registry
            if statement.statement_id.startswith("statement-policy-disclosure-")
        ]
        rendered = "\n".join(render_statement(statement) for statement in disclosures)
        requirements = required_statements_by_section(registry)

        self.assertEqual(len(disclosures), 4)
        self.assertIn("155909/249504", rendered)
        self.assertIn("删去 45261 个实体", rendered)
        self.assertIn("不能表述为同一样本稳健性检验", rendered)
        self.assertIn("1998、1999、2000、2001 年", rendered)
        self.assertIn("远端政策前合并项状态为完整", rendered)
        self.assertIn("事件期组间差异系数", rendered)
        self.assertIn("政策年 2007", rendered)
        self.assertIn("两者数值不可直接比较", rendered)
        self.assertIn("1998—2006 年", rendered)
        self.assertIn("真政策期污染行为 0", rendered)
        self.assertIn("36596 个分配单元", rendered)
        self.assertIn("处理单元 8671 个", rendered)
        self.assertIn("最小分辨率", rendered)
        self.assertIn("可交换", rendered)
        self.assertNotIn("动态效应", rendered)
        for statement in disclosures:
            self.assertIn(
                statement.statement_id,
                requirements["empirical_results"],
            )
        verify_statement_sources(disclosures, ledger, run)

        anchors = "\n".join(
            f"[[STATEMENT:{statement_id}]]"
            for statement_id in requirements["empirical_results"]
        )
        section = compile_section_draft(
            ManuscriptSectionDraft(
                section_id="empirical_results",
                content_template="以下为冻结事实。\n" + anchors,
            ),
            registry,
            title="实证结果",
            required_statement_ids=requirements["empirical_results"],
            research_run_id=run.research_run_id,
        )
        self.assertIn("不能单独建立因果识别", section.content_markdown)

    def test_period_index_policy_disclosures_do_not_call_periods_years(self) -> None:
        ledger = _ledger()
        ledger.claims[0].supporting_runs = ["run-1"]
        run = _run().model_copy(deep=True)
        run.executions.extend(
            [
                ExecutionRecord(
                    execution_id="execution-period-event",
                    run_type="falsification",
                    plan_step_id="check-policy-event-study",
                    check_id="check-policy-event-study",
                    execution_status="succeeded",
                    diagnostic_results={
                        "time_scale": "period_index",
                        "joint_pretrend_p_value": 0.2,
                        "remote_pre_requested": True,
                        "remote_pre_status": "complete",
                        "remote_pre_complete": True,
                        "remote_pre_term": "event_remote_pre",
                        "requested_remote_pre_years": [1, 19],
                        "generated_remote_pre_years": [1, 19],
                        "unavailable_remote_pre_years": [],
                        "collinear_remote_pre": False,
                        "policy_year_event_requested": True,
                        "policy_start_year": 31,
                        "event_term_scaling": "binary_group_year_contrast",
                        "policy_year_event_term": "event_31",
                        "policy_year_event_coefficient_directly_comparable_to_baseline": False,
                    },
                ),
                ExecutionRecord(
                    execution_id="execution-period-fake-time",
                    run_type="falsification",
                    plan_step_id="check-policy-placebo-time",
                    check_id="check-policy-placebo-time",
                    execution_status="succeeded",
                    diagnostic_results={
                        "status": "succeeded",
                        "time_scale": "period_index",
                        "sample_start_year": 1,
                        "sample_end_year": 30,
                        "policy_start_year": 31,
                        "rows_used": 100,
                        "rows_excluded_at_or_after_true_policy": 80,
                        "true_policy_contamination_rows": 0,
                        "pseudo_pre_support": True,
                        "pseudo_post_support": True,
                    },
                ),
            ]
        )

        registry = build_statement_registry(ledger, run)
        disclosures = [
            statement
            for statement in registry
            if statement.statement_id.startswith("statement-policy-disclosure-")
        ]
        rendered = "\n".join(render_statement(statement) for statement in disclosures)
        period_values = [
            value
            for statement in disclosures
            for value in statement.protected_values
            if "year" in value.source_path
        ]

        self.assertIn("第 1 期、第 19 期", rendered)
        self.assertIn("政策起始期第 31 期", rendered)
        self.assertIn("第 1—30 期", rendered)
        self.assertNotIn("政策年 31", rendered)
        self.assertNotIn("1—30 年", rendered)
        self.assertTrue(period_values)
        self.assertEqual(
            {value.value_kind for value in period_values},
            {"period_index"},
        )
        verify_statement_sources(disclosures, ledger, run)

    def test_policy_composite_disclosure_fails_closed_on_missing_attrition(self) -> None:
        ledger = _ledger()
        ledger.claims[0].supporting_runs = ["run-1"]
        run = _run().model_copy(deep=True)
        run.executions.append(
            ExecutionRecord(
                execution_id="execution-policy-fixed-pre",
                run_type="robustness",
                plan_step_id="check-policy-group-fixed-pre",
                check_id="check-policy-group-fixed-pre",
                execution_status="succeeded",
                diagnostic_results={
                    "group_assignment_mode": "fixed_last_pre_policy",
                    "rows_input": 249504,
                    "rows_used": 155909,
                    "rows_dropped_for_group_assignment": 93595,
                },
            )
        )

        with self.assertRaisesRegex(
            ManuscriptIRError,
            "entities_dropped_no_pre_policy_group is required",
        ):
            build_statement_registry(ledger, run)

        run.executions[-1].diagnostic_results[
            "entities_dropped_no_pre_policy_group"
        ] = 45261
        run.executions[-1].diagnostic_results[
            "rows_dropped_for_group_assignment"
        ] = 1
        with self.assertRaisesRegex(
            ManuscriptIRError,
            "fixed-pre attrition diagnostics are inconsistent",
        ):
            build_statement_registry(ledger, run)

    def test_opposing_execution_is_traceable_for_a_downgraded_claim(self) -> None:
        ledger = _ledger()
        ledger.claims[0].approval_status = "downgraded"
        ledger.claims[0].supporting_runs = []
        ledger.claims[0].opposing_runs = ["execution-baseline"]

        registry = build_statement_registry(ledger, self.run)

        self.assertTrue(
            any(item.statement_kind == "estimate_fact" for item in registry)
        )

    def test_approved_claim_without_bound_run_exposes_no_execution_facts(self) -> None:
        ledger = _ledger()
        ledger.claims[0].supporting_runs = []
        ledger.claims[0].opposing_runs = []

        registry = build_statement_registry(ledger, self.run)

        self.assertEqual(
            {item.statement_kind for item in registry},
            {"authorized_claim"},
        )

    def test_compile_injects_canonical_values_and_provenance(self) -> None:
        section = compile_section_draft(
            self._draft(),
            self.registry,
            title="五、实证结果",
            required_statement_ids=self.required["empirical_results"],
            research_run_id=self.run.research_run_id,
        )
        self.assertIn("-0.1235", section.content_markdown)
        self.assertIn("<0.001", section.content_markdown)
        self.assertIn("29919", section.content_markdown)
        self.assertIn("0.457", section.content_markdown)
        self.assertEqual(section.claim_ids, ["claim-H1"])
        self.assertEqual(section.run_ids[0], "run-1")
        self.assertEqual(section.content_template, self._draft().content_template)

    def test_missing_duplicate_and_unknown_anchors_are_rejected(self) -> None:
        required = self.required["conclusion"]
        with self.assertRaisesRegex(ManuscriptIRError, "missing required"):
            compile_section_draft(
                ManuscriptSectionDraft(section_id="conclusion", content_template="审慎结论。"),
                self.registry,
                title="结论",
                required_statement_ids=required,
            )
        duplicate = f"[[STATEMENT:{required[0]}]]\n[[STATEMENT:{required[0]}]]"
        with self.assertRaisesRegex(ManuscriptIRError, "exactly once"):
            compile_section_draft(
                ManuscriptSectionDraft(section_id="conclusion", content_template=duplicate),
                self.registry,
                title="结论",
                required_statement_ids=required,
            )
        with self.assertRaisesRegex(ManuscriptIRError, "unknown statement"):
            compile_section_draft(
                ManuscriptSectionDraft(
                    section_id="conclusion",
                    content_template="[[STATEMENT:statement-does-not-exist]]",
                ),
                self.registry,
                title="结论",
                required_statement_ids=required,
            )

    def test_sections_without_requirements_reject_global_statement_anchors(self) -> None:
        global_anchor = self.required["conclusion"][0]
        for section_id in ("introduction", "research_design"):
            with self.subTest(section_id=section_id):
                with self.assertRaisesRegex(
                    ManuscriptIRError,
                    "unexpected statement anchor",
                ):
                    compile_section_draft(
                        ManuscriptSectionDraft(
                            section_id=section_id,
                            content_template=(
                                "本节仅说明研究背景与设计边界。"
                                f"[[STATEMENT:{global_anchor}]]"
                            ),
                        ),
                        self.registry,
                        title=section_id,
                        required_statement_ids=[],
                    )

    def test_bare_numbers_new_empirical_judgment_and_formal_citation_are_rejected(self) -> None:
        for template, message in (
            ("样本共有 100 行。", "bare numeric"),
            ("回归结果显示方向稳定。", "empirical judgment"),
            ("按照张三（2020）的研究。", "bare numeric"),
        ):
            with self.subTest(template=template):
                with self.assertRaisesRegex(ManuscriptIRError, message):
                    compile_section_draft(
                        ManuscriptSectionDraft(
                            section_id="introduction", content_template=template
                        ),
                        self.registry,
                        title="引言",
                        required_statement_ids=[],
                    )

    def test_traceable_sections_reject_unanchored_directional_findings(self) -> None:
        claim_anchor = self.required["conclusion"][0]
        for finding in (
            "企业ESG表现越高，企业短债长用程度越低。",
            "本研究发现企业ESG表现越高，企业短债长用程度越低。",
        ):
            with self.subTest(finding=finding):
                with self.assertRaisesRegex(ManuscriptIRError, "empirical judgment"):
                    compile_section_draft(
                        ManuscriptSectionDraft(
                            section_id="conclusion",
                            content_template=(
                                finding
                                + f"[[STATEMENT:{claim_anchor}]]"
                            ),
                        ),
                        self.registry,
                        title="结论",
                        required_statement_ids=[claim_anchor],
                    )
        for assertion in (
            "ESG导致融资成本下降。",
            "核心变量抑制结果变量。",
            "ESG推动企业改善期限结构。",
            "ESG提高融资效率。",
        ):
            with self.subTest(assertion=assertion):
                with self.assertRaisesRegex(
                    ManuscriptIRError,
                    "empirical judgment|causal assertion",
                ):
                    compile_section_draft(
                        ManuscriptSectionDraft(
                            section_id="conclusion",
                            content_template=(
                                assertion
                                + f"[[STATEMENT:{claim_anchor}]]"
                            ),
                        ),
                        self.registry,
                        title="结论",
                        required_statement_ids=[claim_anchor],
                    )

    def test_sourced_non_significance_claim_can_be_compiled(self) -> None:
        ledger = _ledger()
        ledger.claims[0].final_text = (
            "在冻结样本中，未发现达到常用统计显著性阈值的关联。"
        )
        registry = build_statement_registry(ledger, self.run)
        claim_statement = next(
            item for item in registry if item.statement_kind == "authorized_claim"
        )
        section = compile_section_draft(
            ManuscriptSectionDraft(
                section_id="conclusion",
                content_template=(
                    "审慎总结。"
                    f"[[STATEMENT:{claim_statement.statement_id}]]"
                ),
            ),
            registry,
            title="结论",
            required_statement_ids=[claim_statement.statement_id],
        )
        self.assertIn("未发现达到常用统计显著性阈值的关联", section.content_markdown)

    def test_numeric_and_source_tampering_are_detected(self) -> None:
        statement = next(
            item for item in self.registry if item.statement_kind == "estimate_fact"
        )
        tampered_value = statement.protected_values[0].model_copy(
            update={"raw_value": 99.0, "rendered_value": "99.0000"}
        )
        tampered = statement.model_copy(update={"protected_values": [tampered_value, *statement.protected_values[1:]]})
        with self.assertRaisesRegex(ManuscriptIRError, "does not match its source"):
            verify_statement_sources([tampered], self.ledger, self.run)

        changed_run = self.run.model_copy(deep=True)
        changed_run.executions[0].estimates[0]["coefficient"] = -0.9
        with self.assertRaisesRegex(ManuscriptIRError, "does not match its source"):
            verify_statement_sources([statement], self.ledger, changed_run)

    def test_dangling_source_is_detected(self) -> None:
        statement = next(
            item for item in self.registry if item.statement_kind == "sample_fact"
        )
        broken = statement.model_copy(
            update={
                "protected_values": [
                    statement.protected_values[0].model_copy(
                        update={"source_id": "missing-execution"}
                    )
                ]
            }
        )
        with self.assertRaisesRegex(ManuscriptIRError, "dangling source id"):
            verify_statement_sources([broken], self.ledger, self.run)

    def test_empty_passage_registry_blocks_formal_reference(self) -> None:
        with self.assertRaises(ManuscriptIRError):
            compile_section_draft(
                ManuscriptSectionDraft(
                    section_id="introduction",
                    content_template="参见文献 [1]。",
                ),
                self.registry,
                title="引言",
                required_statement_ids=[],
            )
        passage = VerifiedPassageRef(
            passage_id="passage-1",
            source_id="source-1",
            locator="p. 1",
            text_sha256="a" * 64,
            citation_render="张三，研究标题。",
        )
        registry = build_statement_registry(self.ledger, self.run, [passage])
        citation = next(item for item in registry if item.statement_kind == "citation")
        section = compile_section_draft(
            ManuscriptSectionDraft(
                section_id="introduction",
                content_template=f"相关来源：[[STATEMENT:{citation.statement_id}]]",
            ),
            registry,
            title="引言",
            required_statement_ids=[citation.statement_id],
        )
        self.assertIn("张三，研究标题。", section.content_markdown)

    def test_h4_audit_rereads_sources_and_recompiles_text(self) -> None:
        section = compile_section_draft(
            self._draft(),
            self.registry,
            title="实证结果",
            required_statement_ids=self.required["empirical_results"],
            research_run_id=self.run.research_run_id,
        )
        package = _package(section)
        self.assertEqual(audit_manuscript_ir(package, self.ledger, self.run), [])
        tampered_section = section.model_copy(
            update={"content_markdown": section.content_markdown.replace("-0.1235", "-9.9999")}
        )
        problems = audit_manuscript_ir(
            _package(tampered_section), self.ledger, self.run
        )
        self.assertTrue(any("differs from stored" in problem for problem in problems))

    def test_h4_audit_rejects_coherent_statement_deletion(self) -> None:
        section = compile_section_draft(
            self._draft(),
            self.registry,
            title="实证结果",
            required_statement_ids=self.required["empirical_results"],
            research_run_id=self.run.research_run_id,
        )
        removed = next(
            item for item in section.statements if item.statement_kind == "estimate_fact"
        )
        tampered = section.model_copy(
            update={
                "content_template": section.content_template.replace(
                    f"[[STATEMENT:{removed.statement_id}]]", ""
                ),
                "content_markdown": section.content_markdown.replace(
                    render_statement(removed), ""
                ),
                "statements": [
                    item
                    for item in section.statements
                    if item.statement_id != removed.statement_id
                ],
            }
        )

        problems = audit_manuscript_ir(_package(tampered), self.ledger, self.run)

        self.assertTrue(
            any("rebuilt section requirements" in problem for problem in problems)
        )

    def test_ir0_rebuild_uses_fresh_template_not_legacy_body(self) -> None:
        legacy = _package(
            ManuscriptSection(
                section_id="conclusion",
                title="结论",
                content_markdown="旧正文伪造系数 999.999。",
                status="generated",
            ),
            ir_version=0,
        )
        claim_anchor = self.required["conclusion"][0]
        rebuilt = rebuild_ir1_package(
            legacy,
            [
                ManuscriptSectionDraft(
                    section_id="conclusion",
                    content_template=f"审慎总结。[[STATEMENT:{claim_anchor}]]",
                )
            ],
            self.ledger,
            self.run,
            required_by_section={"conclusion": [claim_anchor]},
        )
        self.assertEqual(rebuilt.ir_version, 1)
        self.assertEqual(rebuilt.version, 2)
        self.assertNotIn("999.999", rebuilt.manuscript_sections[0].content_markdown)
        self.assertIn("负向关联", rebuilt.manuscript_sections[0].content_markdown)


if __name__ == "__main__":
    unittest.main()

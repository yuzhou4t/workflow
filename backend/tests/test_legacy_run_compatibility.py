from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hypoweaver.definition import DEFINITION_VERSION
from hypoweaver.engine import WorkflowEngine
from hypoweaver.models import (
    AnalysisPlan,
    ClaimLedger,
    CreateRunRequest,
    FormalResearchContract,
    GateDecisionRequest,
    ManuscriptPackage,
    ResearchRun,
)
from hypoweaver.repository import RunRepository
from hypoweaver.seal import canonical_sha256, sign_manifest, verify_manifest


_LEGACY_STEP_FIELDS = {
    "threat_id",
    "target_claim_ids",
    "test_role",
    "required_for_admission",
    "source_issue_ids",
    "not_executable_reason",
}
_PLAN_STEP_GROUPS = (
    "estimands",
    "sample_rules",
    "variable_construction",
    "baseline_models",
    "diagnostics",
    "robustness_tests",
    "falsification_tests",
    "mechanism_tests",
    "heterogeneity_tests",
)
_LEGACY_CLAIM_FIELDS = {
    "claim_type",
    "required_check_ids",
    "admission_status",
    "max_allowed_strength",
    "gate_reasons",
}


class LegacyCompletedRunCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_v13_completed_run_remains_hash_valid_and_read_only(self) -> None:
        self.assertEqual(DEFINITION_VERSION, "1.8.0")
        with (
            tempfile.TemporaryDirectory() as tempdir,
            patch.dict(
                "os.environ",
                {"HYPOWEAVER_SEAL_SECRET": "legacy-test-secret-0123456789abcdef"},
            ),
        ):
            database = Path(tempdir) / "runs.db"
            repository = RunRepository(database)
            engine = WorkflowEngine(repository)
            completed = await _completed_fixture_run(engine)
            legacy_payload = _as_v13_completed_payload(completed.model_dump(mode="json"))
            serialized = json.dumps(
                legacy_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE runs SET payload = ? WHERE id = ?",
                    (serialized, completed.id),
                )

            before = _stored_row(database, completed.id)
            loaded = engine.get_run(completed.id)
            listed = next(item for item in engine.list_runs() if item.id == completed.id)
            advanced = await engine.advance(completed.id)
            after = _stored_row(database, completed.id)

            self.assertEqual(loaded.definition_version, "1.3.0")
            self.assertEqual(loaded.status, "completed")
            self.assertEqual(listed.version, completed.version)
            self.assertEqual(advanced.version, completed.version)
            self.assertEqual(before, after)

            for key, envelope in loaded.artifacts.items():
                self.assertEqual(
                    envelope["sha256"],
                    canonical_sha256(envelope["payload"]),
                    key,
                )
                self.assertEqual(
                    envelope["sha256"],
                    legacy_payload["artifacts"][key]["sha256"],
                    key,
                )

            sealed = loaded.artifacts["sealed_output"]["payload"]
            unsigned_seal = {
                key: value for key, value in sealed.items() if key != "seal_sha256"
            }
            self.assertNotIn("analysis_plan_sha256", sealed)
            self.assertTrue(verify_manifest(unsigned_seal, sealed["seal_sha256"]))
            self.assertEqual(
                sealed["seal_sha256"],
                legacy_payload["artifacts"]["sealed_output"]["payload"][
                    "seal_sha256"
                ],
            )

            AnalysisPlan.model_validate(
                loaded.artifacts["analysis_plan"]["payload"]
            )
            FormalResearchContract.model_validate(
                loaded.artifacts["formal_research_contract"]["payload"]
            )
            ResearchRun.model_validate(
                loaded.artifacts["research_run"]["payload"]
            )
            ClaimLedger.model_validate(
                loaded.artifacts["approved_claim_ledger"]["payload"]
            )
            ManuscriptPackage.model_validate(
                loaded.artifacts["manuscript_package"]["payload"]
            )

            loaded.case_name = "in-memory mutation"
            self.assertEqual(engine.get_run(completed.id).case_name, completed.case_name)
            self.assertEqual(_stored_row(database, completed.id), before)


async def _completed_fixture_run(engine: WorkflowEngine):
    run = await engine.create_run(
        CreateRunRequest(preset_case_id="green-finance-did")
    )
    run = await engine.decide_gate(
        run.id,
        "H1",
        GateDecisionRequest(action="approve", idempotency_key="legacy-h1"),
    )
    run = await engine.decide_gate(
        run.id,
        "H2",
        GateDecisionRequest(action="approve", idempotency_key="legacy-h2"),
    )
    run = await engine.decide_gate(
        run.id,
        "H3",
        GateDecisionRequest(
            action="generate_plan_only",
            idempotency_key="legacy-h3",
            claims=[
                {"claim_id": claim.claim_id, "decision": "hold"}
                for claim in run.claims
            ],
        ),
    )
    return await engine.decide_gate(
        run.id,
        "H4",
        GateDecisionRequest(action="approve", idempotency_key="legacy-h4"),
    )


def _as_v13_completed_payload(current: dict) -> dict:
    legacy = copy.deepcopy(current)
    legacy["definition_version"] = "1.3.0"
    legacy.pop("processed_idempotency_keys", None)
    artifacts = legacy["artifacts"]

    plan = artifacts["analysis_plan"]["payload"]
    _strip_t14_plan_fields(plan)
    _rehash(artifacts["analysis_plan"])

    contract = artifacts["formal_research_contract"]["payload"]
    _strip_t14_plan_fields(contract["approved_plan"])
    contract["approved_plan_hash"] = canonical_sha256(contract["approved_plan"])
    _rehash(artifacts["formal_research_contract"])

    research_run = artifacts["research_run"]["payload"]
    for execution in research_run.get("executions", []):
        execution.pop("check_id", None)
        execution.pop("not_executed_reason_code", None)
        execution.pop("provenance", None)
    _rehash(artifacts["research_run"])

    ledger = artifacts["approved_claim_ledger"]["payload"]
    for claim in ledger.get("claims", []):
        for field in _LEGACY_CLAIM_FIELDS:
            claim.pop(field, None)
    _rehash(artifacts["approved_claim_ledger"])
    for claim in legacy.get("claims", []):
        for field in _LEGACY_CLAIM_FIELDS:
            claim.pop(field, None)

    manuscript = artifacts["manuscript_package"]["payload"]
    manuscript.pop("ir_version", None)
    for section in manuscript.get("manuscript_sections", []):
        section.pop("content_template", None)
        section.pop("statements", None)
    _rehash(artifacts["manuscript_package"])

    source_hashes = {
        key: artifacts[key]["sha256"]
        for key in (
            "formal_research_contract",
            "research_run",
            "approved_claim_ledger",
            "manuscript_package",
        )
    }
    sealed = {
        "run_id": legacy["id"],
        "seal_algorithm": "hmac-sha256",
        "contract_sha256": source_hashes["formal_research_contract"],
        "research_run_sha256": source_hashes["research_run"],
        "claim_ledger_sha256": source_hashes["approved_claim_ledger"],
        "manuscript_sha256": source_hashes["manuscript_package"],
    }
    sealed["seal_sha256"] = sign_manifest(sealed)
    artifacts["sealed_output"]["payload"] = sealed
    _rehash(artifacts["sealed_output"])
    for step in legacy.get("steps", []):
        if step.get("node_id") == "complete":
            step["input"] = copy.deepcopy(sealed)
            step["output"] = copy.deepcopy(sealed)
    return legacy


def _strip_t14_plan_fields(plan: dict) -> None:
    plan.pop("check_registry_version", None)
    for group in _PLAN_STEP_GROUPS:
        for step in plan.get(group, []):
            for field in _LEGACY_STEP_FIELDS:
                step.pop(field, None)


def _rehash(envelope: dict) -> None:
    envelope["sha256"] = canonical_sha256(envelope["payload"])


def _stored_row(database: Path, run_id: str) -> tuple[int, str, str]:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT version, payload, updated_at FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    assert row is not None
    return int(row[0]), str(row[1]), str(row[2])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hypoweaver.benchmark_evaluator import seal_benchmark_packet
from hypoweaver.benchmark_models import (
    ABLATION_IDS,
    FAULT_IDS,
    AblationReplayResult,
    BenchmarkPacket,
    BenchmarkReference,
    BenchmarkResourceUsage,
    FaultOutcome,
    FaultReplayReport,
    FrozenBenchmarkProtocol,
    HardMetric,
    HardMetricReport,
    NormalizedDesign,
    OfficialAttemptBinding,
)
from hypoweaver.benchmark_protocol import official_holdout_lock_id, seal_protocol
from hypoweaver.models import ModelCallReceipt
from hypoweaver.recovery_campaign import (
    RecoveryCampaignStore,
    build_recovery_freeze,
    campaign_id_for_freeze,
    canonical_recovery_campaign_path,
    create_recovery_campaign,
    cumulative_llm_calls,
    import_prior_usage,
    import_prior_usage_from_ledger,
    invalidate_recovery_campaign,
    map_model_call_receipts,
    recovery_pool_remaining,
    seal_recovery_freeze,
    verify_recovery_campaign,
    verify_recovery_environment,
)
from hypoweaver.recovery_identity import FIRST_ROUND_LOGICAL_SLOTS
from hypoweaver.recovery_models import (
    HARD_METRIC_IDS,
    RecoveryCallReceipt,
    RecoveryComparisonSubmission,
    RecoveryRoundSubmission,
    RecoveryUsage,
)
from hypoweaver.seal import canonical_sha256


NOW = "2026-07-16T00:00:00+00:00"
LATER = "2026-07-16T00:01:00+00:00"


class RecoveryCampaignTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.visible_path = self.root / "case" / "visible.txt"
        self.data_path = self.root / "case" / "panel.csv"
        self.reference_path = self.root / "case" / "reference.json"
        self.summary_path = self.root / "case" / "reference-summary.md"
        self.agent_source = self.root / "sources" / "agent.py"
        self.harness_source = self.root / "sources" / "harness.py"
        self.hypo_source = self.root / "sources" / "hypo.py"
        self.config_path = self.root / "config" / "recovery.json"
        for path in (
            self.visible_path,
            self.data_path,
            self.reference_path,
            self.summary_path,
            self.agent_source,
            self.harness_source,
            self.hypo_source,
            self.config_path,
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.visible_path.write_text("seen case\n", encoding="utf-8")
        self.data_path.write_text("firm,year,y\n1,2024,2\n", encoding="utf-8")
        self.summary_path.write_text("sealed reference summary\n", encoding="utf-8")
        self.agent_source.write_text("AGENT = 'current'\n", encoding="utf-8")
        self.harness_source.write_text("HARNESS = 'current'\n", encoding="utf-8")
        self.hypo_source.write_text("HYPO = 'declared'\n", encoding="utf-8")
        self.config_path.write_text('{"model":"qwen"}\n', encoding="utf-8")

        visible_sha = _file_sha256(self.visible_path)
        data_sha = _file_sha256(self.data_path)
        self.reference = BenchmarkReference(
            case_id="seen-case-1",
            visible_input_sha256=visible_sha,
            data_sha256=[data_sha],
            expected_design={},
            required_check_ids=[],
            independently_reproducible_check_ids=[],
        )
        self.reference_path.write_text(
            json.dumps(self.reference.model_dump(mode="json")),
            encoding="utf-8",
        )
        self.protocol = seal_protocol(
            FrozenBenchmarkProtocol(
                case_id=self.reference.case_id,
                visible_input_sha256=visible_sha,
                data_sha256=[data_sha],
                reference_sha256=canonical_sha256(
                    self.reference.model_dump(mode="json")
                ),
                source_sha256={
                    "hypoweaver": "d" * 64,
                    "agent_laboratory": "e" * 64,
                    "benchmark_harness": "f" * 64,
                },
                configuration_sha256="1" * 64,
                source_artifact_paths={
                    "hypoweaver": ["sources/hypo.py"],
                    "agent_laboratory": ["sources/agent.py"],
                    "benchmark_harness": ["sources/harness.py"],
                },
                configuration_artifact_paths=["config/recovery.json"],
                frozen_at=NOW,
            )
        )
        self.freeze = build_recovery_freeze(
            self.protocol,
            artifact_root=self.root,
            visible_input_path=self.visible_path,
            data_paths=(self.data_path,),
            reference_path=self.reference_path,
            reference_summary_path=self.summary_path,
            frozen_at=NOW,
        )
        self.binding = OfficialAttemptBinding(
            attempt_id="4" * 64,
            run_manifest_sha256="5" * 64,
            begun_at=NOW,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_freeze_rehashes_live_sources_and_all_case_files(self) -> None:
        self.assertNotEqual(
            self.freeze.agent_laboratory_sha256,
            self.protocol.source_sha256["agent_laboratory"],
        )
        self.assertEqual(self.freeze.reference_summary_sha256, _file_sha256(self.summary_path))
        self.assertEqual(set(self.freeze.sealed_label_orders), {"A_B", "B_A"})
        self.assertEqual(set(self.freeze.sealed_system_assignments), {"A_B", "B_A"})
        loaded = verify_recovery_environment(
            self.freeze,
            self.protocol,
            artifact_root=self.root,
            visible_input_path=self.visible_path,
            data_paths=(self.data_path,),
            reference_path=self.reference_path,
            reference_summary_path=self.summary_path,
        )
        self.assertEqual(loaded, self.reference)

        self.agent_source.write_text("AGENT = 'drifted'\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "environment hash drift"):
            verify_recovery_environment(
                self.freeze,
                self.protocol,
                artifact_root=self.root,
                visible_input_path=self.visible_path,
                data_paths=(self.data_path,),
                reference_path=self.reference_path,
                reference_summary_path=self.summary_path,
            )

    def test_prior_calls_are_exact_but_legacy_tokens_remain_a_lower_bound(self) -> None:
        prior = import_prior_usage_from_ledger(
            self.binding,
            source_official_holdout_lock_id=official_holdout_lock_id(self.protocol),
            usage=RecoveryUsage(
                llm_calls=22,
                input_tokens=157728,
                output_tokens=23559,
            ),
            resource_ledger_sha256="7" * 64,
            verified_receipt_sha256=tuple(f"{index:064x}" for index in range(1, 22)),
            token_usage_status="lower_bound",
            imported_at=NOW,
        )
        campaign = create_recovery_campaign(self.freeze, prior, created_at=NOW)

        self.assertEqual(prior.evidence.missing_receipt_count, 1)
        self.assertEqual(prior.evidence.token_usage_status, "lower_bound")
        self.assertIn(
            "legacy_single_pass_tokens_unavailable",
            prior.evidence.limitation_codes,
        )
        self.assertEqual(campaign.cumulative_token_usage_status, "lower_bound")
        self.assertEqual(cumulative_llm_calls(campaign), 22)

    def test_replacement_freeze_inherits_orders_and_uses_v2_identity(self) -> None:
        predecessor = invalidate_recovery_campaign(
            create_recovery_campaign(
                self.freeze,
                self._prior(calls=22),
                created_at=NOW,
            ),
            "receipt_binding_defect",
            invalidated_at=LATER,
        )
        predecessor_state_root = self.root / "predecessor-state"
        predecessor_path = canonical_recovery_campaign_path(
            predecessor_state_root,
            predecessor.freeze,
        )
        RecoveryCampaignStore(predecessor_path).create(predecessor)

        replacement_freeze = build_recovery_freeze(
            self.protocol,
            artifact_root=self.root,
            visible_input_path=self.visible_path,
            data_paths=(self.data_path,),
            reference_path=self.reference_path,
            reference_summary_path=self.summary_path,
            predecessor_campaign=predecessor,
            frozen_at=LATER,
        )

        binding = replacement_freeze.predecessor_binding
        self.assertIsNotNone(binding)
        self.assertEqual(
            replacement_freeze.sealed_label_orders,
            predecessor.freeze.sealed_label_orders,
        )
        self.assertEqual(
            replacement_freeze.sealed_system_assignments,
            predecessor.freeze.sealed_system_assignments,
        )
        self.assertEqual(binding.predecessor_campaign_sha256, predecessor.campaign_sha256)
        self.assertEqual(binding.predecessor_cumulative_llm_calls, 22)
        self.assertNotEqual(
            campaign_id_for_freeze(replacement_freeze),
            predecessor.campaign_id,
        )
        replacement = create_recovery_campaign(
            replacement_freeze,
            self._prior(calls=22),
            created_at=LATER,
        )
        self.assertEqual(cumulative_llm_calls(replacement), 22)
        loaded = verify_recovery_environment(
            replacement.freeze,
            self.protocol,
            artifact_root=self.root,
            visible_input_path=self.visible_path,
            data_paths=(self.data_path,),
            reference_path=self.reference_path,
            reference_summary_path=self.summary_path,
            predecessor_campaign_path=predecessor_path,
        )
        self.assertEqual(loaded, self.reference)

    def test_replacement_rejects_unqualified_predecessor(self) -> None:
        open_predecessor = create_recovery_campaign(
            self.freeze,
            self._prior(),
            created_at=NOW,
        )
        with self.assertRaisesRegex(ValueError, "must be invalidated"):
            self._replacement_freeze(open_predecessor)

    def test_replacement_carries_conservative_calls_and_started_round(self) -> None:
        store = RecoveryCampaignStore(self.root / "charged-predecessor.json")
        store.create(
            create_recovery_campaign(
                self.freeze,
                self._prior(calls=22),
                created_at=NOW,
            )
        )
        self.assertIsNotNone(store.reserve_round(owner_id="owner", now=NOW))
        charged_predecessor = store.invalidate("unknown_attempt_usage")

        replacement = create_recovery_campaign(
            self._replacement_freeze(charged_predecessor),
            self._prior(calls=22),
            created_at=LATER,
        )

        self.assertEqual(replacement.prior_usage.usage.llm_calls, 22)
        self.assertEqual(replacement.predecessor_carryover.conservative_llm_calls, 20)
        self.assertEqual(replacement.predecessor_carryover.started_round_count, 1)
        self.assertEqual(cumulative_llm_calls(replacement), 42)
        self.assertEqual(recovery_pool_remaining(replacement), 52)
        self.assertEqual(
            replacement.freeze.predecessor_binding.predecessor_incremental_llm_calls,
            20,
        )

        replacement_store = RecoveryCampaignStore(
            self.root / "replacement-with-five-slots.json"
        )
        replacement_store.create(replacement)
        for index in range(5):
            reservation = replacement_store.reserve_round(
                owner_id=f"owner-{index}"
            )
            self.assertIsNotNone(reservation)
            replacement_store.finalize_terminal_round(
                owner_id=f"owner-{index}",
                reservation_id=reservation.reservation_id,
                submission=RecoveryRoundSubmission(
                    freeze_sha256=str(replacement.freeze.freeze_sha256),
                    call_limit=reservation.call_limit,
                    implementation_sha256=(
                        replacement.freeze.hypoweaver_source_sha256
                    ),
                    started_at=NOW,
                    completed_at=LATER,
                    usage=RecoveryUsage(),
                    technical_failure="fixture_technical_failure",
                ),
            )
        exhausted = replacement_store.load()
        self.assertEqual(len(exhausted.rounds), 5)
        self.assertEqual(exhausted.status, "exhausted")
        self.assertEqual(exhausted.status_reason, "max_rounds_reached")
        self.assertIsNone(replacement_store.reserve_round(owner_id="owner-six"))

    def test_two_level_replacement_carryover_is_conservative_and_additive(self) -> None:
        first_store = RecoveryCampaignStore(self.root / "chain-first.json")
        first_store.create(
            create_recovery_campaign(
                self.freeze,
                self._prior(calls=22),
                created_at=NOW,
            )
        )
        self.assertIsNotNone(first_store.reserve_round(owner_id="first", now=NOW))
        first_invalidated = first_store.invalidate("first_unknown_attempt")
        first_replacement = create_recovery_campaign(
            self._replacement_freeze(first_invalidated),
            self._prior(calls=22),
            created_at=LATER,
        )
        second_store = RecoveryCampaignStore(self.root / "chain-second.json")
        second_store.create(first_replacement)
        self.assertIsNotNone(second_store.reserve_round(owner_id="second"))
        second_invalidated = second_store.invalidate("second_unknown_attempt")

        second_replacement = create_recovery_campaign(
            self._replacement_freeze(second_invalidated),
            self._prior(calls=22),
        )

        self.assertEqual(second_replacement.prior_usage.usage.llm_calls, 22)
        self.assertEqual(
            second_replacement.predecessor_carryover.conservative_llm_calls,
            40,
        )
        self.assertEqual(
            second_replacement.predecessor_carryover.started_round_count,
            2,
        )
        self.assertEqual(cumulative_llm_calls(second_replacement), 62)
        self.assertEqual(recovery_pool_remaining(second_replacement), 32)
        self.assertEqual(
            second_replacement.freeze.sealed_label_orders,
            first_replacement.freeze.sealed_label_orders,
        )
        third_store = RecoveryCampaignStore(self.root / "chain-third.json")
        third_store.create(second_replacement)
        reservation = third_store.reserve_round(owner_id="third")
        self.assertIsNotNone(reservation)
        self.assertEqual(reservation.call_limit, 20)
        self.assertEqual(recovery_pool_remaining(third_store.load()), 12)

    def test_finalized_predecessor_rounds_preserve_known_usage_and_last_slot(self) -> None:
        first_store = RecoveryCampaignStore(self.root / "known-chain-first.json")
        first_store.create(
            create_recovery_campaign(
                self.freeze,
                self._prior(calls=22),
                created_at=NOW,
            )
        )
        self.assertIsNotNone(first_store.reserve_round(owner_id="first", now=NOW))
        first_invalidated = first_store.invalidate("first_unknown_attempt")
        replacement = create_recovery_campaign(
            self._replacement_freeze(first_invalidated),
            self._prior(calls=22),
            created_at=LATER,
        )
        second_store = RecoveryCampaignStore(self.root / "known-chain-second.json")
        second_store.create(replacement)

        for round_index, call_count in enumerate((5, 6, 5), start=1):
            owner = f"known-owner-{round_index}"
            reservation = second_store.reserve_round(
                owner_id=owner,
                lease_seconds=7200,
            )
            self.assertIsNotNone(reservation)
            model_receipts = [
                _model_receipt(slot, round_index * 10 + index)
                for index, slot in enumerate(
                    FIRST_ROUND_LOGICAL_SLOTS[:call_count],
                    start=1,
                )
            ]
            model_receipts[0] = model_receipts[0].model_copy(
                update={
                    "outcome": "transport_failure",
                    "error_type": "APIConnectionError",
                    "input_tokens": 0,
                    "output_tokens": 0,
                }
            )
            receipts = map_model_call_receipts(
                model_receipts,
                campaign_id=replacement.campaign_id,
                round_id=reservation.round_id,
                require_complete=False,
            )
            second_store.finalize_terminal_round(
                owner_id=owner,
                reservation_id=reservation.reservation_id,
                submission=RecoveryRoundSubmission(
                    freeze_sha256=str(replacement.freeze.freeze_sha256),
                    call_limit=reservation.call_limit,
                    implementation_sha256=(
                        replacement.freeze.hypoweaver_source_sha256
                    ),
                    started_at=NOW,
                    completed_at=LATER,
                    usage=RecoveryUsage(
                        llm_calls=call_count,
                        input_tokens=sum(item.input_tokens for item in receipts),
                        output_tokens=sum(item.output_tokens for item in receipts),
                        wall_time_seconds=float(call_count),
                        technical_failures=("APIConnectionError",),
                    ),
                    receipts=receipts,
                    technical_failure="fixture_TestDagError",
                ),
            )

        self.assertIsNotNone(second_store.reserve_round(owner_id="unknown-fourth"))
        predecessor = second_store.invalidate("fourth_unknown_attempt")
        successor = create_recovery_campaign(
            self._replacement_freeze(predecessor),
            self._prior(calls=22),
        )

        carryover = successor.predecessor_carryover
        self.assertIsNotNone(carryover)
        self.assertEqual(carryover.conservative_llm_calls, 56)
        self.assertEqual(carryover.started_round_count, 5)
        self.assertEqual(carryover.known_usage.llm_calls, 16)
        self.assertEqual(carryover.known_usage.input_tokens, 130)
        self.assertEqual(carryover.known_usage.output_tokens, 26)
        self.assertEqual(carryover.known_usage.wall_time_seconds, 16.0)
        self.assertEqual(
            carryover.known_usage.technical_failures,
            ("APIConnectionError",) * 3,
        )
        self.assertEqual(carryover.unknown_llm_calls, 40)
        self.assertEqual(cumulative_llm_calls(successor), 78)
        self.assertEqual(recovery_pool_remaining(successor), 16)
        successor_store = RecoveryCampaignStore(self.root / "known-chain-third.json")
        successor_store.create(successor)
        final_reservation = successor_store.reserve_round(owner_id="last-owner")
        self.assertIsNotNone(final_reservation)
        self.assertEqual(final_reservation.round_index, 1)
        self.assertEqual(final_reservation.call_limit, 16)
        self.assertIsNone(successor_store.reserve_round(owner_id="no-extra-owner"))

        tampered_rounds = []
        previous_hash = None
        for index, round_record in enumerate(predecessor.rounds):
            usage = round_record.usage
            if index == 0:
                usage = usage.model_copy(
                    update={"input_tokens": usage.input_tokens + 1}
                )
            unsigned = round_record.model_dump(
                mode="json",
                exclude={"round_sha256"},
            )
            unsigned["usage"] = usage.model_dump(mode="json")
            unsigned["previous_round_sha256"] = previous_hash
            resealed = type(round_record)(
                **unsigned,
                round_sha256=canonical_sha256(unsigned),
            )
            tampered_rounds.append(resealed)
            previous_hash = resealed.round_sha256
        unsigned_campaign = predecessor.model_copy(
            update={"rounds": tuple(tampered_rounds)}
        ).model_dump(mode="json", exclude={"campaign_sha256"})
        tampered = type(predecessor).model_validate(
            {
                **unsigned_campaign,
                "campaign_sha256": canonical_sha256(unsigned_campaign),
            }
        )
        with self.assertRaisesRegex(ValueError, "receipt input tokens"):
            self._replacement_freeze(tampered)

    def test_replacement_predecessor_state_tamper_is_rejected(self) -> None:
        predecessor = invalidate_recovery_campaign(
            create_recovery_campaign(self.freeze, self._prior(), created_at=NOW),
            "receipt_binding_defect",
            invalidated_at=LATER,
        )
        state_root = self.root / "tamper-state"
        predecessor_path = canonical_recovery_campaign_path(
            state_root,
            predecessor.freeze,
        )
        RecoveryCampaignStore(predecessor_path).create(predecessor)
        replacement_freeze = self._replacement_freeze(predecessor)
        payload = json.loads(predecessor_path.read_text(encoding="utf-8"))
        payload["status_reason"] = "tampered"
        predecessor_path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "campaign .* mismatch"):
            verify_recovery_environment(
                replacement_freeze,
                self.protocol,
                artifact_root=self.root,
                visible_input_path=self.visible_path,
                data_paths=(self.data_path,),
                reference_path=self.reference_path,
                reference_summary_path=self.summary_path,
                predecessor_campaign_path=predecessor_path,
            )

    def test_replacement_rejects_resealed_prior_provenance_forgery(self) -> None:
        predecessor = invalidate_recovery_campaign(
            create_recovery_campaign(
                self.freeze,
                self._prior(calls=22),
                created_at=NOW,
            ),
            "predecessor_code_invalidation",
            invalidated_at=LATER,
        )
        replacement_freeze = self._replacement_freeze(predecessor)
        original = self._prior(calls=22)
        variants = {}

        token_payload = original.model_dump(
            mode="json", exclude={"import_sha256"}
        )
        token_payload["usage"]["input_tokens"] += 1
        variants["tokens"] = token_payload

        attempt_payload = original.model_dump(
            mode="json", exclude={"import_sha256"}
        )
        attempt_payload["source_official_attempt_id"] = "a" * 64
        variants["attempt"] = attempt_payload

        manifest_payload = original.model_dump(
            mode="json", exclude={"import_sha256"}
        )
        manifest_payload["source_official_run_manifest_sha256"] = "b" * 64
        variants["manifest"] = manifest_payload

        ledger_payload = original.model_dump(
            mode="json", exclude={"import_sha256"}
        )
        ledger_payload["evidence"]["resource_ledger_sha256"] = "c" * 64
        variants["ledger"] = ledger_payload

        receipt_payload = original.model_dump(
            mode="json", exclude={"import_sha256"}
        )
        receipt_payload["evidence"]["verified_receipt_sha256"][0] = "d" * 64
        variants["receipt"] = receipt_payload

        for label, payload in variants.items():
            with self.subTest(label=label):
                forged = type(original).model_validate(payload)
                with self.assertRaisesRegex(ValueError, "provenance differs"):
                    create_recovery_campaign(
                        replacement_freeze,
                        forged,
                        created_at=LATER,
                    )

        refreshed_payload = original.model_dump(
            mode="json", exclude={"import_sha256"}
        )
        refreshed_payload["imported_at"] = "2026-07-17T12:00:00+00:00"
        refreshed = type(original).model_validate(refreshed_payload)
        accepted = create_recovery_campaign(
            replacement_freeze,
            refreshed,
            created_at=LATER,
        )
        self.assertEqual(accepted.prior_usage.imported_at, refreshed.imported_at)

    def test_legacy_freeze_and_campaign_hashes_exclude_empty_predecessor(self) -> None:
        freeze_payload = self.freeze.model_dump(mode="json")
        self.assertNotIn("predecessor_binding", freeze_payload)
        campaign = create_recovery_campaign(
            self.freeze,
            self._prior(),
            created_at=NOW,
        )
        payload = campaign.model_dump(mode="json", by_alias=True)
        self.assertNotIn("predecessor_binding", payload["protocol"])
        loaded = type(campaign).model_validate(payload)
        verify_recovery_campaign(loaded)
        self.assertEqual(loaded.campaign_sha256, campaign.campaign_sha256)
        self.assertEqual(loaded.campaign_id, campaign.campaign_id)

    def test_sealed_zero_call_replacement_binding_remains_compatible(self) -> None:
        predecessor = invalidate_recovery_campaign(
            create_recovery_campaign(
                self.freeze,
                self._prior(calls=22),
                created_at=NOW,
            ),
            "predecessor_code_invalidation",
            invalidated_at=LATER,
        )
        current = self._replacement_freeze(predecessor)
        binding_payload = current.predecessor_binding.model_dump(mode="json")
        binding_payload.pop("predecessor_invalidation_sha256")
        binding_payload.pop("predecessor_started_round_count")
        binding_payload.pop("predecessor_prior_usage_content_sha256")
        binding_payload.pop("predecessor_known_usage")
        binding_payload.pop("predecessor_unknown_llm_calls")
        legacy_binding = type(current.predecessor_binding).model_validate(
            binding_payload
        )
        legacy_freeze = seal_recovery_freeze(
            current.model_copy(
                update={
                    "predecessor_binding": legacy_binding,
                    "freeze_sha256": None,
                }
            )
        )
        campaign = create_recovery_campaign(
            legacy_freeze,
            self._prior(calls=22),
            created_at=LATER,
        )
        serialized = campaign.model_dump(mode="json", by_alias=True)
        serialized_binding = serialized["protocol"]["predecessor_binding"]
        self.assertNotIn("predecessor_invalidation_sha256", serialized_binding)
        self.assertNotIn("predecessor_started_round_count", serialized_binding)
        self.assertNotIn("predecessor_known_usage", serialized_binding)
        self.assertNotIn("predecessor_unknown_llm_calls", serialized_binding)
        loaded = type(campaign).model_validate(serialized)
        verify_recovery_campaign(loaded)
        self.assertEqual(loaded.freeze.freeze_sha256, legacy_freeze.freeze_sha256)
        self.assertEqual(loaded.campaign_sha256, campaign.campaign_sha256)
        predecessor_path = canonical_recovery_campaign_path(
            self.root / "legacy-binding-state",
            predecessor.freeze,
        )
        RecoveryCampaignStore(predecessor_path).create(predecessor)
        self.assertEqual(
            verify_recovery_environment(
                legacy_freeze,
                self.protocol,
                artifact_root=self.root,
                visible_input_path=self.visible_path,
                data_paths=(self.data_path,),
                reference_path=self.reference_path,
                reference_summary_path=self.summary_path,
                predecessor_campaign_path=predecessor_path,
            ),
            self.reference,
        )

    def test_round_reservation_is_atomic_and_expiry_is_conservatively_charged(self) -> None:
        store = self._store()
        first = store.reserve_round(owner_id="owner-a", lease_seconds=1, now=NOW)
        self.assertIsNotNone(first)
        self.assertIsNone(store.reserve_round(owner_id="owner-a", now=NOW))
        self.assertIsNone(store.reserve_round(owner_id="owner-b", now=NOW))
        self.assertEqual(store.load().status, "open")
        self.assertEqual(recovery_pool_remaining(store.load()), 74)

        expired_at = (datetime.fromisoformat(NOW) + timedelta(seconds=2)).isoformat()
        self.assertIsNone(
            store.reserve_round(owner_id="owner-a", lease_seconds=1, now=expired_at)
        )
        campaign = store.load()
        self.assertEqual(campaign.status, "invalidated")
        self.assertEqual(campaign.protocol_status, "invalidated")
        self.assertEqual(campaign.invalidation.conservative_llm_call_charge, 20)
        self.assertTrue(campaign.invalidation.unknown_call_evidence)
        self.assertEqual(cumulative_llm_calls(campaign), 20)

    def test_manual_invalidation_cannot_clear_active_round_at_zero_cost(self) -> None:
        store = self._store()
        reservation = store.reserve_round(owner_id="owner", now=NOW)
        self.assertIsNotNone(reservation)

        campaign = store.invalidate("manual_fail_closed")

        self.assertEqual(campaign.status, "invalidated")
        self.assertEqual(campaign.invalidation.reservation_scope, "round")
        self.assertEqual(
            campaign.invalidation.conservative_llm_call_charge,
            reservation.call_limit,
        )

    def test_manual_invalidation_cannot_clear_active_comparison_at_zero_cost(self) -> None:
        store = self._qualified_store()
        reservation = store.reserve_comparison(owner_id="owner", now=NOW)
        self.assertIsNotNone(reservation)

        campaign = store.invalidate("manual_fail_closed")

        self.assertEqual(campaign.status, "invalidated")
        self.assertEqual(campaign.invalidation.reservation_scope, "comparison")
        self.assertEqual(campaign.invalidation.conservative_llm_call_charge, 26)

    def test_stale_lock_file_does_not_block_os_managed_lock(self) -> None:
        store = self._store()
        store.lock_path.write_text("stale process marker", encoding="utf-8")

        reservation = store.reserve_round(owner_id="owner", now=NOW)

        self.assertIsNotNone(reservation)
        self.assertTrue(store.lock_path.exists())

    def test_legal_round_can_finalize_more_than_600_seconds_after_reservation(self) -> None:
        store = self._store()
        reservation = store.reserve_round(
            owner_id="slow-owner",
            lease_seconds=7200,
            now=NOW,
        )
        completed = (
            datetime.fromisoformat(NOW) + timedelta(seconds=601)
        ).isoformat()
        campaign = store.finalize_terminal_round(
            owner_id="slow-owner",
            reservation_id=reservation.reservation_id,
            submission=RecoveryRoundSubmission(
                freeze_sha256=str(self.freeze.freeze_sha256),
                call_limit=reservation.call_limit,
                implementation_sha256=self.freeze.hypoweaver_source_sha256,
                started_at=NOW,
                completed_at=completed,
                usage=RecoveryUsage(),
                technical_failure="no_model_call_preflight_failure",
            ),
            now=completed,
        )
        self.assertEqual(campaign.status, "open")
        self.assertEqual(campaign.rounds[0].status, "technical_failed")

    def test_model_receipts_map_by_start_order_and_partial_failure_is_preserved(self) -> None:
        first, second = _same_prompt_model_receipts()
        mapped = map_model_call_receipts(
            [second, first],
            campaign_id="campaign",
            round_id="round-01",
            require_complete=False,
        )
        by_logical = {item.logical_call_id: item.logical_slot_id for item in mapped}
        self.assertEqual(by_logical["logical-z"], "h1_h2:candidate_plan_batch:1")
        self.assertEqual(by_logical["logical-a"], "h1_h2:candidate_plan_batch:2")
        self.assertTrue(all(item.source_receipt_sha256 for item in mapped))
        legacy_payload = mapped[0].model_dump(mode="json")
        self.assertNotIn("error_category", legacy_payload)
        self.assertIsNone(
            RecoveryCallReceipt.model_validate(legacy_payload).error_category
        )

        failed = first.model_copy(
            update={
                "outcome": "provider_failure",
                "error_type": "ProviderError",
                "error_category": "unknown_provider",
            }
        )
        partial = map_model_call_receipts(
            [failed],
            campaign_id="campaign",
            round_id="round-01",
            require_complete=False,
        )
        self.assertEqual(partial[0].outcome, "provider_failure")
        self.assertEqual(partial[0].error_category, "unknown_provider")
        self.assertEqual(
            partial[0].source_receipt_sha256,
            canonical_sha256(failed.model_dump(mode="json")),
        )
        unsafe_payload = partial[0].model_dump(mode="json")
        unsafe_payload["error_category"] = "unsafe_detail"
        with self.assertRaises(ValueError):
            RecoveryCallReceipt.model_validate(unsafe_payload)

    def test_writer_content_repairs_keep_exactly_nine_logical_slots(self) -> None:
        for repaired_batch_count in (1, 2):
            with self.subTest(repaired_batch_count=repaired_batch_count):
                source = _all_model_receipts()
                repairs = []
                for repair_index, initial in enumerate(
                    source[-repaired_batch_count:],
                    start=1,
                ):
                    repairs.append(
                        initial.model_copy(
                            update={
                                "call_id": (
                                    f"writer-repair-{repaired_batch_count}-"
                                    f"{repair_index}"
                                ),
                                "attempt_index": 2,
                                "attempt_type": "content_repair",
                                "started_at": _timestamp(20 + repair_index * 2),
                                "completed_at": _timestamp(
                                    21 + repair_index * 2
                                ),
                                "response_sha256": (
                                    f"{200 + repaired_batch_count * 10 + repair_index:064x}"
                                ),
                                "provider_response_id_sha256": (
                                    f"{300 + repaired_batch_count * 10 + repair_index:064x}"
                                ),
                            }
                        )
                    )

                mapped = map_model_call_receipts(
                    source + repairs,
                    campaign_id="campaign",
                    round_id="round-01",
                )

                self.assertEqual(
                    {item.logical_slot_id for item in mapped},
                    set(FIRST_ROUND_LOGICAL_SLOTS),
                )
                self.assertEqual(
                    len({item.logical_call_id for item in mapped}),
                    9,
                )
                self.assertEqual(len(mapped), 9 + repaired_batch_count)
                repaired_slots = {
                    item.logical_slot_id
                    for item in mapped
                    if item.attempt_type == "content_repair"
                }
                expected_slots = set(
                    FIRST_ROUND_LOGICAL_SLOTS[-repaired_batch_count:]
                )
                self.assertEqual(repaired_slots, expected_slots)
                for slot in repaired_slots:
                    self.assertEqual(
                        sorted(
                            item.attempt_index
                            for item in mapped
                            if item.logical_slot_id == slot
                        ),
                        [1, 2],
                    )

    def test_successful_slot_rejects_non_content_followup(self) -> None:
        source = _all_model_receipts()
        invalid_followup = source[-1].model_copy(
            update={
                "call_id": "invalid-writer-followup",
                "attempt_index": 2,
                "attempt_type": "primary",
                "started_at": _timestamp(30),
                "completed_at": _timestamp(31),
                "response_sha256": "f" * 64,
                "provider_response_id_sha256": "e" * 64,
            }
        )
        with self.assertRaisesRegex(ValueError, "only with content repair"):
            map_model_call_receipts(
                source + [invalid_followup],
                campaign_id="campaign",
                round_id="round-01",
            )

    def test_controller_recomputes_hard_metrics_and_requires_six_of_six_ablations(self) -> None:
        store = self._store()
        campaign = store.load()
        reservation = store.reserve_round(owner_id="owner", lease_seconds=7200, now=NOW)
        packet, usage, receipts = self._packet_and_receipts(campaign, "round-01")
        replay = _replay(packet, all_degraded=True)
        hard = _hard_report(packet, passed=True)
        with (
            patch("hypoweaver.recovery_campaign.replay_ablations", return_value=replay),
            patch("hypoweaver.recovery_campaign.evaluate_hard_metrics", return_value=hard),
        ):
            campaign = store.finalize_evaluated_round(
                owner_id="owner",
                reservation_id=reservation.reservation_id,
                packet=packet,
                fault_replay=replay,
                hard_metric_report=hard,
                reference=self.reference,
                usage=usage,
                receipts=receipts,
                started_at=NOW,
                completed_at=LATER,
                now=LATER,
            )
        self.assertEqual(campaign.status, "qualified_seen_case")
        self.assertEqual(
            campaign.protocol_status,
            "hard-gate-qualified-on-seen-case",
        )
        self.assertTrue(all(campaign.rounds[0].hard_metric_results.values()))
        self.assertEqual(
            sum(campaign.rounds[0].ablation_target_degradation_results.values()),
            6,
        )

    def test_forged_report_is_invalidated_using_recomputed_content(self) -> None:
        store = self._store()
        campaign = store.load()
        reservation = store.reserve_round(owner_id="owner", lease_seconds=7200, now=NOW)
        packet, usage, receipts = self._packet_and_receipts(campaign, "round-01")
        replay = _replay(packet, all_degraded=True)
        forged = _hard_report(packet, passed=True)
        actual = _hard_report(packet, passed=False)
        with (
            patch("hypoweaver.recovery_campaign.replay_ablations", return_value=replay),
            patch("hypoweaver.recovery_campaign.evaluate_hard_metrics", return_value=actual),
        ):
            campaign = store.finalize_evaluated_round(
                owner_id="owner",
                reservation_id=reservation.reservation_id,
                packet=packet,
                fault_replay=replay,
                hard_metric_report=forged,
                reference=self.reference,
                usage=usage,
                receipts=receipts,
                started_at=NOW,
                completed_at=LATER,
                now=LATER,
            )
        self.assertEqual(campaign.status, "invalidated")
        self.assertEqual(campaign.rounds[0].status, "invalidated")
        self.assertEqual(campaign.rounds[0].usage.llm_calls, 9)
        self.assertEqual(campaign.rounds[0].hard_metric_results, {})

    def test_forged_source_receipt_falls_back_to_full_conservative_charge(self) -> None:
        store = self._store()
        campaign = store.load()
        reservation = store.reserve_round(owner_id="owner", lease_seconds=7200, now=NOW)
        packet, usage, receipts = self._packet_and_receipts(campaign, "round-01")
        receipts = (
            receipts[0].model_copy(update={"source_receipt_sha256": "9" * 64}),
            *receipts[1:],
        )
        replay = _replay(packet, all_degraded=True)
        hard = _hard_report(packet, passed=True)
        with (
            patch("hypoweaver.recovery_campaign.replay_ablations", return_value=replay),
            patch("hypoweaver.recovery_campaign.evaluate_hard_metrics", return_value=hard),
        ):
            campaign = store.finalize_evaluated_round(
                owner_id="owner",
                reservation_id=reservation.reservation_id,
                packet=packet,
                fault_replay=replay,
                hard_metric_report=hard,
                reference=self.reference,
                usage=usage,
                receipts=receipts,
                started_at=NOW,
                completed_at=LATER,
                now=LATER,
            )
        self.assertEqual(campaign.status, "invalidated")
        self.assertEqual(campaign.rounds, ())
        self.assertEqual(campaign.invalidation.conservative_llm_call_charge, 20)

    def test_failed_ablation_prevents_qualification_even_when_all_hard_metrics_pass(self) -> None:
        store = self._store()
        campaign = store.load()
        reservation = store.reserve_round(owner_id="owner", lease_seconds=7200, now=NOW)
        packet, usage, receipts = self._packet_and_receipts(campaign, "round-01")
        replay = _replay(packet, all_degraded=False)
        hard = _hard_report(packet, passed=True)
        with (
            patch("hypoweaver.recovery_campaign.replay_ablations", return_value=replay),
            patch("hypoweaver.recovery_campaign.evaluate_hard_metrics", return_value=hard),
        ):
            campaign = store.finalize_evaluated_round(
                owner_id="owner",
                reservation_id=reservation.reservation_id,
                packet=packet,
                fault_replay=replay,
                hard_metric_report=hard,
                reference=self.reference,
                usage=usage,
                receipts=receipts,
                started_at=NOW,
                completed_at=LATER,
                now=LATER,
            )
        self.assertEqual(campaign.status, "open")
        self.assertEqual(campaign.rounds[0].status, "hard_gate_failed")
        self.assertEqual(
            sum(campaign.rounds[0].ablation_target_degradation_results.values()),
            5,
        )

    def test_comparison_reservation_is_one_shot_and_expiry_charges_26(self) -> None:
        store = self._qualified_store()
        reservation = store.reserve_comparison(
            owner_id="comparison-a",
            lease_seconds=1,
            now=NOW,
        )
        self.assertIsNotNone(reservation)
        self.assertIsNone(store.reserve_comparison(owner_id="comparison-b", now=NOW))
        expired_at = (datetime.fromisoformat(NOW) + timedelta(seconds=2)).isoformat()
        self.assertIsNone(
            store.reserve_comparison(owner_id="comparison-a", now=expired_at)
        )
        campaign = store.load()
        self.assertEqual(campaign.status, "invalidated")
        self.assertEqual(campaign.invalidation.reservation_scope, "comparison")
        self.assertEqual(campaign.invalidation.conservative_llm_call_charge, 26)
        self.assertEqual(cumulative_llm_calls(campaign), 35)

    def test_store_uses_delivery_aliases_and_rejects_hash_chain_tampering(self) -> None:
        store = self._store()
        payload = json.loads(store.path.read_text(encoding="utf-8"))
        self.assertIn("protocol", payload)
        self.assertIn("round_manifests", payload)
        self.assertNotIn("freeze", payload)
        self.assertNotIn("rounds", payload)
        payload["protocol"]["case_id"] = "tampered"
        store.path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "freeze sha256 mismatch"):
            store.load()

    def test_recovery_code_never_calls_official_attempt_mutators(self) -> None:
        source = (
            Path(__file__).parents[1]
            / "src"
            / "hypoweaver"
            / "recovery_campaign.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "begin_official_attempt",
            "fail_official_attempt",
            "complete_official_attempt",
        ):
            self.assertNotIn(forbidden, source)

    def _prior(self, calls: int = 0):
        return import_prior_usage(
            self.binding,
            source_official_holdout_lock_id=official_holdout_lock_id(self.protocol),
            usage=RecoveryUsage(llm_calls=calls),
            official_receipt_sha256=tuple(f"{index + 100:064x}" for index in range(calls)),
            imported_at=NOW,
        )

    def _replacement_freeze(self, predecessor):
        return build_recovery_freeze(
            self.protocol,
            artifact_root=self.root,
            visible_input_path=self.visible_path,
            data_paths=(self.data_path,),
            reference_path=self.reference_path,
            reference_summary_path=self.summary_path,
            predecessor_campaign=predecessor,
            frozen_at=LATER,
        )

    def _store(self) -> RecoveryCampaignStore:
        store = RecoveryCampaignStore(self.root / f"campaign-{len(list(self.root.glob('campaign-*')))}.json")
        store.create(create_recovery_campaign(self.freeze, self._prior(), created_at=NOW))
        return store

    def _packet_and_receipts(self, campaign, round_id):
        model_receipts = _all_model_receipts()
        receipts = map_model_call_receipts(
            model_receipts,
            campaign_id=campaign.campaign_id,
            round_id=round_id,
        )
        usage = RecoveryUsage(
            llm_calls=9,
            input_tokens=sum(item.input_tokens for item in model_receipts),
            output_tokens=sum(item.output_tokens for item in model_receipts),
        )
        packet = seal_benchmark_packet(
            BenchmarkPacket(
                packet_id=f"packet-{round_id}",
                system_id="hypoweaver",
                case_id=self.freeze.case_id,
                visible_input_sha256=self.freeze.visible_input_sha256,
                data_sha256=list(self.freeze.data_sha256),
                model_id="qwen-plus",
                design=NormalizedDesign(),
                resource_usage=BenchmarkResourceUsage(
                    llm_calls=usage.llm_calls,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                ),
            )
        )
        return packet, usage, receipts

    def _qualified_store(self) -> RecoveryCampaignStore:
        store = self._store()
        campaign = store.load()
        reservation = store.reserve_round(owner_id="round-owner", lease_seconds=7200, now=NOW)
        packet, usage, receipts = self._packet_and_receipts(campaign, "round-01")
        replay = _replay(packet, all_degraded=True)
        hard = _hard_report(packet, passed=True)
        with (
            patch("hypoweaver.recovery_campaign.replay_ablations", return_value=replay),
            patch("hypoweaver.recovery_campaign.evaluate_hard_metrics", return_value=hard),
        ):
            store.finalize_evaluated_round(
                owner_id="round-owner",
                reservation_id=reservation.reservation_id,
                packet=packet,
                fault_replay=replay,
                hard_metric_report=hard,
                reference=self.reference,
                usage=usage,
                receipts=receipts,
                started_at=NOW,
                completed_at=LATER,
                now=LATER,
            )
        return store


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _timestamp(index: int) -> str:
    return (
        datetime(2026, 7, 16, tzinfo=timezone.utc) + timedelta(seconds=index)
    ).isoformat()


def _model_receipt(slot: str, index: int, *, logical_id: str | None = None) -> ModelCallReceipt:
    group, prompt_key, _slot_index = slot.split(":")
    return ModelCallReceipt(
        call_id=f"provider-call-{index}",
        logical_call_id=logical_id or f"logical-{index}",
        call_group=group,
        prompt_key=prompt_key,
        prompt_version="1.0.0",
        attempt_index=1,
        max_attempts=3,
        attempt_type="primary",
        outcome="succeeded",
        provider="qwen",
        model="qwen-plus",
        started_at=_timestamp(index),
        completed_at=_timestamp(index + 1),
        response_sha256=f"{index + 10:064x}",
        input_sha256=f"{index + 30:064x}",
        output_schema_sha256=f"{index + 50:064x}",
        provider_response_id_sha256=f"{index + 70:064x}",
        input_tokens=10,
        output_tokens=2,
    )


def _all_model_receipts() -> list[ModelCallReceipt]:
    return [
        _model_receipt(slot, index)
        for index, slot in enumerate(FIRST_ROUND_LOGICAL_SLOTS, start=1)
    ]


def _same_prompt_model_receipts() -> tuple[ModelCallReceipt, ModelCallReceipt]:
    slot = "h1_h2:candidate_plan_batch:1"
    first = _model_receipt(slot, 1, logical_id="logical-z")
    second = _model_receipt(slot, 2, logical_id="logical-a")
    return first, second


def _replay(packet: BenchmarkPacket, *, all_degraded: bool) -> FaultReplayReport:
    outcomes = [
        FaultOutcome(
            fault_id=fault_id,
            detected=True,
            action="block",
            evidence=[fault_id],
        )
        for fault_id in FAULT_IDS
    ]
    ablations = []
    for index, ablation_id in enumerate(ABLATION_IDS):
        ablations.append(
            AblationReplayResult(
                ablation_id=ablation_id,
                disabled_component=f"component-{index}",
                packet_sha256=str(packet.packet_sha256),
                target_fault_ids=[FAULT_IDS[index]],
                fault_outcomes=outcomes,
                detected_fault_count=9,
                target_fault_degraded=(all_degraded or index > 0),
            )
        )
    return FaultReplayReport(
        case_id=packet.case_id,
        clean_packet_sha256=str(packet.packet_sha256),
        full_system_outcomes=outcomes,
        clean_false_block_count=0,
        ablations=ablations,
    )


def _hard_report(packet: BenchmarkPacket, *, passed: bool) -> HardMetricReport:
    metrics = [
        HardMetric(
            metric_id=metric_id,
            numerator=int(passed or index > 0),
            denominator=1,
            value=float(passed or index > 0),
            target="test",
            passed=(passed or index > 0),
        )
        for index, metric_id in enumerate(HARD_METRIC_IDS)
    ]
    return HardMetricReport(
        report_id=f"hard-{packet.packet_id}",
        case_id=packet.case_id,
        packet_id=packet.packet_id,
        metrics=metrics,
        all_hard_gates_passed=all(metric.passed for metric in metrics),
        created_at=NOW,
    )


if __name__ == "__main__":
    unittest.main()

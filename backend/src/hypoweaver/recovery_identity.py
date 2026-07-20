from __future__ import annotations

import hashlib
from pathlib import Path

from . import benchmark_evaluator, benchmark_faults
from .benchmark_models import ABLATION_IDS, FAULT_IDS
from .prompts import get_prompt
from .research_api import runtime_identity
from .seal import canonical_sha256


FIRST_ROUND_LOGICAL_SLOTS = (
    "h1_h2:hypothesis_decomposition:1",
    "h1_h2:candidate_plan_batch:1",
    "h1_h2:candidate_plan_batch:2",
    "h1_h2:reviewer_report_batch:1",
    "h1_h2:reviewer_report_batch:2",
    "h3:evidence_claim_bundle:1",
    "h3:scientific_audit:1",
    "h4:manuscript_section_draft_batch:1",
    "h4:manuscript_section_draft_batch:2",
)


def evaluator_identity_sha256() -> str:
    return canonical_sha256(
        {
            "source_sha256": _file_sha256(Path(benchmark_evaluator.__file__)),
            "hard_metric_ids": [
                "contract_execution_fidelity",
                "required_step_terminal_rate",
                "required_evidence_completion",
                "fatal_fault_detection_rate",
                "clean_false_block_count",
                "protected_numeric_consistency",
                "statement_traceability",
                "causal_overreach_escape_count",
                "independent_replication_rate",
            ],
        }
    )


def fault_matrix_identity_sha256() -> str:
    return canonical_sha256(
        {
            "source_sha256": _file_sha256(Path(benchmark_faults.__file__)),
            "fault_ids": list(FAULT_IDS),
            "ablation_ids": list(ABLATION_IDS),
        }
    )


def prompt_registry_identity_sha256() -> str:
    prompt_keys = sorted({slot.split(":")[1] for slot in FIRST_ROUND_LOGICAL_SLOTS})
    return canonical_sha256(
        {
            key: {
                "version": prompt.version,
                "system": prompt.system,
                "user_template": prompt.user_template,
                "output_model": prompt.output_model.__name__,
                **prompt.call_policy(),
            }
            for key in prompt_keys
            for prompt in (get_prompt(key),)
        }
    )


def research_runtime_identity_sha256() -> str:
    return canonical_sha256(runtime_identity())


def hypoweaver_source_sha256() -> str:
    value = runtime_identity().get("source_sha256")
    if not isinstance(value, str) or len(value) != 64:
        raise RuntimeError("HypoWeaver runtime identity has no source_sha256")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

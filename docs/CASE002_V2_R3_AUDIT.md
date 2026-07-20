# Case002 v2 development suite r3 audit

## Status

Suite: `case002-v2-dev-20260719-r3`

Classification: development calibration only. Case002 is one seen development case and is not an independent benchmark sample. The suite cannot support a general claim that either system has stronger scientific ability.

The sealed r3 artifacts remain unchanged. All corrections below apply prospectively to r4.

## Four-cell outcome

| Input view | Agent Laboratory | HypoWeaver | Pair interpretation |
| --- | --- | --- | --- |
| discovery blind | Provider transport terminal after data preparation; no experiment or paper | H2 blocked before execution | No completion or artifact-quality comparison |
| reproduction aligned | Upstream stopped after data preparation; no accepted experiment code or paper | Completed execution, estimator-level NumPy reproduction and an audited draft | Completion comparison only; no paired scientific-output quality comparison |

The aligned HypoWeaver run preserved the frozen controls, fixed effects, cluster specification, group sensitivities, alternative outcome and permutation scheme. Its baseline and several sensitivity estimates were internally consistent. That fact does not validate the r3 causal interpretation because two P0 implementation errors affected the falsification layer.

## Why r3 is not a valid scientific-quality comparison

1. Agent Laboratory's frozen upstream code expected a Hugging Face `datasets` import, but the benchmark environment omitted the dependency and the sandbox correctly denied installation. This mixed an environment mismatch with workflow capability.
2. The discovery Agent cell ended in a provider transport failure. Infrastructure-excluded cells cannot form a paired comparison.
3. The aligned Agent cell produced no experiment or manuscript. It can be scored for task completion after the environment is fixed, but there is no artifact pair on which to compare method quality, inference, robustness or writing.
4. There is one independent case and one run seed. Repeated reviewers or four cells do not increase the number of independent research tasks.

## P0 scientific defects found in HypoWeaver r3

### Contaminated fake-time placebo

The 2004 pseudo-policy regression used the full 1998–2013 sample. Real policy-period observations therefore entered the pseudo-post period. Its significant coefficient cannot be interpreted as a clean pre-policy falsification.

r4 requirement: restrict the fake-time estimation sample to observations strictly before the true policy year, verify pseudo-pre and pseudo-post support, and report `true_policy_contamination_rows=0`, the placebo sample end year and excluded-row count. A regression test must show that changing outcomes after the true policy date cannot change the fake-time result.

### Missing remote pre-period bin

The aligned input required 1998–2001 to be combined into one remote lead. r3 generated only the 2002–2005 leads, so the joint pretrend test did not cover the full frozen pre-period design.

r4 requirement: encode the remote years structurally, generate one `event_remote_pre` term in both implementations, include it in the joint pretrend test and fail closed when requested years are unavailable.

### Event-study scale disclosure

The baseline policy exposure gives 2007 a weight of 0.42. The event study uses standard binary `treated × year` contrasts. r4 must freeze this as `event_term_scaling=binary_group_year_contrast` and explicitly state that the 2007 event coefficient is a year-specific treated-control contrast, not a per-unit policy-exposure coefficient. The two magnitudes must not be compared as if they shared a scale.

## Fairness, state and reporting defects

- An analysis-ready provided table was incorrectly blocked because its upstream raw-data ETL log was unavailable. For this given-input track, missing upstream ETL is a disclosure boundary; independent analysis-stage re-estimation begins from the frozen table.
- A shared cluster-specification concern was attached to only one otherwise identical candidate. r4 propagates issues across identical frozen invariants and records an explicit issue disposition, preventing candidate shopping.
- The primary ResearchRun labelled the later external reproduction placeholder `dependency_failed`. r4 uses `external_replication_pending`; the final outcome comes from ReproductionAudit.
- The policy reproducer uses a separate NumPy estimator and covariance implementation but shares analysis-table preparation and policy regressor construction. r4 reports `independence_scope=estimator_only`; it must not be described as an end-to-end independent reproduction.
- Provider-request latency, per-implementation statistical wall time and end-to-end cell elapsed time were conflated. r4 freezes them separately. Exceeding the cell limit is a system-capability budget failure, not benchmark infrastructure failure.
- The mixed Claim Gate wording changed “insufficient causal identification” into “unstable conditional association.” r4 preserves the observed statistical association while stating that conflicting identification or falsification evidence prevents a causal claim.
- Five manuscript sections fell back to generic text; visible years were redacted, execution UUIDs appeared in prose, sample counts repeated and punctuation duplicated. r4 admits only years traceable to visible input or frozen design, renders human-readable frozen step names, deduplicates sample facts and repairs deterministic anchor punctuation.

## r4 frozen interpretation rules

- `max_executions=12` means frozen DAG step slots per statistical implementation, not physical model fits or model calls.
- `max_wall_time_seconds=1800` is the ceiling for each statistical implementation phase.
- `max_end_to_end_wall_time_seconds=2700` is the benchmark system-cell ceiling.
- HypoWeaver retains 20 logical model calls and 40 provider attempts per view; each logical request allows at most three byte-identical technical attempts.
- Agent Laboratory retains 40 provider attempts. A cross-architecture logical-call ceiling is not imposed; its native upstream schedule remains frozen.
- Fake-time, assignment-unit permutation, fixed-last-pre group, stable-only sample, entity clustering, alternative outcome, event study and estimator-level reproduction are all registered before results are observed.

## Permitted r3 conclusion

The only defensible r3 statement is:

> On one seen Case002 development task, HypoWeaver completed the reproduction-aligned workflow while Agent Laboratory did not. The Agent environment was mismatched and the HypoWeaver falsification layer contained implementation defects, so r3 does not provide a valid comparison of scientific-output quality or general scientific ability.

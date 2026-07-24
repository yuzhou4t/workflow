# SixBench student operations rules

This repository distributes four independent assignments:

- one complete six-system matrix for Case 005;
- one complete six-system matrix for Case 007;
- one complete six-system matrix for Case 009 after a new frozen release exists;
- one frontend UI implementation task.

It is an operator handoff, not a place to modify a frozen benchmark after
seeing its output.

- Never commit API keys, runtime configuration, authorization receipts, case
  data, hidden evaluator assets, model responses, or run artifacts.
- A student's AI may prepare its own Windows/WSL2 isolation environment, but it
  must follow `docs/AI_ENVIRONMENT_CONTRACT_ZH.md` and return the required
  machine report. It may not invent a weaker isolation policy.
- Only Cases 005, 007, and 009 are executable assignments. External execution
  remains forbidden until a machine-local release package sets
  `execution_enabled=true` and every cryptographic and offline gate passes.
- RC10 and earlier releases are superseded for these delegated assignments.
  Case 009 must not reuse or resume any previous partial run.
- Never edit or resume a partial frozen run. Preserve it and ask the
  coordinator for a new immutable-release decision.
- Keep discovery/aligned views and native/common-executor boards separate.
- Return results through the coordinator's approved private channel, never by
  committing them to this repository.
- The UI assignment must use the exact clean base commit named by the
  coordinator. It must not change benchmark logic, backend scientific
  contracts, hidden-reference boundaries, or API schemas.

Human instructions live in `README.md`, `docs/`, and `assignments/`.

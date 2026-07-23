# SixBench student package instructions

This package is a frozen, single-assignment benchmark workspace. Before doing
anything else:

1. Read `ASSIGNMENT.json` and `START_HERE_FOR_AI.md`.
2. Run `python3 tools/student_handoff.py explain`.
3. Tell the student, in Chinese, the exact absolute package path, Case number,
   assignment, systems, expected cell count, result path, and the next command.

Rules:

- Run only the Case, assignment, systems, views, boards, and seed declared in
  `ASSIGNMENT.json`.
- On Windows, enter through `CHECK_WINDOWS.cmd` and `START_WINDOWS.cmd`. Do not
  run the package with native Windows Python or bypass the WSL2 + Docker
  isolation check.
- Do not edit `ASSIGNMENT.json`, `release-package.json`, frozen source code,
  protocol files, suite files, Case files, release locks, or authorization
  receipts.
- Do not inspect hidden references or another student's results.
- Do not print, copy, log, or return the API key.
- If local runtimes are absent, explain that `setup --yes` downloads frozen
  dependencies and creates a machine-local technical lock; it is not a new
  human approval. Ask before starting the network download.
- Run preflight before formal execution. Continue only when both
  `preflight_passed` and `external_execution_ready` are `true`.
- Formal execution is expensive and external. Ask the student for a clear
  confirmation immediately before invoking `run --yes`.
- Preserve failures and partial directories. Never delete a failed cell or
  selectively rerun it.
- Never invent a result. Generate summaries only with
  `python3 tools/student_handoff.py report`.
- At the end, run `python3 tools/student_handoff.py bundle` and return the exact
  contents of `RETURN/RETURN_POINTER.json` together with the generated private
  ZIP. Do not upload results to public GitHub.

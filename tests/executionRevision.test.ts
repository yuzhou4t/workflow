import { describe, expect, it } from 'vitest'
import { manuscriptQuality, returnedRevisionGate, revisionSeed } from '../src/components/ExecutionWorkspace'
import type { RunSnapshot, StepAttempt } from '../src/runtime/types'

function step(input: Partial<StepAttempt> & Pick<StepAttempt, 'id' | 'nodeId' | 'status'>): StepAttempt {
  return {
    attempt: 1,
    prompts: [],
    input: null,
    output: null,
    logs: [],
    ...input,
  }
}

function run(steps: StepAttempt[], currentNodeId: string): RunSnapshot {
  return {
    id: 'run-revision',
    version: 4,
    definitionId: 'app-a',
    definitionVersion: '1.0.0',
    caseId: 'case-1',
    caseName: '修订测试',
    mode: 'fixture',
    status: 'blocked',
    currentNodeId,
    executionStatus: 'not_started',
    scientificStatus: 'not_evaluated',
    planOnly: false,
    createdAt: '',
    updatedAt: '',
    steps,
    events: [],
    claims: [],
    allowedActions: [],
  }
}

describe('gate revision recovery', () => {
  it('prefills H1 from the waiting artifact instead of the later decision step', () => {
    const snapshot = run([
      step({
        id: 'h1-waiting',
        nodeId: 'h1_gate',
        status: 'waiting_human',
        input: { case_id: 'case-1', input_conflicts: [], missing_required_information: [] },
      }),
      step({
        id: 'h1-returned',
        nodeId: 'h1_gate',
        status: 'succeeded',
        input: { reviewed_artifacts: { research_package: 'hash' } },
        output: { gate: 'H1', action: 'revise' },
      }),
    ], 'input_validation')

    expect(returnedRevisionGate(snapshot)).toBe('H1')
    expect(JSON.parse(revisionSeed(snapshot, 'H1'))).toEqual({ case_id: 'case-1' })
  })

  it('increments the H2 plan version from the waiting analysis plan', () => {
    const snapshot = run([
      step({ id: 'h2-waiting', nodeId: 'h2_gate', status: 'waiting_human', input: { analysis_plan: { plan_version: 2, method_family: 'policy_causal' }, critic_report: { critical_issues: [] } } }),
      step({ id: 'h2-returned', nodeId: 'h2_gate', status: 'succeeded', output: { gate: 'H2', action: 'revise' } }),
    ], 'analysis_plan_merge')

    expect(returnedRevisionGate(snapshot)).toBe('H2')
    expect(JSON.parse(revisionSeed(snapshot, 'H2'))).toMatchObject({ plan_version: 3, method_family: 'policy_causal' })
  })

  it('does not revive an old revision after a newer gate decision', () => {
    const snapshot = run([
      step({ id: 'old-return', nodeId: 'h1_gate', status: 'succeeded', output: { gate: 'H1', action: 'revise' } }),
      step({ id: 'new-approval', nodeId: 'h1_gate', status: 'succeeded', output: { gate: 'H1', action: 'approve' } }),
    ], 'analysis_plan_merge')

    expect(returnedRevisionGate(snapshot)).toBeUndefined()
  })

  it('opens an H2 plan revision when the critics block the plan', () => {
    const snapshot = run([
      step({ id: 'plan', nodeId: 'analysis_plan_merge', status: 'succeeded', output: { plan_version: 1, method_family: 'panel_association' } }),
      step({ id: 'critics', nodeId: 'critic_merge', status: 'blocked', output: { verdict: 'blocked', issues: [{ severity: 'critical' }] } }),
    ], 'critic_merge')

    expect(returnedRevisionGate(snapshot)).toBe('H2')
    expect(JSON.parse(revisionSeed(snapshot, 'H2'))).toMatchObject({ plan_version: 2, method_family: 'panel_association' })
  })

  it('does not treat a short result card as a complete manuscript', () => {
    const snapshot = run([], 'complete')
    snapshot.status = 'completed'
    snapshot.manuscript = {
      version: 1,
      mode: 'full_manuscript',
      status: 'ready_for_human_review',
      researchPlan: '',
      sections: [{ id: 'abstract', title: '摘要', content: '短结果', status: 'generated', claimIds: [], runIds: [] }],
      disclosures: [],
      unresolvedIssues: [],
      auditResult: 'pass_with_no_critical_issues',
    }

    expect(manuscriptQuality(snapshot)).toEqual({ complete: false, characterCount: 3 })
  })
})

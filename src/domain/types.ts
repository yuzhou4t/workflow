export type NodeKind =
  | 'start'
  | 'document'
  | 'llm'
  | 'code'
  | 'gate'
  | 'router'
  | 'merge'
  | 'http'
  | 'end'
  | 'other'

export type IssueSeverity = 'blocker' | 'warning' | 'design'

export interface WorkflowIssue {
  id: string
  title: string
  detail: string
  severity: IssueSeverity
  nodeIds: string[]
}

export interface PromptPart {
  id: string
  role: string
  text: string
}

export interface ContractField {
  name: string
  label: string
  type: string
  required?: boolean
  sourceNodeId?: string
  sourceNodeTitle?: string
}

export interface WorkflowStage {
  id: string
  order: number
  title: string
  description: string
  nodeIds: string[]
}

export interface WorkflowNode {
  id: string
  title: string
  type: string
  kind: NodeKind
  stageId: string
  description: string
  position: { x: number; y: number }
  prompts: PromptPart[]
  inputs: ContractField[]
  outputs: ContractField[]
  outputSchema: unknown | null
  issueIds: string[]
  rawData: Record<string, unknown>
}

export interface WorkflowEdge {
  id: string
  source: string
  target: string
  sourceHandle?: string
  targetHandle?: string
  label?: string
  animated: boolean
}

export interface WorkflowStats {
  nodes: number
  edges: number
  llm: number
  gates: number
  routers: number
  merges: number
}

export interface WorkflowDefinition {
  id: 'app-a' | 'app-b'
  name: string
  description: string
  sourceFile: string
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
  stages: WorkflowStage[]
  issues: WorkflowIssue[]
  stats: WorkflowStats
}

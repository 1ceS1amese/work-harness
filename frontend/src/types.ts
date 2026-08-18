export type RiskLevel = 'low' | 'medium' | 'high' | 'critical'
export type AllocationMode = 'auto' | 'direct' | 'cross_functional'
export type AgentFunction = 'analysis' | 'research' | 'execution' | 'risk'

export interface AgentAssignment {
  mode: AllocationMode
  assignees: string[]
  functions_by_agent: Partial<Record<string, AgentFunction[]>>
  cross_review: boolean
}

export type RunStatus =
  | 'pending'
  | 'running'
  | 'waiting_human'
  | 'succeeded'
  | 'partial'
  | 'failed'
  | 'cancelled'

export interface TaskRequest {
  goal: string
  context?: Record<string, unknown>
  constraints?: string[]
  expected_output?: string | null
  task_type?: string
  risk_level?: RiskLevel
  assignment?: AgentAssignment
  idempotency_key?: string | null
}

export interface TaskSpec extends Omit<TaskRequest, 'idempotency_key'> {
  task_id: string
  assignment: AgentAssignment
  ambiguities: string[]
  created_at: string
}

export interface SubtaskSpec {
  subtask_id: string
  title: string
  description: string
  dependencies: string[]
  worker_role: string
  assigned_function: AgentFunction | null
  logical_model: string
  allowed_tools: string[]
  acceptance_criteria: string[]
  token_budget: number
  cost_budget_usd: number
}

export interface Plan {
  plan_id: string
  version: number
  subtasks: SubtaskSpec[]
  rationale: string
}

export interface Evidence {
  source: string
  locator: string | null
  summary: string
  tool_call_id: string | null
}

export interface Artifact {
  artifact_id: string
  run_id: string
  subtask_id: string
  producer: string
  version: number
  content: string
  evidence: Evidence[]
  assumptions: string[]
  unresolved_questions: string[]
  confidence: number
  status: 'success' | 'partial' | 'failed'
  created_at: string
  duration_ms: number
}

export interface ReviewIssue {
  code: string
  severity: 'info' | 'warning' | 'error' | 'critical'
  message: string
  evidence: string | null
  recommendation: string | null
}

export interface Review {
  review_id: string
  artifact_id: string
  artifact_version: number
  reviewer: string
  dimension: string
  verdict: 'pass' | 'revise' | 'reject'
  issues: ReviewIssue[]
  confidence: number
  created_at: string
}

export interface Decision {
  decision_id: string
  action: 'approve' | 'repair' | 'request_human' | 'fail'
  reasons: string[]
  target_subtasks: string[]
  human_prompt: string | null
  created_at: string
}

export interface Usage {
  input_tokens: number
  output_tokens: number
  cached_tokens: number
  estimated_cost_usd: number
}

export interface ModelCallRecord {
  node: string
  provider: string
  model: string
  input_tokens: number
  output_tokens: number
  cached_tokens: number
  estimated_cost_usd: number
  latency_ms: number
  retries: number
  fallback_from: string | null
}

export interface ToolCallRecord {
  node: string
  role: string
  tool_name: string
  call_id: string
  input_fingerprint: string
  output_summary: string
  truncated: boolean
  duration_ms: number
}

export interface RunError {
  code: string
  message: string
  node: string | null
  retryable: boolean
  occurred_at: string
}

export interface RunResult {
  run_id: string
  status: RunStatus
  output: string
  task: TaskSpec | null
  plan: Plan | null
  artifacts: Artifact[]
  reviews: Review[]
  decision: Decision | null
  errors: RunError[]
  repair_rounds: number
  usage: Usage
  model_calls: ModelCallRecord[]
  tool_calls: ToolCallRecord[]
}

export interface RunAccepted {
  run_id: string
  status_url: string
}

export interface HealthResponse {
  status: string
  providers: Record<string, unknown>
  checkpoint: string
  run_store: string
}

export interface CapabilitiesResponse {
  logical_models: string[]
  tools: string[]
  task_types: string[]
  allocation_modes: AllocationMode[]
  functions: AgentFunctionDescriptor[]
  agents: AgentDescriptor[]
}

export interface AgentFunctionDescriptor {
  id: AgentFunction
  display_name: string
  description: string
}

export interface AgentDescriptor {
  id: string
  display_name: string
  responsibility: string
  specialty: string | null
  assignable: boolean
}

export type MessageRole = 'user' | 'assistant'

export interface ChatMessage {
  id: string
  role: MessageRole
  content: string
  createdAt: string
  runId?: string
  status?: RunStatus
}

export interface Conversation {
  id: string
  title: string
  taskType: string
  riskLevel: RiskLevel
  allocationMode: AllocationMode
  selectedAgentIds: string[]
  functionsByAgent: Partial<Record<string, AgentFunction[]>>
  crossReview: boolean
  messages: ChatMessage[]
  createdAt: string
  updatedAt: string
}

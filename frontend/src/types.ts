export type RiskLevel = 'low' | 'medium' | 'high' | 'critical'
export type AllocationMode = 'auto' | 'direct' | 'cross_functional'
export type AgentFunction = 'analysis' | 'research' | 'execution' | 'risk'

export interface AgentAssignment {
  mode: AllocationMode
  assignees: string[]
  functions_by_agent: Partial<Record<string, AgentFunction[]>>
  cross_review: boolean
}

export interface SkillSelection {
  skill_id: string
  provider: string | null
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
  skills?: SkillSelection[]
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
  skill_ids?: string[]
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
  skill_ids?: string[]
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
  cache_hit: boolean
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
  provider_store: string
  provider_compatible: boolean
  scheduler: {
    active_runs: number
    queued_runs: number
    stream_subscribers: number
    run_max_concurrency: number
  }
}

export interface ProviderCompatibilityItem {
  provider: string
  adapter: string
  protocol_variant: 'openai' | 'deepseek'
  api_family: 'chat_completions' | 'responses'
  protocol_version: string
  capabilities: string[]
  configured_capabilities: string[]
  baseline_capabilities: string[]
  compatible: boolean
  issues: string[]
}

export type ProviderProtocolVariant = 'openai' | 'deepseek'
export type ProviderApiFamily = 'chat_completions' | 'responses'
export type ProviderCredentialSource = 'environment' | 'stored'

export interface ProviderConfig {
  adapter: string
  protocol_variant: ProviderProtocolVariant
  api_family: ProviderApiFamily
  structured_output_mode: 'json_schema' | 'json_object' | null
  chat_token_parameter: 'max_completion_tokens' | 'max_tokens' | null
  store_responses: boolean
  enabled: boolean
  base_url: string | null
  base_url_env: string | null
  credential_source: ProviderCredentialSource
  api_key_env: string | null
  allowed_models: string[]
  capabilities: string[]
  timeout_seconds: number
  max_concurrency: number
  allow_insecure_http: boolean
  allow_private_network: boolean
  options: Record<string, unknown>
}

export interface ProviderProfileSpec {
  config: Pick<ProviderConfig, 'adapter'> & Partial<Omit<ProviderConfig, 'adapter'>>
  model_bindings?: Record<string, string>
  agent_bindings?: Record<string, string>
  api_key?: string
  clear_api_key?: boolean
}

export interface ProviderDescriptor {
  name: string
  source: 'yaml' | 'persistent' | 'removed'
  config: ProviderConfig
  model_bindings: Record<string, string>
  agent_bindings: Record<string, string>
  loaded: boolean
  endpoint_available: boolean | null
  credential_available: boolean | null
}

export interface ProviderList {
  items: ProviderDescriptor[]
  restart_required: boolean
}

export interface ProviderSaved {
  provider: ProviderDescriptor
  restart_required: boolean
}

export interface ProviderContractDescriptor {
  protocol_version: string
  baseline_capabilities: string[]
  compatible: boolean
}

export interface SkillDescriptor {
  id: string
  description: string
  version: string
  logical_model: string
  required_capabilities: string[]
  allowed_tools: string[]
  compatible_providers: string[]
  preferred_providers: string[]
  available_providers: string[]
  applies_to: AgentFunction[]
}

export interface CapabilitiesResponse {
  logical_models: string[]
  tools: string[]
  tool_adapters: string[]
  provider_adapters: string[]
  provider_contract: ProviderContractDescriptor
  providers: ProviderCompatibilityItem[]
  skills: SkillDescriptor[]
  task_types: string[]
  allocation_modes: AllocationMode[]
  runtime_policy: {
    review_strategy: 'adaptive' | 'comprehensive' | 'three_dimensional'
    deep_review_risk_levels: RiskLevel[]
    deep_review_task_types: string[]
    run_max_concurrency: number
  }
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
  model_route: { provider: string; model: string } | null
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
  skillSelections: SkillSelection[]
  messages: ChatMessage[]
  createdAt: string
  updatedAt: string
}

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
  engine?: 'legacy' | 'blackboard'
  blackboard?: BlackboardTaskInput
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

export interface TaskSpec extends Omit<TaskRequest, 'idempotency_key' | 'engine' | 'blackboard'> {
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
  max_output_tokens: number
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
  usage_complete?: boolean
  estimated_cost_usd: number
}

export interface ModelCallRecord {
  node: string
  provider: string
  model: string
  input_tokens: number
  output_tokens: number
  cached_tokens: number
  usage_complete?: boolean
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

export interface AgentInteraction {
  interaction_id: string
  kind: 'assignment' | 'dependency' | 'repair' | 'result' | 'review'
  sender: string
  recipient: string
  subtask_id: string
  artifact_id: string | null
  artifact_version: number
  review_id: string | null
  repair_round: number
}

export interface RunResult {
  run_id: string
  // Older legacy responses omit these fields.
  engine?: 'legacy' | 'blackboard'
  pending_intents?: ObjectRef[]
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
  agent_interactions?: AgentInteraction[]
}

export interface RunAccepted {
  run_id: string
  status_url: string
}

export interface ObjectRef {
  object_id: string
  revision: number
}

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue }
export type ObjectKind = 'fact' | 'intent' | 'hint'
export type ClaimType = 'user_statement' | 'file_content' | 'tool_observation' | 'derived' | 'assessment' | 'delivery'

export interface AcceptanceCriterion {
  id: string
  description: string
  claim_key: string
  claim_type: ClaimType
  scope: Record<string, string>
  operator: 'exists' | 'equals' | 'at_least' | 'at_most'
  expected: JsonValue
  required_checks: string[]
}

export interface ContentRef {
  content_digest: string
  media_type: 'text/plain' | 'text/markdown' | 'application/json'
  size_bytes: number
}

interface KnowledgeObject {
  id: string
  board_id: string
  schema_version: number
  revision: number
  created_at: string
  created_by: string
  origin_activation_id: string | null
  content_digest: string
  scope: Record<string, string>
  topic: string
  tags: string[]
  derived_from: ObjectRef[]
  links: { relation: 'contributes_to' | 'refines' | 'supports' | 'contradicts' | 'supersedes'; target: ObjectRef }[]
  attachments: ContentRef[]
}

export interface BlackboardFact extends KnowledgeObject {
  kind: 'fact'
  statement: string
  claim_type: ClaimType
  claim_key: string | null
  value: JsonValue
  lifecycle: 'valid' | 'contested' | 'stale' | 'superseded' | 'expired' | 'retracted'
  valid_from: string | null
  valid_until: string | null
  premise_refs: ObjectRef[]
  evidence: {
    source: 'user_input' | 'file' | 'tool' | 'rule' | 'assessment' | 'delivery'
    locator: string
    content_digest: string
    description: string
  }[]
  verification: {
    receipt_id: string
    verifier_id: string
    verifier_version: string
    method: string
    verified_at: string
    content_digest: string
    checks: string[]
    premise_refs: ObjectRef[]
  }
}

export interface BlackboardIntent extends KnowledgeObject {
  kind: 'intent'
  objective: string
  root_intent_ref: ObjectRef | null
  parent_intent_ref: ObjectRef | null
  acceptance: AcceptanceCriterion[]
  priority: number
  status: 'open' | 'satisfied' | 'blocked' | 'abandoned'
  satisfied_by: ObjectRef[]
  blockers: ObjectRef[]
  status_reason: string
}

export interface BlackboardHint extends KnowledgeObject {
  kind: 'hint'
  suggestion: string
  applies_when: string
  rationale: string
  source_refs: ObjectRef[]
  confidence: number | null
  expires_at: string | null
  status: 'active' | 'consumed' | 'dismissed' | 'expired'
  status_reason: string
}

export type BoardObject = BlackboardFact | BlackboardIntent | BlackboardHint

export interface BoardFinalization {
  board_id: string
  status: 'succeeded' | 'partial' | 'failed' | 'cancelled'
  reason: string
  finalized_at_revision: number
  finalized_at: string
  output: string
  witnesses: ObjectRef[]
  missing_criteria: string[]
  attempted_directions: string[]
}

export interface BlackboardSummary {
  board_id: string
  revision: number
  evaluated_at: string
  counts: Record<ObjectKind, number>
  root_intent: BlackboardIntent
  goal: { satisfied: boolean; witnesses: ObjectRef[]; missing_criteria: string[]; conflicts: ObjectRef[] }
  finalization: BoardFinalization | null
  objects_cursor: string
  event_cursor: string
}

export interface BlackboardObjectQuery {
  kind?: ObjectKind
  status?: BlackboardFact['lifecycle'] | BlackboardIntent['status'] | BlackboardHint['status']
  topic?: string
  intent_id?: string
  at_revision?: number
  cursor?: string
  limit?: number
}

export interface BlackboardObjectPage {
  board_id: string
  revision: number
  filters: { kind: ObjectKind | null; status: string | null; topic: string | null; intent_id: string | null }
  items: BoardObject[]
  total: number
  limit: number
  next_cursor: string | null
}

export interface BoardEvent {
  board_id: string
  sequence: number
  source_id: string
  activation_id: string | null
  committed_at: string
  changed_refs: ObjectRef[]
  requested_changes: { target: ObjectRef; action: string; reason: string }[]
  event_type: 'knowledge' | 'finalized'
  finalization: BoardFinalization | null
  goal_revision: { target: ObjectRef; objective: string; acceptance: AcceptanceCriterion[]; reason: string } | null
}

export interface BlackboardObjectDetail {
  board_id: string
  revision: number
  item: { object: BoardObject; event: BoardEvent }
  activation?: {
    activation_id: string
    source_id: string
    source_version: string
    reason: string
    intent_ref: ObjectRef
    input_refs: ObjectRef[]
  } | null
}

export interface BlackboardHistoryPage {
  board_id: string
  revision: number
  object_id: string
  items: BlackboardObjectDetail['item'][]
  total: number
  limit: number
  next_cursor: string | null
}

export interface BlackboardContent {
  board_id: string
  revision: number
  content: { ref: ContentRef; body: string }
}

export interface RunList {
  items: {
    run_id: string
    status: RunStatus
    created_at: string
    updated_at: string
    engine: 'legacy' | 'blackboard'
    execution: { engine: 'legacy' | 'blackboard'; workflow_revision: string; schema_version: number; configuration_fingerprint: string } | null
  }[]
  limit: number
  offset: number
}

export interface HealthResponse {
  status: string
  providers: Record<string, unknown>
  checkpoint: string
  run_store: string
  provider_store: string
  settings_store: string
  provider_compatible: boolean
  scheduler: {
    active_runs: number
    queued_runs: number
    inflight_runs: number
    stream_subscribers: number
    run_max_concurrency: number
    run_queue_capacity: number
    run_queue_retry_after_seconds: number
    inflight_capacity: number
    available_admission_slots: number
    admitted_runs_total: number
    rejected_runs_total: number
    recovered_runs_total: number
    recovery_backlog_observed: number
    shared_queue?: {
      available_jobs: number
      leased_jobs: number
      dead_jobs: number
      completed_jobs: number
      cancelled_jobs: number
      incompatible_active_jobs: number
      oldest_available_seconds: number
    }
    live_workers?: number
    worker_capacity?: number
    worker_active_jobs?: number
    worker_config_consistent?: boolean
    shared_queue_healthy?: boolean
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

export interface PromptSettingDescriptor {
  name: string
  display_name: string
  description: string
  version: string
  body: string
  customized: boolean
  loaded: boolean
}

export interface EditableRuntimeSettings {
  run_max_concurrency: number
  run_queue_capacity: number
  run_queue_retry_after_seconds: number
  worker_max_concurrency: number
  reviewer_max_concurrency: number
  review_strategy: 'adaptive' | 'comprehensive' | 'three_dimensional'
  deep_review_risk_levels: RiskLevel[]
  deep_review_task_types: string[]
  max_planned_subtasks: number
  max_repair_rounds: number
  node_timeout_seconds: number
  run_timeout_seconds: number
  max_artifact_chars: number
  max_tool_result_chars: number
  model_retry_attempts: number
}

export interface ApplicationSettingsDescriptor {
  prompts: PromptSettingDescriptor[]
  runtime: EditableRuntimeSettings
  restart_required: boolean
  storage: string
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

export interface BlackboardTaskInput {
  topic: string
  acceptance: AcceptanceCriterion[]
}

export interface WorkflowCapabilities {
  default_engine: 'legacy' | 'blackboard'
  accept_new_blackboard_runs: boolean
  blackboard_templates: {
    id: string
    title: string
    description: string
    input: BlackboardTaskInput
  }[]
}

export interface CapabilitiesResponse {
  workflow?: WorkflowCapabilities
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

/** Response shapes from SPEC sections 2.3, 2.7 and 2.8. */
import type { StreamStats } from "./stream";

export type ModelRole = "chat" | "autocomplete" | "embedding";

export interface OpenAIModel {
  id: string;
  object: "model";
  owned_by: string;
  role?: ModelRole;
}

export interface ApiKey {
  id: number;
  name: string;
  prefix: string | null;
  rpm_limit: number;
  daily_token_quota: number;
  created_at: string;
  revoked_at: string | null;
  last_used_at: string | null;
  builtin: boolean;
  /** False for the built-in web-console key: it is metered, but its limits are not enforced. */
  limited: boolean;
}

export interface CreatedApiKey extends ApiKey {
  /** The plain key. Returned exactly once, at creation. */
  key: string;
}

export type UsageRange = "24h" | "7d" | "30d";

export interface UsageTotals {
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  errors: number;
}

export interface UsageBreakdownRow {
  label: string;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  errors: number;
}

export interface UsagePoint {
  /** Unix seconds, bucket start. */
  t: number;
  requests: number;
  tokens: number;
}

export interface UsageLatency {
  p50_ms: number | null;
  p95_ms: number | null;
  ttft_p50_ms: number | null;
  ttft_p95_ms: number | null;
  tokens_per_s_avg: number | null;
}

export interface QuotaRow {
  label: string;
  used: number;
  /** null when the key has no enforced quota (the web console). */
  quota: number | null;
}

export interface Usage {
  totals: UsageTotals;
  by_key: UsageBreakdownRow[];
  by_model: UsageBreakdownRow[];
  timeseries: UsagePoint[];
  latency: UsageLatency;
  today_by_key: QuotaRow[];
}

export type BackendState = "ready" | "sleeping" | "waking";

export interface AdminModel {
  alias: string;
  name: string;
  role: ModelRole;
  /** null while the backend sleeps: the gateway won't wake Ollama just to list tags. */
  pulled: boolean | null;
  loaded: boolean;
  size_vram: number | null;
}

export interface ModelsStatus {
  backend: BackendState;
  models: AdminModel[];
}

export interface GpuStats {
  name: string | null;
  utilization_percent: number | null;
  memory_used_bytes: number | null;
  memory_total_bytes: number | null;
  temperature_celsius: number | null;
  power_watts: number | null;
  power_limit_watts: number | null;
}

export interface SystemStatus {
  backend: BackendState;
  queue: { autocomplete: number; chat: number; batch: number };
  inflight: number;
  max_inflight: number | null;
  demand: number;
  idle_for_s: number | null;
  /** FORGE_IDLE_SECONDS: idle time after which demand drops and KEDA scales Ollama to zero. */
  idle_seconds: number | null;
  gpu: GpuStats | null;
}

export interface ChatMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

/** A saved chat message: the console keeps per-answer details next to the text. */
export interface StoredMessage extends ChatMessage {
  stats?: StreamStats;
  stopped?: boolean;
  /** Why the answer failed; set on an assistant message whose request errored. */
  error?: string;
}

export interface ConversationSummary {
  id: string;
  title: string;
  updated_at: string;
}

export interface Conversation extends ConversationSummary {
  model: string;
  messages: StoredMessage[];
  created_at: string;
}

export type RepoStatus = "queued" | "cloning" | "embedding" | "ready" | "failed";

export interface Repo {
  id: number;
  url: string;
  branch: string | null;
  status: RepoStatus;
  error: string | null;
  files_indexed: number;
  chunks: number;
  commit_sha: string | null;
  created_at: string;
  updated_at: string;
}

export interface RagSource {
  n: number;
  path: string;
  start_line: number;
  end_line: number;
  score: number;
}

export interface TokenUsage {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
}

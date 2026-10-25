import { api } from "./client";
import { normalizeModelsStatus, normalizeStatus, normalizeUsage } from "./normalize";
import type {
  ApiKey,
  Conversation,
  ConversationSummary,
  CreatedApiKey,
  ModelsStatus,
  OpenAIModel,
  Repo,
  StoredMessage,
  SystemStatus,
  Usage,
  UsageRange,
} from "./types";

export const auth = {
  me: () => api.get<{ authenticated: boolean }>("/api/auth/me"),
  login: (password: string) => api.post<unknown>("/api/auth/login", { password }, { skipAuthRedirect: true }),
  logout: () => api.post<unknown>("/api/auth/logout", {}, { skipAuthRedirect: true }),
};

export async function listOpenAIModels(signal?: AbortSignal): Promise<OpenAIModel[]> {
  const res = await api.get<{ data?: OpenAIModel[] }>("/v1/models", signal);
  return res.data ?? [];
}

export interface KeyLimits {
  rpm_limit?: number;
  daily_token_quota?: number;
}

export const keys = {
  list: () => api.get<ApiKey[]>("/api/keys"),
  create: (name: string, limits: KeyLimits) => api.post<CreatedApiKey>("/api/keys", { name, ...limits }),
  update: (id: number, limits: KeyLimits) => api.patch<ApiKey>(`/api/keys/${id}`, limits),
  revoke: (id: number) => api.del(`/api/keys/${id}`),
};

export async function getUsage(range: UsageRange, signal?: AbortSignal): Promise<Usage> {
  return normalizeUsage(await api.get<unknown>(`/api/usage?range=${range}`, signal));
}

export async function getModelsStatus(signal?: AbortSignal): Promise<ModelsStatus> {
  return normalizeModelsStatus(await api.get<unknown>("/api/models", signal));
}

export async function getStatus(signal?: AbortSignal): Promise<SystemStatus> {
  return normalizeStatus(await api.get<unknown>("/api/status", signal));
}

export const conversations = {
  list: () => api.get<ConversationSummary[]>("/api/conversations"),
  get: (id: string) => api.get<Conversation>(`/api/conversations/${encodeURIComponent(id)}`),
  save: (id: string, body: { title: string; model: string; messages: StoredMessage[] }) =>
    api.put<Conversation>(`/api/conversations/${encodeURIComponent(id)}`, body),
  remove: (id: string) => api.del(`/api/conversations/${encodeURIComponent(id)}`),
};

export const repos = {
  list: () => api.get<Repo[]>("/api/repos"),
  create: (url: string, branch: string | null) => api.post<Repo>("/api/repos", { url, branch }),
  reindex: (id: number) => api.post<Repo>(`/api/repos/${id}/reindex`),
  remove: (id: number) => api.del(`/api/repos/${id}`),
};

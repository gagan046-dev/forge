import { api } from "./client";
import { normalizeModelsStatus, normalizeStatus, normalizeUsage } from "./normalize";
import type {
  ApiKey,
  Conversation,
  ConversationSummary,
  CreatedApiKey,
  GitStatus,
  ModelsStatus,
  OpenAIModel,
  Repo,
  StoredMessage,
  SystemStatus,
  Usage,
  UsageRange,
  Workspace,
  WorkspaceEntry,
  WorkspaceFile,
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

const ws = (name: string) => `/api/workspaces/${encodeURIComponent(name)}`;
const q = (path: string) => `path=${encodeURIComponent(path)}`;

export const workspaces = {
  list: () => api.get<Workspace[]>("/api/workspaces"),
  create: (name: string, gitUrl: string | null, branch: string | null) =>
    api.post<Workspace>("/api/workspaces", { name, git_url: gitUrl, branch }),
  remove: (name: string) => api.del(ws(name)),
  tree: (name: string) => api.get<{ entries: WorkspaceEntry[]; truncated: boolean }>(`${ws(name)}/tree`),
  read: (name: string, path: string) => api.get<WorkspaceFile>(`${ws(name)}/file?${q(path)}`),
  write: (name: string, path: string, content: string) => api.put<{ path: string; size: number }>(`${ws(name)}/file`, { path, content }),
  mkdir: (name: string, path: string) => api.post<{ path: string }>(`${ws(name)}/folder`, { path }),
  move: (name: string, from: string, to: string) => api.post<{ path: string }>(`${ws(name)}/move`, { from, to }),
  removeEntry: (name: string, path: string) => api.del(`${ws(name)}/entry?${q(path)}`),
  search: (name: string, query: string) =>
    api.get<{ hits: { path: string; line: number; text: string }[]; truncated: boolean }>(
      `${ws(name)}/search?q=${encodeURIComponent(query)}`,
    ),
  archiveUrl: (name: string) => `${ws(name)}/archive.zip`,
  gitStatus: (name: string) => api.get<GitStatus>(`${ws(name)}/git`),
  gitDiff: (name: string) => api.get<{ diff: string; truncated: boolean }>(`${ws(name)}/git/diff`),
  setRemote: (name: string, url: string) => api.post<{ remote: string }>(`${ws(name)}/git/remote`, { url }),
  commit: (name: string, message: string, authorName: string, authorEmail: string) =>
    api.post<{ sha: string; summary: string }>(`${ws(name)}/git/commit`, {
      message,
      author_name: authorName,
      author_email: authorEmail,
    }),
  push: (name: string, token: string | null) =>
    api.post<{ pushed: boolean; branch: string; output: string }>(`${ws(name)}/git/push`, { token }),
};

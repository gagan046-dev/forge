/**
 * Fetch wrapper for the Forge gateway.
 *
 * The SPA is served from the same origin as the gateway, so the admin session
 * cookie rides along with `credentials: "same-origin"`. The gateway rejects
 * cookie-authenticated writes without `X-Forge-Csrf: 1` (a header a cross-site
 * form cannot set), so every request carries it.
 */

export class ApiError extends Error {
  readonly status: number;
  /** OpenAI-style error type, e.g. "rate_limit_exceeded"; "network_error" when no response arrived. */
  readonly type: string;
  readonly code: string | null;
  /** Seconds from the Retry-After header, when the server sent one. */
  readonly retryAfter: number | null;

  constructor(opts: { status: number; type: string; message: string; code?: string | null; retryAfter?: number | null }) {
    super(opts.message);
    this.name = "ApiError";
    this.status = opts.status;
    this.type = opts.type;
    this.code = opts.code ?? null;
    this.retryAfter = opts.retryAfter ?? null;
  }
}

const STATUS_TYPES: Record<number, string> = {
  400: "invalid_request_error",
  401: "invalid_api_key",
  403: "forbidden",
  404: "not_found",
  409: "conflict",
  422: "invalid_request_error",
  429: "rate_limit_exceeded",
  499: "superseded",
  503: "backend_unavailable",
  504: "queue_timeout",
};

function parseRetryAfter(value: string | null): number | null {
  if (!value) return null;
  const seconds = Number(value);
  if (Number.isFinite(seconds)) return Math.max(0, Math.round(seconds));
  const date = Date.parse(value);
  if (Number.isNaN(date)) return null;
  return Math.max(0, Math.round((date - Date.now()) / 1000));
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

/**
 * Turns an error response into an ApiError. Handles the gateway's OpenAI-shaped
 * errors ({"error": {...}}), FastAPI's {"detail": "..."} and validation lists
 * ({"detail": [{"msg": ...}]}), and plain-text bodies from proxies.
 */
export function toApiError(status: number, bodyText: string, headers: Headers): ApiError {
  let type = STATUS_TYPES[status] ?? (status >= 500 ? "server_error" : "request_error");
  let code: string | null = null;
  let message: string | null = null;

  let body: unknown = null;
  try {
    body = bodyText ? JSON.parse(bodyText) : null;
  } catch {
    body = null;
  }

  if (body && typeof body === "object") {
    const record = body as Record<string, unknown>;
    const err = record.error;
    if (err && typeof err === "object") {
      const e = err as Record<string, unknown>;
      type = asString(e.type) ?? type;
      code = asString(e.code) ?? (typeof e.code === "number" ? String(e.code) : null);
      message = asString(e.message);
    } else if (typeof err === "string") {
      message = err;
    }
    const detail = record.detail;
    if (!message && typeof detail === "string") {
      message = detail;
    } else if (!message && Array.isArray(detail)) {
      message = detail
        .map((d) => {
          const item = d as Record<string, unknown>;
          const loc = Array.isArray(item.loc) ? item.loc.filter((p) => p !== "body").join(".") : "";
          const msg = asString(item.msg) ?? "invalid value";
          return loc ? `${loc}: ${msg}` : msg;
        })
        .join("; ");
    } else if (!message && detail && typeof detail === "object") {
      const d = detail as Record<string, unknown>;
      message = asString(d.message);
      type = asString(d.type) ?? type;
    }
  } else if ([502, 503, 504].includes(status)) {
    // A non-JSON 5xx comes from the ingress or dev proxy, not the gateway:
    // the gateway itself is down or restarting.
    type = "gateway_unavailable";
    message = `The gateway is not responding (HTTP ${status}).`;
  } else if (bodyText && bodyText.length < 300 && !bodyText.trimStart().startsWith("<")) {
    message = bodyText.trim();
  }

  return new ApiError({
    status,
    type,
    code,
    message: message ?? `Request failed with HTTP ${status}`,
    retryAfter: parseRetryAfter(headers.get("retry-after")),
  });
}

function withPeriod(text: string): string {
  return /[.!?]$/.test(text) ? text : `${text}.`;
}

/** A short, human explanation of an error, suitable for an inline banner. */
export function describeError(err: unknown): string {
  if (!(err instanceof ApiError)) {
    return err instanceof Error ? err.message : String(err);
  }
  const retry = err.retryAfter !== null ? ` Retry in ${err.retryAfter}s.` : "";
  switch (err.type) {
    case "network_error":
    case "gateway_unavailable":
      return "Could not reach the gateway. Check that the forge-gateway pods are running.";
    case "queue_full":
      return `The GPU queue is full.${retry}`;
    case "rate_limit_exceeded":
      return `Rate limit reached for this key.${retry}`;
    case "quota_exceeded":
      return `Daily token quota used up for this key.${retry}`;
    case "backend_unavailable":
      return `The inference backend did not wake up in time.${retry}`;
    case "queue_timeout":
      return "Waited too long for a GPU slot. The queue is busy; try again shortly.";
    default:
      // Any other refusal that says when to come back (another proxy's 429, a 503) gets the hint too.
      return retry && (err.status === 429 || err.status === 503) ? `${withPeriod(err.message)}${retry}` : err.message;
  }
}

type UnauthorizedListener = () => void;
let onUnauthorized: UnauthorizedListener | null = null;

/** The auth context registers this so an expired session sends the user to the login page. */
export function setUnauthorizedHandler(listener: UnauthorizedListener | null): void {
  onUnauthorized = listener;
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
  /** Login itself answers 401 for a wrong password; that must not trigger a redirect. */
  skipAuthRedirect?: boolean;
  headers?: Record<string, string>;
}

/** Performs a request and returns the raw Response when it is 2xx; otherwise throws ApiError. */
export async function request(path: string, opts: RequestOptions = {}): Promise<Response> {
  const headers: Record<string, string> = { "X-Forge-Csrf": "1", Accept: "application/json", ...opts.headers };
  let body: BodyInit | undefined;
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }

  let response: Response;
  try {
    response = await fetch(path, {
      method: opts.method ?? (body ? "POST" : "GET"),
      headers,
      body,
      credentials: "same-origin",
      signal: opts.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError({ status: 0, type: "network_error", message: "Network request failed" });
  }

  if (!response.ok) {
    const text = await response.text().catch(() => "");
    const error = toApiError(response.status, text, response.headers);
    if (response.status === 401 && !opts.skipAuthRedirect && path.startsWith("/api/")) {
      onUnauthorized?.();
    }
    throw error;
  }
  return response;
}

async function json<T>(path: string, opts: RequestOptions): Promise<T> {
  const response = await request(path, opts);
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export const api = {
  get: <T>(path: string, signal?: AbortSignal) => json<T>(path, { signal }),
  post: <T>(path: string, body?: unknown, opts: Omit<RequestOptions, "body" | "method"> = {}) =>
    json<T>(path, { ...opts, method: "POST", body: body ?? {} }),
  put: <T>(path: string, body: unknown) => json<T>(path, { method: "PUT", body }),
  patch: <T>(path: string, body: unknown) => json<T>(path, { method: "PATCH", body }),
  del: <T = void>(path: string) => json<T>(path, { method: "DELETE" }),
};

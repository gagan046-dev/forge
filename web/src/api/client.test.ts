import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, describeError, request, setUnauthorizedHandler, toApiError } from "./client";

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  setUnauthorizedHandler(null);
});

describe("toApiError", () => {
  it("maps an OpenAI-shaped error with Retry-After", () => {
    const err = toApiError(
      429,
      JSON.stringify({ error: { message: "GPU queue is full", type: "queue_full", code: "queue_full" } }),
      new Headers({ "Retry-After": "7" }),
    );
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(429);
    expect(err.type).toBe("queue_full");
    expect(err.code).toBe("queue_full");
    expect(err.message).toBe("GPU queue is full");
    expect(err.retryAfter).toBe(7);
    expect(describeError(err)).toBe("The GPU queue is full. Retry in 7s.");
  });

  it("maps FastAPI string details", () => {
    const err = toApiError(404, JSON.stringify({ detail: "Conversation not found" }), new Headers());
    expect(err.type).toBe("not_found");
    expect(err.message).toBe("Conversation not found");
  });

  it("flattens FastAPI validation errors and drops the body location prefix", () => {
    const body = { detail: [{ loc: ["body", "rpm_limit"], msg: "Input should be greater than 0", type: "greater_than" }] };
    const err = toApiError(422, JSON.stringify(body), new Headers());
    expect(err.type).toBe("invalid_request_error");
    expect(err.message).toBe("rpm_limit: Input should be greater than 0");
  });

  it("treats a non-JSON 502/503/504 as the gateway being down, not a gateway error", () => {
    const html = toApiError(502, "<html><body>Bad Gateway</body></html>", new Headers());
    expect(html.type).toBe("gateway_unavailable");
    expect(describeError(html)).toMatch(/Could not reach the gateway/);
    const json = toApiError(504, JSON.stringify({ error: { message: "waited too long", type: "queue_timeout" } }), new Headers());
    expect(json.type).toBe("queue_timeout");
  });

  it("falls back to the status for other HTML or empty bodies", () => {
    const err = toApiError(500, "<html>oops</html>", new Headers());
    expect(err.type).toBe("server_error");
    expect(err.message).toBe("Request failed with HTTP 500");
  });

  it("reads an HTTP-date Retry-After", () => {
    const later = new Date(Date.now() + 30_000).toUTCString();
    const err = toApiError(503, "{}", new Headers({ "Retry-After": later }));
    expect(err.type).toBe("backend_unavailable");
    expect(err.retryAfter).toBeGreaterThanOrEqual(28);
    expect(err.retryAfter).toBeLessThanOrEqual(31);
  });

  it("keeps a short plain-text body as the message", () => {
    expect(toApiError(400, "model is required", new Headers()).message).toBe("model is required");
  });
});

describe("request", () => {
  it("sends the CSRF header, same-origin credentials and a JSON body", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200, { id: 1 }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.post<{ id: number }>("/api/keys", { name: "vigil" });
    expect(result).toEqual({ id: 1 });

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/keys");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
    const headers = init.headers as Record<string, string>;
    expect(headers["X-Forge-Csrf"]).toBe("1");
    expect(headers["Content-Type"]).toBe("application/json");
    expect(init.body).toBe('{"name":"vigil"}');
  });

  it("sends the CSRF header on GET too", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(200, []));
    vi.stubGlobal("fetch", fetchMock);
    await api.get("/api/keys");
    const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect((init.headers as Record<string, string>)["X-Forge-Csrf"]).toBe("1");
    expect(init.method).toBe("GET");
    expect(init.body).toBeUndefined();
  });

  it("returns undefined for 204 responses", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 204 })));
    await expect(api.del("/api/keys/3")).resolves.toBeUndefined();
  });

  it("throws a typed error and calls the unauthorized handler on an /api 401", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(401, { detail: "Not authenticated" })));
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);

    await expect(api.get("/api/usage?range=24h")).rejects.toMatchObject({ status: 401, message: "Not authenticated" });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("does not redirect when login itself answers 401", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(401, { detail: "Wrong password" })));
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);

    await expect(api.post("/api/auth/login", { password: "x" }, { skipAuthRedirect: true })).rejects.toBeInstanceOf(ApiError);
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it("maps a network failure to network_error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("Failed to fetch"))));
    const err = await request("/api/status").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).type).toBe("network_error");
    expect((err as ApiError).status).toBe(0);
  });

  it("rethrows aborts unchanged so callers can tell a stop from a failure", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new DOMException("aborted", "AbortError"))));
    const err = await request("/v1/chat/completions").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(DOMException);
    expect((err as DOMException).name).toBe("AbortError");
  });
});

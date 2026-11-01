/**
 * Typed API client (types generated from cloud/openapi.json: `npm run api:types`).
 *
 * The access token lives in memory only. The refresh token is an HttpOnly cookie that
 * the API sets at login, so page scripts never see it; on load and whenever a call
 * returns 401 we ask /v1/auth/refresh for a new access token.
 *
 * Pages embedded in the operational system (another site's frame) get no cookie, so
 * there the refresh token is kept in memory too and sent in the body (plan 8).
 */
import createClient from "openapi-fetch";

import type { components, paths } from "@/api/schema";

export type Schemas = components["schemas"];

/** "" = same origin (production: nginx proxies /v1 to the API). */
export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/$/, "");

let accessToken: string | null = null;
let embeddedRefreshToken: string | null = null;
let tenantId: string | null = null;
let refreshTimer: ReturnType<typeof setTimeout> | undefined;
let lastActivity = Date.now();
const tokenListeners = new Set<(token: string | null) => void>();

/** Proactive refresh only while the user is around, so the server's idle timeout applies. */
const ACTIVE_WITHIN_MS = 15 * 60 * 1000;

export function noteActivity(): void {
  lastActivity = Date.now();
}

export function getAccessToken(): string | null {
  return accessToken;
}

export function setAccessToken(token: string | null, expiresInSeconds?: number): void {
  accessToken = token;
  clearTimeout(refreshTimer);
  if (token && expiresInSeconds) {
    refreshTimer = setTimeout(
      () => {
        if (Date.now() - lastActivity < ACTIVE_WITHIN_MS) void refreshAccessToken();
      },
      Math.max(expiresInSeconds - 60, 10) * 1000,
    );
  }
  tokenListeners.forEach((listener) => listener(token));
}

export function onTokenChange(listener: (token: string | null) => void): () => void {
  tokenListeners.add(listener);
  return () => tokenListeners.delete(listener);
}

/** Super-admins act on one tenant at a time (sent as X-Tenant-Id). */
export function getTenantId(): string | null {
  return tenantId;
}

export function setTenantId(id: string | null): void {
  tenantId = id;
}

/** Embedded pages only: the session's refresh token (null: use the cookie). */
export function setEmbeddedRefreshToken(token: string | null): void {
  embeddedRefreshToken = token;
}

let refreshing: Promise<boolean> | null = null;

/** Swap the refresh cookie (or the embedded page's refresh token) for a new access token.
 * Concurrent callers share one call. */
export function refreshAccessToken(): Promise<boolean> {
  refreshing ??= (async () => {
    try {
      const embedded = embeddedRefreshToken;
      const response = await fetch(`${API_URL}/v1/auth/refresh`, {
        method: "POST",
        credentials: "include",
        ...(embedded && {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token: embedded }),
        }),
      });
      if (!response.ok) {
        setAccessToken(null);
        return false;
      }
      const body = (await response.json()) as Schemas["TokenPair"];
      if (embedded) embeddedRefreshToken = body.refresh_token;
      setAccessToken(body.access_token, body.expires_in);
      return true;
    } catch {
      return false;
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

function withAuth(request: Request): Request {
  const headers = new Headers(request.headers);
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  if (tenantId) headers.set("X-Tenant-Id", tenantId);
  return new Request(request, { headers });
}

/** fetch with auth headers, retrying once after a token refresh on 401. */
export async function authFetch(input: Request): Promise<Response> {
  const retry = input.clone();
  let response = await fetch(withAuth(input));
  const isAuthCall = new URL(input.url, window.location.href).pathname.startsWith("/v1/auth/");
  if (response.status === 401 && !isAuthCall && (await refreshAccessToken())) {
    response = await fetch(withAuth(retry));
  }
  return response;
}

/** fetch for non-JSON calls (images, CSV, file uploads). Paths start with /v1. */
export function rawFetch(path: string, init: RequestInit = {}): Promise<Response> {
  return authFetch(new Request(`${API_URL}${path}`, { credentials: "include", ...init }));
}

export const api = createClient<paths>({
  baseUrl: API_URL || undefined,
  credentials: "include",
  fetch: authFetch,
});

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

/** Human-readable message from a FastAPI error body. */
export function errorMessage(body: unknown, status: number): string {
  const detail = (body as { detail?: unknown } | undefined)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d: { loc?: unknown[]; msg?: string }) => {
        const field = (d.loc ?? []).filter((p) => p !== "body" && p !== "query").join(".");
        return field ? `${field}: ${d.msg}` : (d.msg ?? "");
      })
      .join("; ");
  }
  return `Request failed (${status})`;
}

/** Await an openapi-fetch call: its data, or an ApiError. */
export async function unwrap<T>(
  call: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await call;
  if (!response.ok) throw new ApiError(response.status, errorMessage(error, response.status));
  return data as T;
}

/** Throws an ApiError for a failed raw response. */
export async function ensureOk(response: Response): Promise<Response> {
  if (response.ok) return response;
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = undefined;
  }
  throw new ApiError(response.status, errorMessage(body, response.status));
}

/** WebSocket URL for an API path. */
export function wsUrl(path: string): string {
  const base = API_URL || window.location.origin;
  return base.replace(/^http/, "ws") + path;
}

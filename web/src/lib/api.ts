export class ApiError extends Error {
  status: number;
  detail?: unknown;

  constructor(message: string, status: number, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

const API_ROOT = import.meta.env.VITE_API_ROOT ?? "/api/v1";

async function readBody(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    return response.json();
  }
  const text = await response.text();
  return text ? { detail: text } : null;
}

export async function apiRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (!(init.body instanceof FormData) && init.body && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  headers.set("accept", "application/json");

  const response = await fetch(`${API_ROOT}${path}`, {
    credentials: "same-origin",
    ...init,
    headers,
  });
  const body = await readBody(response);
  if (!response.ok) {
    const detail = body && typeof body === "object" && "detail" in body ? (body as { detail?: unknown }).detail : body;
    const message = typeof detail === "string" ? detail : `Request failed (${response.status})`;
    throw new ApiError(message, response.status, detail);
  }
  return body as T;
}

export async function apiRequestFirst<T>(paths: string[], init: RequestInit = {}): Promise<T> {
  let lastError: unknown;
  for (const path of paths) {
    try {
      return await apiRequest<T>(path, init);
    } catch (error) {
      lastError = error;
      if (!(error instanceof ApiError) || error.status !== 404) throw error;
    }
  }
  throw lastError;
}

export function listFrom<T>(payload: unknown, keys: string[] = []): T[] {
  if (Array.isArray(payload)) return payload as T[];
  if (!payload || typeof payload !== "object") return [];
  const object = payload as Record<string, unknown>;
  for (const key of ["items", "results", "data", ...keys]) {
    if (Array.isArray(object[key])) return object[key] as T[];
  }
  return [];
}

export function totalFrom(payload: unknown, items: unknown[]): number {
  if (payload && typeof payload === "object") {
    const total = (payload as Record<string, unknown>).total;
    if (typeof total === "number") return total;
  }
  return items.length;
}

export function toQuery(values: Record<string, string | number | boolean | undefined>): string {
  const query = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== "") query.set(key, String(value));
  });
  const value = query.toString();
  return value ? `?${value}` : "";
}

export const apiEventUrl = `${API_ROOT}/events`;

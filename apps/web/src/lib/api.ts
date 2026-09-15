import type { ApiError } from "./types";

export class AssariumApiError extends Error {
  code: string;
  details: Record<string, unknown>;
  status: number;

  constructor(status: number, error: ApiError) {
    super(error.message);
    this.name = "AssariumApiError";
    this.status = status;
    this.code = error.code;
    this.details = error.details ?? {};
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });

  if (!response.ok) {
    let payload: { error?: ApiError; detail?: unknown } = {};
    try {
      payload = await response.json();
    } catch {
      // Fall through to the generic message below.
    }
    throw new AssariumApiError(
      response.status,
      payload.error ?? {
        code: "http_error",
        message:
          typeof payload.detail === "string"
            ? payload.detail
            : `The server returned ${response.status}.`,
        details: {},
      },
    );
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body ?? {}) }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body ?? {}) }),
  del: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};

/** Build a repeated `path=` query string for the browse/schema/sample endpoints. */
export function pathQuery(path: string[], extra?: Record<string, string | number>) {
  const params = new URLSearchParams();
  path.forEach((segment) => params.append("path", segment));
  Object.entries(extra ?? {}).forEach(([key, value]) => params.set(key, String(value)));
  const query = params.toString();
  return query ? `?${query}` : "";
}

/**
 * Fetch a file from a POST endpoint and hand it to the browser's download flow.
 *
 * Exports need the current filter set in the body, so a plain anchor href cannot
 * express them; the blob is created and revoked here instead.
 */
export async function downloadFile(path: string, body: unknown): Promise<void> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!response.ok) {
    throw new AssariumApiError(response.status, {
      code: "export_failed",
      message: "The export could not be produced.",
      details: {},
    });
  }

  const disposition = response.headers.get("Content-Disposition") ?? "";
  const match = /filename="?([^"]+)"?/.exec(disposition);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = match?.[1] ?? "assarium-export";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

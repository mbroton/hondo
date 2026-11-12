export const API_BASE: string = import.meta.env.VITE_API_BASE ?? "/api"

export class ApiError extends Error {
  readonly status: number
  // The server's `detail` string, when it sent one. Stable enough to branch
  // on, e.g. to tell `connection_in_use` from other 409s. Never shown to the
  // user as-is.
  readonly detail: string | null
  readonly retryAfter: number | null

  constructor(
    status: number,
    detail?: string | null,
    message?: string,
    retryAfter: number | null = null
  ) {
    super(message ?? `API request failed with status ${status}`)
    this.name = "ApiError"
    this.status = status
    this.detail = detail ?? null
    this.retryAfter = retryAfter
  }
}

async function errorDetail(response: Response): Promise<string | null> {
  try {
    const body: unknown = await response.json()
    if (
      typeof body === "object" &&
      body !== null &&
      "detail" in body &&
      typeof body.detail === "string"
    ) {
      return body.detail
    }
  } catch {
    // A non-JSON error body is normal (proxy errors, gateway pages).
  }
  return null
}

export async function apiFetch<T>(
  path: string,
  init?: RequestInit
): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    // Same-origin requests send cookies anyway; this keeps the session
    // when VITE_API_BASE points at a cross-origin API.
    credentials: "include",
    ...init,
    headers: {
      Accept: "application/json",
      ...init?.headers,
    },
  })

  if (!response.ok) {
    const retryAfter = Number(response.headers.get("Retry-After"))
    throw new ApiError(
      response.status,
      await errorDetail(response),
      undefined,
      Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : null
    )
  }

  if (response.status === 204) {
    return undefined as T
  }

  return (await response.json()) as T
}

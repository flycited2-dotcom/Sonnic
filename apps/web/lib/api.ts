export class ApiError extends Error {
  constructor(public code: string) { super(code); }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, {
      ...init, credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", ...init.headers },
      signal: AbortSignal.timeout(8000),
    });
  } catch { throw new ApiError("connection_failed"); }
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new ApiError(typeof error.detail === "string" ? error.detail : "connection_failed");
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

export type Locale = "ru-RU" | "en-US";
export type Config = { ai_mode: "demo" | "disabled" | "real"; supported_locales: Locale[] };
export type Result = {
  title: string; symbols: {label: string; evidence: string}[]; emotional_tone: string;
  summary: string; key_connection: string; reflection_question: string;
  locked_sections: string[]; mode: "demo" | "real"; locale: Locale;
  evidence_basis: "template_not_ai" | "model_from_user_text"; research_retrieval_used: false;
};
export type Job = {
  dream_id: string; status: "queued" | "processing" | "completed" | "failed";
  stage: "queued" | "extracting" | "composing" | "completed" | "failed";
  result: Result | null; error: string | null; can_retry: boolean;
};

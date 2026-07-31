/**
 * Backend error envelope parsing — the single entry point every page should
 * use to turn a rejected request into user-facing copy.
 *
 * The API answers failures with `{"error": {code, message, detail}}`, which
 * axios buries under `error.response.data`.  An `instanceof Error` check alone
 * therefore reports axios' own "Request failed with status code 409" and drops
 * both the operator-facing message and the code that tells 409 apart from 404,
 * so the envelope MUST be inspected before falling back to `Error.message`.
 */

import type { ErrorResponse } from "@/types/api";

const DEFAULT_FALLBACK = "接口暂不可用";

/** Copy for failures that never reached the server.
 *
 * axios describes these in English ("Network Error", "timeout of 30000ms
 * exceeded"); surfacing that verbatim puts English into a Chinese UI, and it
 * tells the user nothing they can act on either.
 */
const TRANSPORT_FAILURE_COPY: Record<string, string> = {
  ERR_NETWORK: "网络连接失败，请检查网络后重试。",
  ECONNABORTED: "请求超时，请稍后重试。",
  ETIMEDOUT: "请求超时，请稍后重试。",
  ERR_CANCELED: "请求已取消。",
};

function readProperty(value: unknown, key: string): unknown {
  return typeof value === "object" && value !== null
    ? Reflect.get(value, key)
    : undefined;
}

function nonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function serverResponse(error: unknown): object | null {
  const response = readProperty(error, "response");
  return typeof response === "object" && response !== null ? response : null;
}

function errorEnvelope(error: unknown): Partial<ErrorResponse["error"]> | null {
  const envelope = readProperty(
    readProperty(serverResponse(error), "data"),
    "error",
  );
  return typeof envelope === "object" && envelope !== null
    ? (envelope as Partial<ErrorResponse["error"]>)
    : null;
}

/**
 * Best available human-readable message for a rejected request.
 *
 * Resolution order: envelope message, FastAPI `detail`, `Error.message` for
 * transport failures, a plain string rejection, then `fallback`.
 */
export function getErrorMessage(
  error: unknown,
  fallback: string = DEFAULT_FALLBACK,
): string {
  const envelopeMessage = nonEmptyString(errorEnvelope(error)?.message);
  if (envelopeMessage) return envelopeMessage;
  const detail = nonEmptyString(
    readProperty(readProperty(serverResponse(error), "data"), "detail"),
  );
  if (detail) return detail;
  // Once the server has answered, Error.message is only axios' English status
  // line, which tells the operator less than the caller's own fallback copy.
  if (serverResponse(error) === null) {
    // Checked before Error.message: axios tags transport failures with a code
    // and an English message, whereas an Error raised deliberately by our own
    // code carries no code and a message worth showing.
    const transport = TRANSPORT_FAILURE_COPY[String(readProperty(error, "code"))];
    if (transport) return transport;
    if (error instanceof Error) {
      const message = nonEmptyString(error.message);
      if (message) return message;
    }
  }
  return nonEmptyString(error) ?? fallback;
}

/** HTTP status of a rejected request, or `null` when it never reached one. */
export function getErrorStatus(error: unknown): number | null {
  // Coerced rather than type-guarded: a status arriving as a string (some
  // mocks, some proxies) must still reach the callers that branch on 409.
  const status = Number(readProperty(serverResponse(error), "status"));
  return Number.isFinite(status) && status > 0 ? status : null;
}

/** Machine-readable envelope code, for branching on the failure kind. */
export function getErrorCode(error: unknown): string | null {
  return nonEmptyString(errorEnvelope(error)?.code);
}

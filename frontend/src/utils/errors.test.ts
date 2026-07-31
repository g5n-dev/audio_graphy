import { describe, expect, it } from "vitest";
import { getErrorCode, getErrorMessage, getErrorStatus } from "./errors";

/** Shaped like a rejected axios request: an Error carrying the HTTP response. */
function axiosError(status: number, data: unknown): Error {
  return Object.assign(
    new Error(`Request failed with status code ${status}`),
    { isAxiosError: true, response: { status, data } },
  );
}

function envelope(code: string, message: string): Record<string, unknown> {
  return { error: { code, message, detail: {} } };
}

describe("getErrorMessage", () => {
  it("prefers the backend envelope over the axios Error message", () => {
    const error = axiosError(409, envelope("conflict", "标签已被其他人更新"));
    expect(getErrorMessage(error)).toBe("标签已被其他人更新");
  });

  it("falls back to a FastAPI detail string", () => {
    const error = axiosError(422, { detail: "请求体校验失败" });
    expect(getErrorMessage(error)).toBe("请求体校验失败");
  });

  it("keeps axios' status line out of the UI when the envelope is unusable", () => {
    const error = axiosError(500, envelope("internal_error", "   "));
    expect(getErrorMessage(error, "接口异常")).toBe("接口异常");
  });

  it("translates transport failures instead of leaking axios' English copy", () => {
    const network = Object.assign(new Error("Network Error"), { code: "ERR_NETWORK" });
    const timeout = Object.assign(new Error("timeout of 30000ms exceeded"), {
      code: "ECONNABORTED",
    });
    expect(getErrorMessage(network)).toBe("网络连接失败，请检查网络后重试。");
    expect(getErrorMessage(timeout)).toBe("请求超时，请稍后重试。");
  });

  it("still surfaces an Error our own code raised deliberately", () => {
    // No axios code, so the message is ours and worth showing verbatim.
    expect(getErrorMessage(new Error("录音已被其他接待占用"))).toBe(
      "录音已被其他接待占用",
    );
  });

  it("accepts a plain string rejection", () => {
    expect(getErrorMessage("扫描超时")).toBe("扫描超时");
  });

  it("returns the fallback for a non-Error rejection", () => {
    expect(getErrorMessage(null, "登录失败")).toBe("登录失败");
    expect(getErrorMessage({})).toBe("接口暂不可用");
  });
});

describe("getErrorStatus", () => {
  it("reads the HTTP status of a rejected request", () => {
    expect(getErrorStatus(axiosError(409, envelope("conflict", "冲突")))).toBe(
      409,
    );
  });

  it("returns null when the request never reached a response", () => {
    expect(getErrorStatus(new Error("网络中断"))).toBeNull();
    expect(getErrorStatus({})).toBeNull();
  });

  it("coerces a string status so 409 branches still fire", () => {
    // Some mocks and proxies hand the status back as a string; callers that
    // branch on a stale-revision 409 must not silently stop firing.
    expect(getErrorStatus({ response: { status: "409" } })).toBe(409);
  });
});

describe("getErrorCode", () => {
  it("exposes the envelope code so 409 stays distinguishable from 404", () => {
    expect(
      getErrorCode(axiosError(409, envelope("deployment_conflict", "冲突"))),
    ).toBe("deployment_conflict");
  });

  it("returns null without an envelope", () => {
    expect(getErrorCode(axiosError(404, { detail: "Not Found" }))).toBeNull();
    expect(getErrorCode(new Error("网络中断"))).toBeNull();
  });
});

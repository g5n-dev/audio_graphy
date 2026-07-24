import { describe, expect, it } from "vitest";
import { resolveApiProxyTarget } from "./apiProxy";

describe("resolveApiProxyTarget", () => {
  it("uses the host-development backend by default", () => {
    expect(resolveApiProxyTarget({})).toBe("http://localhost:8000");
  });

  it("uses the Compose service target without exposing it to the browser", () => {
    expect(
      resolveApiProxyTarget({
        VITE_API_PROXY_TARGET: " http://backend:8000/ ",
      }),
    ).toBe("http://backend:8000");
  });

  it("rejects relative proxy targets before the dev server starts", () => {
    expect(() =>
      resolveApiProxyTarget({ VITE_API_PROXY_TARGET: "/api/v1" }),
    ).toThrow(/absolute http/);
  });
});

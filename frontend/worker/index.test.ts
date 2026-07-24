import { describe, expect, it } from "vitest";
import worker from "./index";

async function request(
  path: string,
  init?: RequestInit,
): Promise<Response> {
  return worker.fetch(
    new Request(`https://demo.example${path}`, init),
    undefined,
  );
}

describe("Sites demo worker", () => {
  it("returns a safe 404 for static requests when no assets binding exists", async () => {
    const response = await request("/");
    expect(response.status).toBe(404);
  });

  it("serves a coherent authenticated reception dataset", async () => {
    const login = await request("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: "demo@audiography.cn",
        password: "demo123",
      }),
    });
    expect(login.status).toBe(200);
    expect(await login.json()).toMatchObject({
      access_token: "demo-access-token",
      user: { tenant_id: "tenant-demo" },
    });

    const queue = await request("/api/v1/receptions");
    const queuePayload = await queue.json();
    expect(queuePayload).toMatchObject({
      total: 1,
      items: [{ id: 101 }],
    });

    const workspace = await request("/api/v1/receptions/101/workspace");
    const workspacePayload = await workspace.json();
    expect(workspacePayload.reception.id).toBe(101);
    expect(workspacePayload.recordings).toHaveLength(4);
    expect(workspacePayload.dialogue_units).toHaveLength(8);
  });

  it("keeps every core demo action on a successful contract path", async () => {
    const paths = [
      "/api/v1/receptions/101/automation/run",
      "/api/v1/receptions/101/merge",
      "/api/v1/receptions/101/segment",
      "/api/v1/receptions/101/dialogue-units/1001/split",
      "/api/v1/receptions/101/dialogue-units/1001/merge",
      "/api/v1/receptions/101/dialogue-tags/derive",
    ];

    for (const path of paths) {
      const response = await request(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      expect(response.status, path).toBe(200);
    }

    const automation = await request(
      "/api/v1/receptions/101/automation",
    );
    expect(await automation.json()).toMatchObject({
      reception_id: 101,
      status: "ready",
      stage: "ready",
    });
  });

  it("discovers and accepts an explainable demo reception candidate", async () => {
    const discovery = await request(
      "/api/v1/receptions/proposals/discover",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ store_id: "上海静安旗舰店" }),
      },
    );
    expect(await discovery.json()).toMatchObject({
      total: 1,
      scanned_recordings: 4,
      items: [
        {
          candidate_type: "merge_group",
          recording_ids: [5001, 5002],
          decision: "merge",
        },
      ],
    });

    const accepted = await request(
      "/api/v1/receptions/proposals/accept",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          scenario: "gold",
          recording_ids: [5001, 5002],
          merge_mode: "logical",
        }),
      },
    );
    const acceptedPayload = await accepted.json();
    expect(acceptedPayload.id).toBe(101);
    expect(acceptedPayload.recordings[0]).toMatchObject({
      recording_id: 5001,
    });
  });
});

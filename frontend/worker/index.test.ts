import { beforeEach, describe, expect, it } from "vitest";
import worker, { type Env } from "./index";
import type {
  D1Database,
  D1PreparedStatement,
  D1Result,
  D1RunResult,
} from "./durableState";

class FakeD1Statement implements D1PreparedStatement {
  private values: unknown[] = [];

  constructor(
    private readonly database: FakeD1Database,
    private readonly query: string,
  ) {}

  bind(...values: unknown[]): D1PreparedStatement {
    this.values = values;
    return this;
  }

  async first<T>(): Promise<T | null> {
    if (this.query.includes("INSERT INTO demo_sequences")) {
      const namespace = String(this.values[0]);
      const initialValue = Number(this.values[1]);
      const current = this.database.sequences.get(namespace);
      const value = current === undefined ? initialValue : current + 1;
      this.database.sequences.set(namespace, value);
      return { value } as T;
    }
    if (
      this.query.includes("SELECT payload_json") &&
      this.query.includes("record_id = ?")
    ) {
      const key = `${String(this.values[0])}:${String(this.values[1])}`;
      const payload = this.database.records.get(key);
      return payload ? ({ payload_json: payload } as T) : null;
    }
    return null;
  }

  async all<T>(): Promise<D1Result<T>> {
    if (
      this.query.includes("SELECT payload_json") &&
      this.query.includes("FROM demo_records")
    ) {
      const namespace = String(this.values[0]);
      const results = [...this.database.records.entries()]
        .filter(([key]) => key.startsWith(`${namespace}:`))
        .map(([, payload_json]) => ({ payload_json })) as T[];
      return { success: true, results };
    }
    if (this.query.includes("FROM demo_audit_events")) {
      return {
        success: true,
        results: [...this.database.audits].reverse() as T[],
      };
    }
    return { success: true, results: [] };
  }

  async run(): Promise<D1RunResult> {
    if (this.query.includes("INSERT INTO demo_records")) {
      const namespace = String(this.values[0]);
      const recordId = String(this.values[1]);
      this.database.records.set(
        `${namespace}:${recordId}`,
        String(this.values[3]),
      );
    } else if (this.query.includes("INSERT INTO demo_audit_events")) {
      this.database.audits.push({
        id: this.database.audits.length + 1,
        tenant_id: String(this.values[0]),
        action: String(this.values[1]),
        resource_type: String(this.values[2]),
        resource_id: String(this.values[3]),
        detail_json: String(this.values[4]),
        created_at: String(this.values[5]),
      });
    }
    return { success: true };
  }
}

class FakeD1Database implements D1Database {
  readonly records = new Map<string, string>();
  readonly sequences = new Map<string, number>();
  readonly audits: Array<Record<string, unknown>> = [];

  prepare(query: string): D1PreparedStatement {
    return new FakeD1Statement(this, query);
  }

  async batch(statements: D1PreparedStatement[]): Promise<D1RunResult[]> {
    return Promise.all(statements.map((statement) => statement.run()));
  }
}

let demoDb: FakeD1Database;

async function request(
  path: string,
  init?: RequestInit,
  env?: Env,
): Promise<Response> {
  return worker.fetch(
    new Request(`https://demo.example${path}`, init),
    env ?? { DB: demoDb },
  );
}

describe("Sites demo worker", () => {
  beforeEach(() => {
    demoDb = new FakeD1Database();
  });

  it("enforces and explains the graph induced-edge response budget", async () => {
    const response = await request("/api/v1/graph/explore?edge_limit=1");

    expect(response.status).toBe(200);
    const body = (await response.json()) as {
      edges: unknown[];
      edge_window: {
        total: number;
        returned: number;
        truncated: boolean;
        render_budget: number;
      };
    };
    expect(body.edges).toHaveLength(1);
    expect(body.edge_window).toEqual({
      total: 10,
      returned: 1,
      truncated: true,
      render_budget: 1,
    });

    const rejected = await request("/api/v1/graph/explore?edge_limit=5001");
    expect(rejected.status).toBe(422);
  });

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

    const automation = await request("/api/v1/receptions/101/automation");
    expect(await automation.json()).toMatchObject({
      reception_id: 101,
      status: "ready",
      stage: "ready",
      target_labels: [
        "stage",
        "intent",
        "objection",
        "next_step",
        "compliance_risk",
      ],
    });
  });

  it("serves range-safe WAV sources without exposing bytes outside the requested grant", async () => {
    const response = await request(
      "/api/v1/receptions/101/recordings/5001/audio?grant=demo-source-5001",
      { headers: { Range: "bytes=0-43" } },
    );

    expect(response.status).toBe(206);
    expect(response.headers.get("Content-Type")).toBe("audio/wav");
    expect(response.headers.get("Accept-Ranges")).toBe("bytes");
    expect(response.headers.get("Content-Range")).toMatch(/^bytes 0-43\/\d+$/);
    expect(response.headers.get("X-Audio-Time-Origin-Ms")).toBe("0");
    expect(response.headers.get("X-Audio-Valid-Source-Range-Ms")).toBe(
      "0-105000",
    );
    const header = new Uint8Array(await response.arrayBuffer());
    expect(new TextDecoder().decode(header.slice(0, 4))).toBe("RIFF");
    expect(new TextDecoder().decode(header.slice(8, 12))).toBe("WAVE");

    const outside = await request(
      "/api/v1/receptions/101/recordings/5001/audio?grant=demo-source-5001",
      { headers: { Range: "bytes=999999999-" } },
    );
    expect(outside.status).toBe(416);
  });

  it("plans integer timeline geometry and replays one asynchronous audio operation", async () => {
    const planResponse = await request(
      "/api/v1/receptions/101/audio-plans",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_version: 4,
          sources: [
            { mapping_id: 6001, gap_before_ms: 0 },
            { mapping_id: 6002, gap_before_ms: 1500 },
          ],
        }),
      },
    );
    expect(planResponse.status).toBe(200);
    const plan = await planResponse.json();
    expect(plan).toMatchObject({
      timeline_revision: 5,
      total_duration_ms: 211_500,
      physical_eligible: true,
      warnings: [],
      sources: [
        {
          mapping_id: 6001,
          recording_id: 5001,
          sequence_no: 0,
          source_start_ms: 0,
          source_end_ms: 105_000,
          gap_before_ms: 0,
          timeline_start_ms: 0,
          timeline_end_ms: 105_000,
        },
        {
          mapping_id: 6002,
          recording_id: 5002,
          sequence_no: 1,
          source_start_ms: 0,
          source_end_ms: 105_000,
          gap_before_ms: 1500,
          timeline_start_ms: 106_500,
          timeline_end_ms: 211_500,
        },
      ],
    });
    expect(plan.plan_token).toEqual(expect.any(String));

    const operationRequest = {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": "audio-op-e2e-1",
      },
      body: JSON.stringify({
        plan_token: plan.plan_token,
        mode: "both",
        expected_version: 4,
      }),
    };
    const created = await request(
      "/api/v1/receptions/101/audio-operations",
      operationRequest,
    );
    expect(created.status).toBe(202);
    const operation = await created.json();
    expect(operation).toMatchObject({
      reception_id: 101,
      status: "queued",
      mode: "both",
      progress: 0,
    });

    const replay = await request(
      "/api/v1/receptions/101/audio-operations",
      operationRequest,
    );
    expect(await replay.json()).toMatchObject({ id: operation.id });

    const completed = await request(
      `/api/v1/receptions/101/audio-operations/${operation.id}`,
    );
    expect(await completed.json()).toMatchObject({
      id: operation.id,
      status: "succeeded",
      progress: 1,
    });
  });

  it("rejects invalid audio timeline proposals before creating artifacts", async () => {
    const duplicate = await request(
      "/api/v1/receptions/101/audio-plans",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_version: 4,
          sources: [
            { mapping_id: 6001, gap_before_ms: 0 },
            { mapping_id: 6001, gap_before_ms: 0 },
          ],
        }),
      },
    );
    expect(duplicate.status).toBe(422);

    const stale = await request("/api/v1/receptions/101/audio-plans", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        expected_version: 3,
        sources: [{ mapping_id: 6001, gap_before_ms: 0 }],
      }),
    });
    expect(stale.status).toBe(409);
  });

  it("rejects durable mutations when the D1 binding is unavailable", async () => {
    const response = await request(
      "/api/v1/receptions/101/automation/run",
      { method: "POST", body: "{}" },
      {},
    );
    expect(response.status).toBe(503);
    expect(await response.json()).toMatchObject({
      error: { code: "DEMO_PERSISTENCE_UNAVAILABLE" },
    });
  });

  it("persists dialogue-tag derivation and reports no-op only on replay", async () => {
    const init = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        group_key: "reception-rules",
        group_version: "rules-v1",
        target_labels: [
          "stage",
          "intent",
          "objection",
          "next_step",
          "compliance_risk",
        ],
      }),
    };
    const first = await request(
      "/api/v1/receptions/101/dialogue-tags/derive",
      init,
    );
    expect(await first.json()).toMatchObject({
      no_op: false,
      requested_labels: [
        "stage",
        "intent",
        "objection",
        "next_step",
        "compliance_risk",
      ],
    });

    const replay = await request(
      "/api/v1/receptions/101/dialogue-tags/derive",
      init,
    );
    expect(await replay.json()).toMatchObject({ no_op: true });
  });

  it("persists an evidence-bound manual correction into the demo workspace", async () => {
    const correction = await request(
      "/api/v1/receptions/101/dialogue-tags/8001",
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_reception_version: 4,
          expected_group_version: "v3.2",
          label_value: "复核迎宾",
          reason: "复听原音后人工确认",
          evidence_ref_ids: ["stage-0"],
        }),
      },
    );

    expect(correction.status).toBe(200);
    expect(await correction.json()).toMatchObject({
      reception_id: 101,
      reception_version: 5,
      assignment: {
        label_key: "stage",
        label_value: "复核迎宾",
        source: "manual",
        is_current: true,
      },
    });

    const workspace = await request("/api/v1/receptions/101/workspace");
    const payload = await workspace.json();
    expect(payload.reception.version).toBe(5);
    expect(payload.dialogue_units[0]).toMatchObject({
      business_stage: "复核迎宾",
      edit_status: "manual_edited",
      version: 2,
    });
    expect(payload.state_transitions[0]).toMatchObject({
      id: 9001,
      from_state: "复核迎宾",
      to_state: "需求发现",
      trigger: "开放式提问",
    });
    expect(payload.state_transitions).toHaveLength(7);
    expect(payload.tag_assignments).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          label_key: "stage",
          label_value: "复核迎宾",
          source: "manual",
          is_current: true,
        }),
      ]),
    );

    const insights = await request("/api/v1/reception-tag-insights");
    const insightPayload = await insights.json();
    expect(insightPayload.evidence_summary[0]).toMatchObject({
      reception_id: 101,
      dialogue_unit_id: 1001,
      label_key: "stage",
      label_value: "复核迎宾",
      confidence: 1,
    });

    const stale = await request("/api/v1/receptions/101/dialogue-tags/8001", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        expected_reception_version: 4,
        expected_group_version: "v3.2",
        label_value: "旧草稿",
        reason: "模拟并发冲突",
        evidence_ref_ids: ["stage-0"],
      }),
    });
    expect(stale.status).toBe(409);
  });

  it("updates only the affected stage edge and its successor in the demo chain", async () => {
    const before = await request("/api/v1/receptions/101/workspace");
    const beforePayload = await before.json();
    const previousTransitions = structuredClone(
      beforePayload.state_transitions,
    );

    const correction = await request(
      "/api/v1/receptions/101/dialogue-tags/8003",
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_reception_version: 4,
          expected_group_version: "v3.2",
          label_value: "深度需求",
          reason: "复听后确认已完成深度需求澄清",
          evidence_ref_ids: ["stage-1"],
        }),
      },
    );
    expect(correction.status).toBe(200);

    const after = await request("/api/v1/receptions/101/workspace");
    const afterPayload = await after.json();
    expect(afterPayload.state_transitions).toHaveLength(
      previousTransitions.length,
    );
    expect(afterPayload.state_transitions[0]).toMatchObject({
      id: previousTransitions[0].id,
      from_state: "初次接触",
      to_state: "深度需求",
      trigger: "manual_tag_correction",
      algorithm_version: "manual-tag-correction-v1",
    });
    expect(afterPayload.state_transitions[1]).toMatchObject({
      id: previousTransitions[1].id,
      from_state: "深度需求",
      to_state: "方案推荐",
      trigger: previousTransitions[1].trigger,
      algorithm_version: previousTransitions[1].algorithm_version,
    });
    expect(afterPayload.state_transitions.slice(2)).toEqual(
      previousTransitions.slice(2),
    );
  });

  it("discovers and accepts an explainable demo reception candidate", async () => {
    const discovery = await request("/api/v1/receptions/proposals/discover", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ store_id: "上海静安旗舰店" }),
    });
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

    const accepted = await request("/api/v1/receptions/proposals/accept", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        scenario: "gold",
        recording_ids: [5001, 5002],
        merge_mode: "logical",
      }),
    });
    const acceptedPayload = await accepted.json();
    expect(acceptedPayload.id).toBe(101);
    expect(acceptedPayload.recordings[0]).toMatchObject({
      recording_id: 5001,
    });
  });

  it("persists tag-governance writes and exposes their audit trail", async () => {
    const created = await request("/api/v1/tag-schemas", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        key: "automotive-reception",
        name: "汽车销售接待",
      }),
    });
    expect(created.status).toBe(201);
    const createdPayload = await created.json();

    const versionResponse = await request(
      `/api/v1/tag-schemas/${createdPayload.id}/versions`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          version: "v1",
          definitions: [
            {
              key: "customer_temperature",
              name: "客户热度",
              category: "intent",
              value_type: "string",
              allowed_values: [],
              subject_types: ["reception"],
              scenarios: ["automotive"],
              evidence_required: true,
              critical: false,
              threshold: 0.75,
            },
          ],
        }),
      },
    );
    const versionPayload = await versionResponse.json();
    await request(
      `/api/v1/tag-schemas/${createdPayload.id}/versions/${versionPayload.id}/publish`,
      { method: "POST", body: "{}" },
    );

    const listed = await request("/api/v1/tag-schemas");
    const listPayload = await listed.json();
    expect(listPayload.items).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          id: createdPayload.id,
          key: "automotive-reception",
        }),
      ]),
    );

    const audits = await request("/api/v1/tag-audit-events");
    expect(await audits.json()).toEqual(
      expect.objectContaining({
        items: expect.arrayContaining([
          expect.objectContaining({
            action: "tag-schema.created",
            resource_type: "tag-schemas",
          }),
        ]),
      }),
    );

    const automation = await request("/api/v1/receptions/101/automation/run", {
      method: "POST",
      body: "{}",
    });
    expect(await automation.json()).toMatchObject({
      target_labels: ["customer_temperature"],
    });
  });

  it("keeps public tag jobs on the server-routed extract contract", async () => {
    const accepted = await request("/api/v1/tag-jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_type: "recompute",
        scope: { reception_ids: [101], label_keys: ["intent"] },
      }),
    });
    expect(accepted.status).toBe(202);
    expect(await accepted.json()).toMatchObject({
      job_type: "recompute",
      tagger_version_id: null,
      origin: "manual",
    });

    for (const body of [
      {
        job_type: "optimize",
        scope: { reception_ids: [101] },
      },
      {
        job_type: "recompute",
        scope: { reception_ids: [101] },
        tagger_version_id: 21,
      },
      {
        job_type: "recompute",
        scope: { reception_ids: [101], deployment_id: 5 },
      },
    ]) {
      const rejected = await request("/api/v1/tag-jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      expect(rejected.status).toBe(422);
    }
  });

  it("persists an immutable review decision and resulting fact", async () => {
    const claimed = await request("/api/v1/tag-reviews/41/claim", {
      method: "POST",
      body: "{}",
    });
    expect(await claimed.json()).toMatchObject({ status: "claimed" });

    const decided = await request("/api/v1/tag-reviews/41/decide", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "correct",
        corrected_value: "价格异议",
        reason_code: "evidence-confirmed",
        evidence_refs: [],
      }),
    });
    expect(await decided.json()).toMatchObject({
      task: { status: "resolved" },
      decision: {
        task_id: 41,
        action: "correct",
        corrected_value: "价格异议",
        resulting_fact_id: expect.any(Number),
      },
      fact: {
        source: "manual",
        tag_key: "objection",
        tag_value: "价格异议",
      },
    });
  });

  it("derives review tier, round and adjudication from the task and endpoint", async () => {
    const legacy = await request("/api/v1/tag-reviews/41/decide", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "accept",
        reason_code: "legacy-client",
        evidence_refs: [],
        truth_tier: "t3",
        annotator_round: 3,
        adjudication: true,
      }),
    });
    expect(legacy.status).toBe(422);

    const adjudicated = await request("/api/v1/tag-reviews/41/adjudicate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "accept",
        reason_code: "server-derived-adjudication",
        evidence_refs: [],
      }),
    });
    expect(adjudicated.status).toBe(200);
    expect(await adjudicated.json()).toMatchObject({
      task: { status: "resolved" },
      decision: {
        adjudication: true,
        truth_tier: "t3",
        annotator_round: 3,
      },
    });
  });

  it("projects a governed correction into workspace, state, insights and lineage", async () => {
    const batch = await request("/api/v1/tag-reviews/create-batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        reason: "critical",
        subjects: [
          {
            subject_type: "dialogue_unit",
            subject_id: 1001,
            reception_id: 101,
            tag_key: "stage",
            proposed_value: "初次接触",
            schema_version_id: 11,
            tagger_version_id: 21,
            evidence_refs: [
              {
                ref_id: "stage-0",
                kind: "text",
                recording_id: 5001,
                start_sec: 0,
                end_sec: 8,
                text_excerpt: "您好，欢迎光临。",
              },
            ],
          },
        ],
      }),
    });
    const batchPayload = await batch.json();
    const taskId = batchPayload.items[0].id;

    const decided = await request(`/api/v1/tag-reviews/${taskId}/decide`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "correct",
        corrected_value: "复核迎宾",
        reason_code: "manual_workspace_correction",
        note: "复听原音后人工确认",
        evidence_refs: [
          {
            ref_id: "stage-0",
            kind: "text",
            recording_id: 5001,
            start_sec: 0,
            end_sec: 8,
            text_excerpt: "您好，欢迎光临。",
          },
        ],
      }),
    });
    const decisionPayload = await decided.json();
    const factId = decisionPayload.fact.id;

    expect(decided.status).toBe(200);
    expect(decisionPayload).toMatchObject({
      task: {
        reception_id: 101,
        schema_version_id: 11,
        tagger_version_id: 21,
      },
      fact: {
        id: expect.any(Number),
        reception_id: 101,
        subject_id: 1001,
        source: "manual",
        tag_key: "stage",
        tag_value: "复核迎宾",
        schema_version_id: 11,
        tagger_version_id: 21,
      },
    });

    const workspace = await request("/api/v1/receptions/101/workspace");
    const workspacePayload = await workspace.json();
    expect(workspacePayload.reception.version).toBe(5);
    expect(workspacePayload.dialogue_units[0]).toMatchObject({
      business_stage: "复核迎宾",
      edit_status: "manual_edited",
      version: 2,
    });
    expect(workspacePayload.state_transitions[0]).toMatchObject({
      id: 9001,
      from_state: "复核迎宾",
      to_state: "需求发现",
      trigger: "开放式提问",
    });
    expect(workspacePayload.state_transitions).toHaveLength(7);
    expect(workspacePayload.tag_assignments).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          id: factId,
          dialogue_unit_id: 1001,
          label_key: "stage",
          label_value: "复核迎宾",
          source: "manual",
          is_current: true,
          model_run_id: `fact:${factId}`,
        }),
      ]),
    );

    const insights = await request("/api/v1/reception-tag-insights");
    const insightsPayload = await insights.json();
    expect(insightsPayload.evidence_summary[0]).toMatchObject({
      reception_id: 101,
      dialogue_unit_id: 1001,
      label_key: "stage",
      label_value: "复核迎宾",
    });

    const lineage = await request(`/api/v1/tag-facts/${factId}/lineage`);
    expect(lineage.status).toBe(200);
    expect(await lineage.json()).toMatchObject({
      fact: { id: factId, tag_value: "复核迎宾", source: "manual" },
      is_current: true,
      schema_version: { id: 11, status: "published" },
      tagger_version: { id: 21, status: "qualified" },
      model_version: "qwen3.6-27b",
    });

    const nextBatch = await request("/api/v1/tag-reviews/create-batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        reason: "critical",
        subjects: [
          {
            subject_type: "dialogue_unit",
            subject_id: 1001,
            reception_id: 101,
            tag_key: "stage",
            proposed_value: "复核迎宾",
            schema_version_id: 11,
          },
        ],
      }),
    });
    expect(await nextBatch.json()).toMatchObject({
      items: [{ proposed_fact_id: factId, cas_bound: true }],
    });
  });

  it("rejects a governed bootstrap review when a canonical fact appears before decide", async () => {
    const batch = await request("/api/v1/tag-reviews/create-batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        reason: "critical",
        subjects: [
          {
            subject_type: "dialogue_unit",
            subject_id: 1001,
            reception_id: 101,
            tag_key: "stage",
            proposed_value: "初次接触",
            schema_version_id: 11,
            evidence_refs: [],
          },
        ],
      }),
    });
    const task = (await batch.json()).items[0];
    expect(task).toMatchObject({
      proposed_fact_id: null,
      cas_bound: true,
      status: "pending",
    });

    demoDb.records.set(
      "tag-assignment-currents:dialogue_unit:1001:stage",
      JSON.stringify({
        id: "dialogue_unit:1001:stage",
        fact_id: 9901,
        revision: 1,
      }),
    );

    const decided = await request(`/api/v1/tag-reviews/${task.id}/decide`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "correct",
        corrected_value: "复核迎宾",
        reason_code: "manual_workspace_correction",
        evidence_refs: [],
      }),
    });
    expect(decided.status).toBe(409);
    expect(await decided.json()).toMatchObject({
      error: { code: "TAG_REVIEW_VERSION_CONFLICT" },
    });
    expect(
      JSON.parse(demoDb.records.get(`tag-reviews:${task.id}`) ?? "{}"),
    ).toMatchObject({ status: "pending" });
  });

  it("serves a successful-job-bound topic cluster snapshot", async () => {
    const response = await request(
      "/api/v1/graph/topic-clusters?job_id=240724&level=0",
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({
      job: { id: 240724, status: "succeeded" },
      level: 0,
      total_clusters: 6,
    });
  });

  it("queues evaluations through a persisted idempotent tag job", async () => {
    const init = {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": "evaluation-demo-key",
      },
      body: JSON.stringify({
        tagger_version_id: 22,
        gold_set_version_id: 51,
        baseline_tagger_version_id: 21,
      }),
    };
    const first = await request("/api/v1/tag-evaluations", init);
    expect(first.status).toBe(202);
    const firstPayload = await first.json();
    expect(firstPayload).toMatchObject({
      job_id: expect.any(Number),
      evaluation: {
        status: "queued",
        tagger_version_id: 22,
        baseline_tagger_version_id: 21,
        gold_set_version_id: 51,
        metrics: {},
      },
    });

    const replay = await request("/api/v1/tag-evaluations", init);
    expect(replay.status).toBe(202);
    expect(await replay.json()).toEqual(firstPayload);

    const conflict = await request("/api/v1/tag-evaluations", {
      ...init,
      body: JSON.stringify({
        tagger_version_id: 22,
        gold_set_version_id: 51,
        baseline_tagger_version_id: 20,
      }),
    });
    expect(conflict.status).toBe(409);
    expect(await conflict.json()).toMatchObject({
      error: { code: "IDEMPOTENCY_KEY_CONFLICT" },
    });

    const missingBaseline = await request("/api/v1/tag-evaluations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tagger_version_id: 22,
        gold_set_version_id: 51,
      }),
    });
    expect(missingBaseline.status).toBe(422);
  });

  it("freezes only a server-resolved review cohort with a complete manifest", async () => {
    const created = await request("/api/v1/tag-gold-sets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        key: "release-holdout",
        name: "发布金标",
        schema_version_id: 11,
      }),
    });
    const goldSet = await created.json();

    const legacy = await request(
      `/api/v1/tag-gold-sets/${goldSet.id}/freeze`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          version: "legacy",
          decision_ids: [101, 102],
        }),
      },
    );
    expect(legacy.status).toBe(422);

    const frozen = await request(
      `/api/v1/tag-gold-sets/${goldSet.id}/freeze`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          version: "2026.07",
          cohort: {
            review_bundle_ids: ["release-2026-07"],
            truth_tiers: ["t2", "t3"],
            subject_types: ["dialogue_unit", "reception"],
          },
          completeness_checklist: {
            full_applicable_matrix: true,
            frozen_input_snapshots: true,
            reception_level_isolation: true,
            t2_t3_truth_only: true,
          },
        }),
      },
    );
    expect(frozen.status).toBe(201);
    expect(await frozen.json()).toMatchObject({
      status: "frozen",
      version: "2026.07",
      completeness_manifest: {
        complete: true,
        review_bundle_ids: ["release-2026-07"],
      },
    });
  });

  it("keeps demo deployments revision-safe and labels observations as demo data", async () => {
    const listed = await request("/api/v1/tag-deployments");
    expect(await listed.json()).toMatchObject({
      items: [
        expect.objectContaining({
          id: 71,
          revision: 3,
          baseline_tagger_version_id: 20,
        }),
      ],
    });

    const observations = await request(
      "/api/v1/tag-deployments/71/observations",
    );
    expect(observations.status).toBe(200);
    expect(await observations.json()).toEqual(
      expect.objectContaining({
        items: expect.arrayContaining([
          expect.objectContaining({
            deployment_id: 71,
            sample_count: expect.any(Number),
            metrics: expect.objectContaining({
              error_rate: expect.any(Number),
            }),
            is_demo: true,
            data_source: "demo",
          }),
        ]),
      }),
    );

    const missingBaseline = await request("/api/v1/tag-deployments", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tagger_version_id: 21,
        evaluation_run_id: 61,
      }),
    });
    expect(missingBaseline.status).toBe(422);

    const created = await request("/api/v1/tag-deployments", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tagger_version_id: 21,
        evaluation_run_id: 61,
        baseline_tagger_version_id: 20,
      }),
    });
    expect(created.status).toBe(201);
    const deployment = await created.json();
    expect(deployment).toMatchObject({
      revision: 1,
      status: "shadow",
      baseline_tagger_version_id: 20,
      promotion_paused: false,
    });

    const attemptedOverride = await request("/api/v1/tag-deployments", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        tagger_version_id: 21,
        evaluation_run_id: 61,
        baseline_tagger_version_id: 20,
        override_reason: "绕过样本门禁",
      }),
    });
    expect(attemptedOverride.status).toBe(422);

    const manualPromotion = await request(
      `/api/v1/tag-deployments/${deployment.id}/promote`,
      {
        method: "POST",
        headers: { "If-Match": "1" },
        body: "{}",
      },
    );
    expect(manualPromotion.status).toBe(403);
    expect(await manualPromotion.json()).toMatchObject({
      error: {
        code: "TAG_DEPLOYMENT_PROMOTION_MONITOR_ONLY",
      },
    });
  });

  it("derives optimization samples server-side and never reads hidden test data", async () => {
    const optimized = await request("/api/v1/tagger-versions/optimize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        gold_set_version_id: 51,
        production_tagger_version_id: 21,
      }),
    });
    expect(optimized.status).toBe(201);
    expect(await optimized.json()).toMatchObject({
      candidate: { status: "draft", schema_version_id: 11 },
      optimization: {
        source_tagger_version_id: 21,
        gold_set_version_id: 51,
        derived_sample_count: 386,
        holdout_read: false,
        data_source: "demo",
      },
    });

    const injectedSamples = await request("/api/v1/tagger-versions/optimize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        gold_set_version_id: 51,
        production_tagger_version_id: 21,
        error_samples: [{ gold_label_id: 1, score: 1 }],
      }),
    });
    expect(injectedSamples.status).toBe(422);
  });

  it("serves the self-evolution overview, badcases and durable demo runs", async () => {
    const overview = await request("/api/v1/tag-evolution/overview");
    expect(overview.status).toBe(200);
    expect(await overview.json()).toMatchObject({
      production_harness: { id: 21 },
      recommended_gold_set_version_id: 51,
      feedback: { next_run_eligible: true },
      release: { stage: "canary_25" },
      data_source: "demo",
    });

    const badcases = await request(
      "/api/v1/tag-badcases?failure_stage=tag_reasoning",
    );
    expect(badcases.status).toBe(200);
    expect(await badcases.json()).toMatchObject({
      total: 1,
      items: [{ tag_key: "intent", failure_stage: "tag_reasoning" }],
    });

    const seededRuns = await request("/api/v1/tag-optimization-runs");
    expect(seededRuns.status).toBe(200);
    expect(await seededRuns.json()).toMatchObject({
      items: [{ status: "completed", data_source: "demo" }],
    });

    const created = await request("/api/v1/tag-optimization-runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        cohort: {
          source: "tag_insights",
          filters: { scenarios: ["automotive"] },
          conflict_only: true,
        },
        target_policy: { policy: "quality_first" },
        search_budget: { max_trials: 16, sealed_holdout_queries: 1 },
      }),
    });
    expect(created.status).toBe(202);
    const createdRun = await created.json();
    expect(createdRun).toMatchObject({
      status: "completed",
      phase: "completed",
      baseline_tagger_version_id: 21,
      gold_set_version_id: 51,
      data_source: "demo",
      is_demo: true,
    });
    expect(createdRun.job_id).toEqual(expect.any(Number));
    expect(createdRun.winner_tagger_version_id).toEqual(expect.any(Number));

    const attemptedGoldOverride = await request(
      "/api/v1/tag-optimization-runs",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          gold_set_version_id: 999,
          cohort: { source: "tag_insights" },
          target_policy: { policy: "quality_first" },
          search_budget: { max_trials: 8, sealed_holdout_queries: 1 },
        }),
      },
    );
    expect(attemptedGoldOverride.status).toBe(422);

    const legacyObjective = await request("/api/v1/tag-optimization-runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        cohort: { source: "tag_insights" },
        objective: { policy: "quality_first" },
        search_budget: { max_trials: 8, sealed_holdout_queries: 1 },
        trigger: "insight",
      }),
    });
    expect(legacyObjective.status).toBe(422);

    const detail = await request(
      `/api/v1/tag-optimization-runs/${createdRun.id}`,
    );
    expect(detail.status).toBe(200);
    expect(await detail.json()).toMatchObject({
      id: createdRun.id,
      status: "completed",
      trials: expect.any(Array),
    });
  });
});

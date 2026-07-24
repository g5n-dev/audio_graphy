import {
  demoExplore,
  demoRecordings,
  demoReceptions,
  demoStateInsights,
  demoStats,
  demoTagAnalysis,
  demoTagInsights,
  demoUser,
  demoWorkspace,
} from "./demoData";

interface AssetsBinding {
  fetch(request: Request): Promise<Response>;
}

interface Env {
  /**
   * Sites serves static assets ahead of the API worker in production. The
   * binding is optional in Vite's local Workers runtime, where returning 404
   * lets the client environment continue with its SPA fallback.
   */
  ASSETS?: AssetsBinding;
}

const apiPrefix = "/api/v1";
const jsonHeaders = {
  "Content-Type": "application/json; charset=utf-8",
  "Cache-Control": "no-store",
};

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: jsonHeaders,
  });
}

function apiError(status: number, code: string, message: string): Response {
  return json({ error: { code, message, detail: {} } }, status);
}

function workspaceForReception(receptionId: number): typeof demoWorkspace {
  if (receptionId === demoWorkspace.reception.id) return demoWorkspace;
  return {
    ...demoWorkspace,
    reception: {
      ...demoWorkspace.reception,
      id: receptionId,
      external_session_id: `demo-reception-${receptionId}`,
    },
  };
}

function automationForReception(receptionId: number) {
  return {
    id: 9000 + receptionId,
    reception_id: receptionId,
    status: "ready",
    stage: "ready",
    attempt_count: 1,
    checkpoints: {
      merge: "complete",
      segmentation: "complete",
      tagging: "complete",
    },
    segmentation_algorithm: "dialogue-hybrid-v1",
    tag_group_key: "reception-rules",
    tag_group_version: "rules-v1",
    target_labels: [
      "stage",
      "intent",
      "objection",
      "need",
      "action",
    ],
    tag_priority: 0,
    last_error_code: null,
    last_error_message: null,
    created_at: "2026-07-24T01:08:00.000Z",
    updated_at: "2026-07-24T09:00:00.000Z",
    finished_at: "2026-07-24T09:00:00.000Z",
  };
}

function demoReceptionResponse(receptionId = demoWorkspace.reception.id) {
  const workspace = workspaceForReception(receptionId);
  return {
    ...workspace.reception,
    recordings: workspace.recordings,
  };
}

function demoDialogueEdit(receptionId: number) {
  const workspace = workspaceForReception(receptionId);
  return {
    reception_id: receptionId,
    reception_version: workspace.reception.version,
    dialogue_units: workspace.dialogue_units,
  };
}

function demoDiscovery(storeId: string) {
  return {
    items: [
      {
        candidate_type: "merge_group",
        recording_ids: demoWorkspace.recordings
          .slice(0, 2)
          .map((recording) => recording.recording_id),
        decision: "merge",
        confidence: 0.94,
        reasons: [
          {
            code: "temporal_continuity",
            contribution: 0.38,
            detail: "两段录音时间连续，间隔 1.2 秒",
            hard_constraint: false,
          },
          {
            code: "speaker_continuity",
            contribution: 0.32,
            detail: "销售与客户声纹连续",
            hard_constraint: false,
          },
          {
            code: "same_store",
            contribution: 0.24,
            detail: "录音来自同一门店",
            hard_constraint: true,
          },
        ],
        store_id: storeId || demoWorkspace.reception.store_id,
        started_at: demoWorkspace.reception.started_at,
        ended_at: demoWorkspace.reception.ended_at,
        duration_status: "available",
        split_at_sec: null,
        at_segment_id: null,
        proposal_token: null,
        proposal_expires_at: null,
      },
    ],
    total: 1,
    scanned_recordings: demoWorkspace.recordings.length,
    truncated: false,
  };
}

function safeGet(pathname: string): Response | null {
  if (pathname === "/recordings") return json(demoRecordings);
  if (pathname === "/receptions") return json(demoReceptions);
  if (pathname === "/graph/explore" || pathname === "/graph/subgraph") {
    return json(demoExplore);
  }
  if (pathname === "/tags/stats") return json(demoStats);
  if (pathname === "/prompts") return json({ items: [] });
  if (pathname === "/speakers") return json({ items: [], total: 0 });
  if (/^\/recordings\/\d+\/segments$/.test(pathname)) {
    return json({
      recording_id: Number(pathname.split("/")[2]),
      items: [],
      total: 0,
      page: 1,
      page_size: 20,
    });
  }
  if (/^\/recordings\/\d+\/tags$/.test(pathname)) {
    return json({
      recording_id: Number(pathname.split("/")[2]),
      view: "current",
      tags: [],
    });
  }
  if (/^\/provenance\/reception\/\d+$/.test(pathname)) {
    return json({
      object_type: "reception",
      object_ref: pathname.split("/").at(-1),
      items: demoWorkspace.provenance_events,
      total: demoWorkspace.provenance_events.length,
      page: 1,
      page_size: 100,
      truncated: false,
    });
  }
  return null;
}

export default {
  async fetch(request: Request, env?: Env): Promise<Response> {
    const url = new URL(request.url);
    if (!url.pathname.startsWith(`${apiPrefix}/`)) {
      return env?.ASSETS?.fetch(request) ?? new Response(null, { status: 404 });
    }

    const pathname = url.pathname.slice(apiPrefix.length);
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204 });
    }

    if (request.method === "POST" && pathname === "/auth/login") {
      return json({
        access_token: "demo-access-token",
        refresh_token: "demo-refresh-token",
        token_type: "bearer",
        expires_in: 86_400,
        user: demoUser,
      });
    }
    if (request.method === "GET" && pathname === "/auth/me") {
      return json(demoUser);
    }
    if (
      request.method === "GET" &&
      pathname === "/reception-state-insights"
    ) {
      return json(demoStateInsights);
    }
    if (
      request.method === "GET" &&
      pathname === "/reception-tag-insights"
    ) {
      return json(demoTagInsights);
    }
    if (
      request.method === "POST" &&
      pathname === "/tag-insights/analyze"
    ) {
      return json(demoTagAnalysis);
    }

    if (
      request.method === "POST" &&
      pathname === "/receptions/proposals/discover"
    ) {
      const body = (await request.json().catch(() => ({}))) as {
        store_id?: unknown;
      };
      return json(
        demoDiscovery(
          typeof body.store_id === "string" ? body.store_id.trim() : "",
        ),
      );
    }
    if (
      request.method === "POST" &&
      pathname === "/receptions/proposals/accept"
    ) {
      return json(demoReceptionResponse());
    }

    const workspaceMatch = /^\/receptions\/(\d+)\/workspace$/.exec(pathname);
    if (request.method === "GET" && workspaceMatch) {
      return json(workspaceForReception(Number(workspaceMatch[1])));
    }

    const automationMatch = /^\/receptions\/(\d+)\/automation(?:\/run)?$/.exec(
      pathname,
    );
    if (
      automationMatch &&
      (request.method === "GET" || request.method === "POST")
    ) {
      return json(automationForReception(Number(automationMatch[1])));
    }

    const mergeMatch = /^\/receptions\/(\d+)\/merge$/.exec(pathname);
    if (request.method === "POST" && mergeMatch) {
      return json(demoReceptionResponse(Number(mergeMatch[1])));
    }

    const segmentMatch = /^\/receptions\/(\d+)\/segment$/.exec(pathname);
    if (request.method === "POST" && segmentMatch) {
      return json(demoDialogueEdit(Number(segmentMatch[1])));
    }

    const unitEditMatch =
      /^\/receptions\/(\d+)\/dialogue-units\/[^/]+\/(?:split|merge)$/.exec(
        pathname,
      );
    if (request.method === "POST" && unitEditMatch) {
      return json(demoDialogueEdit(Number(unitEditMatch[1])));
    }

    const deriveMatch =
      /^\/receptions\/(\d+)\/dialogue-tags\/derive$/.exec(pathname);
    if (request.method === "POST" && deriveMatch) {
      const receptionId = Number(deriveMatch[1]);
      return json({
        reception_id: receptionId,
        group_key: "reception-rules",
        group_version: "rules-v1",
        requested_labels: [
          "stage",
          "intent",
          "objection",
          "need",
          "action",
        ],
        assignment_count: demoWorkspace.tag_assignments.length,
        superseded_count: 0,
        no_op: true,
        assignments: demoWorkspace.tag_assignments,
        missing: [],
      });
    }

    if (request.method === "GET") {
      const response = safeGet(pathname);
      if (response) return response;
    }

    return apiError(404, "DEMO_ROUTE_NOT_FOUND", "演示站未提供该接口。");
  },
};

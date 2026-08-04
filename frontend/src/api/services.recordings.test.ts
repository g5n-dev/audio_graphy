/**
 * Recording ingest chain services — createRecording / status / processing
 * run / reindex. These endpoints previously had no frontend callers at all,
 * so each test pins the exact URL + payload the backend contract expects.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  createRecording,
  getRecordingProcessingRun,
  getRecordingStatus,
  reindexRecording,
} from "./services";
import type {
  PipelineRunResponse,
  RecordingStatusResponse,
  ReindexResponse,
} from "@/types/api";

vi.mock("./client", () => ({
  httpClient: {
    get: vi.fn(),
    post: vi.fn(),
  },
}));

import { httpClient } from "./client";

const mockedGet = httpClient.get as unknown as ReturnType<typeof vi.fn>;
const mockedPost = httpClient.post as unknown as ReturnType<typeof vi.fn>;

beforeEach(() => {
  mockedGet.mockReset();
  mockedPost.mockReset();
});

describe("createRecording", () => {
  it("POSTs the server-side path registration to /recordings", async () => {
    const created = { id: 42, status: "queued" };
    mockedPost.mockResolvedValue({ data: created });

    const result = await createRecording({
      store_id: "store-1",
      path: "mock_data/demo.wav",
      recorded_at: "2026-07-31T10:00:00",
    });

    expect(mockedPost).toHaveBeenCalledWith("/recordings", {
      store_id: "store-1",
      path: "mock_data/demo.wav",
      recorded_at: "2026-07-31T10:00:00",
    });
    expect(result).toEqual(created);
  });
});

describe("getRecordingStatus", () => {
  it("GETs the lightweight status endpoint", async () => {
    const status: RecordingStatusResponse = {
      id: 7,
      agent_user_id: null,
      status: "processing",
      pipeline_state: "asr",
      indexed_at: null,
      // In flight: the active pointer is stale-by-design, the latest one is
      // what identifies the run actually being watched.
      latest_pipeline_run_id: 21,
      active_pipeline_run_id: 12,
    };
    mockedGet.mockResolvedValue({ data: status });

    const result = await getRecordingStatus(7);

    expect(mockedGet).toHaveBeenCalledWith("/recordings/7/status");
    expect(result).toEqual(status);
  });
});

describe("getRecordingProcessingRun", () => {
  it("GETs the durable pipeline run by recording + run id", async () => {
    const run: PipelineRunResponse = {
      id: 12,
      recording_id: 7,
      generation: 2,
      state: "failed_retryable",
      attempt_count: 3,
      required_projections: ["file_index", "vector", "graph"],
      completed_projections: ["file_index"],
      error_code: "ASR_TIMEOUT",
      error_message: "ASR provider timed out",
      lease_expires_at: null,
      started_at: "2026-07-31T10:00:00Z",
      finished_at: "2026-07-31T10:05:00Z",
      activated_at: null,
    };
    mockedGet.mockResolvedValue({ data: run });

    const result = await getRecordingProcessingRun(7, 12);

    expect(mockedGet).toHaveBeenCalledWith("/recordings/7/processing-runs/12");
    expect(result).toEqual(run);
  });
});

describe("reindexRecording", () => {
  const RESPONSE: ReindexResponse = {
    id: 7,
    status: "queued",
    pipeline_state: "queued",
    operation_id: 13,
    generation: 3,
    operation_state: "queued",
    message: "Reindex triggered",
  };

  it("defaults to a non-forced reindex", async () => {
    mockedPost.mockResolvedValue({ data: RESPONSE });

    const result = await reindexRecording(7);

    expect(mockedPost).toHaveBeenCalledWith("/recordings/7/reindex", {
      force: false,
    });
    expect(result).toEqual(RESPONSE);
  });

  it("passes force=true for stuck-processing recoveries", async () => {
    mockedPost.mockResolvedValue({ data: RESPONSE });

    await reindexRecording(7, { force: true });

    expect(mockedPost).toHaveBeenCalledWith("/recordings/7/reindex", {
      force: true,
    });
  });
});

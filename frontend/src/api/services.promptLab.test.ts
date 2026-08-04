import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./client", () => ({
  httpClient: {
    get: vi.fn(),
    post: vi.fn(),
  },
}));

import { httpClient } from "./client";
import {
  createPromptCompilation,
  decidePromptPatches,
  getPromptArtifact,
  getPromptArtifactDiff,
  getPromptLabReadiness,
  listPromptArtifacts,
  listPromptGradients,
  promotePromptArtifact,
} from "./services";

const mockedGet = httpClient.get as unknown as ReturnType<typeof vi.fn>;
const mockedPost = httpClient.post as unknown as ReturnType<typeof vi.fn>;

beforeEach(() => {
  mockedGet.mockReset().mockResolvedValue({ data: { items: [], total: 0 } });
  mockedPost.mockReset().mockResolvedValue({ data: {} });
});

describe("prompt lab services", () => {
  it("reads every resource from the prompt-lab prefix", async () => {
    await getPromptLabReadiness();
    await listPromptArtifacts();
    await getPromptArtifact(42);
    await getPromptArtifactDiff(42);

    expect(mockedGet.mock.calls.map((call) => call[0])).toEqual([
      "/prompt-lab/readiness",
      "/prompt-lab/artifacts",
      "/prompt-lab/artifacts/42",
      "/prompt-lab/artifacts/42/diff",
    ]);
  });

  it("passes the artifact list filters as query parameters", async () => {
    await listPromptArtifacts({ status: "draft", limit: 20 });

    expect(mockedGet).toHaveBeenCalledWith("/prompt-lab/artifacts", {
      params: { status: "draft", limit: 20 },
    });
  });

  it("always sends artifact_id when listing gradients", async () => {
    await listPromptGradients({ artifact_id: 7, decision: "accepted" });

    expect(mockedGet).toHaveBeenCalledWith("/prompt-lab/gradients", {
      params: { artifact_id: 7, decision: "accepted" },
    });
  });

  it("posts a compilation request as the body", async () => {
    const body = {
      baseline_tagger_version_id: 3,
      compiler: {
        compiler: "builtin" as const,
        max_patches: 8,
        min_cluster_support: 3,
        instruction_candidates: 4,
        textgrad_iterations: 2,
        demo_count: 0 as const,
        redaction_mode: "synthetic" as const,
        max_prompt_tokens: 3072,
        efficiency_policy: "quality_uplift_v1" as const,
        seed: 0,
      },
      budget: {
        max_provider_calls: 120,
        max_provider_tokens: 1_500_000,
        max_cost_microunits: 2_000_000,
        max_wall_seconds: 1_800,
      },
    };

    await createPromptCompilation(body);

    expect(mockedPost).toHaveBeenCalledWith("/prompt-lab/compilations", body);
  });

  it("puts the artifact id on the path and the decisions in the body", async () => {
    const body = {
      decisions: [{ patch_id: "a".repeat(32), decision: "rejected" as const }],
      dropped_demo_ids: ["d".repeat(32)],
    };

    await decidePromptPatches(9, body);

    expect(mockedPost).toHaveBeenCalledWith(
      "/prompt-lab/artifacts/9/decisions",
      body,
    );
  });

  it("promotes an artifact through its own sub-resource", async () => {
    const body = {
      version_suffix: "r1",
      change_summary: "采纳两条聚类补丁后的候选提示词",
    };

    await promotePromptArtifact(9, body);

    expect(mockedPost).toHaveBeenCalledWith(
      "/prompt-lab/artifacts/9/promote",
      body,
    );
  });
});

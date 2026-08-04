/**
 * 演示种子的不变量。
 *
 * 前端按服务端 render() 的规则逆向切块来做补丁归属。如果种子的 rendered_prompt
 * 不是「header + 已采纳补丁（按 ordinal, patch_id 排序）+ 示例：+ 各示例」用
 * \n\n 拼接的结果，归属重建就会返回 exact:false——演示站与 e2e 都看不到补丁标识，
 * 而且是静默失效。所以拼接规则要在这里被钉住。
 */

import { describe, expect, it } from "vitest";

import { buildPromptBlockMap } from "@/components/governance/textDiff";

import worker from "./index";

interface SeedPatch {
  patch_id: string;
  ordinal: number;
  body: string;
}
interface SeedDemo {
  demo_id: string;
  rendered_text: string;
}
interface SeedArtifact {
  id: number;
  header: string;
  rendered_prompt: string;
  patches: SeedPatch[];
  demos: SeedDemo[];
  accepted_patch_ids: string[];
}

async function seededArtifacts(): Promise<SeedArtifact[]> {
  // env 不带 DB：读操作只依赖种子，正是我们要验证的东西。
  const response = await worker.fetch(
    new Request("https://demo.test/api/v1/prompt-lab/artifacts"),
    undefined as never,
  );
  const listed = (await response.json()) as { items: { id: number }[] };
  const artifacts: SeedArtifact[] = [];
  for (const item of listed.items) {
    const detail = await worker.fetch(
      new Request(`https://demo.test/api/v1/prompt-lab/artifacts/${item.id}`),
      undefined as never,
    );
    artifacts.push((await detail.json()) as SeedArtifact);
  }
  return artifacts;
}

describe("prompt lab demo seeds", () => {
  it("renders every seeded artifact so the block map can be rebuilt exactly", async () => {
    const artifacts = await seededArtifacts();
    expect(artifacts.length).toBeGreaterThan(0);

    for (const artifact of artifacts) {
      const map = buildPromptBlockMap({
        candidatePrompt: artifact.rendered_prompt,
        patches: artifact.patches,
        demos: artifact.demos,
        acceptedPatchIds: artifact.accepted_patch_ids,
      });
      expect(
        map.exact,
        `产物 ${artifact.id} 的 rendered_prompt 与块拼接规则不一致`,
      ).toBe(true);
      // 每条已采纳补丁都要能定位到，否则差异页上它没有标识。
      for (const patchId of artifact.accepted_patch_ids) {
        expect(map.blocks.some((block) => block.id === patchId)).toBe(true);
      }
    }
  });

  it("omits prompt bodies from the artifact list, matching the real API", async () => {
    const response = await worker.fetch(
      new Request("https://demo.test/api/v1/prompt-lab/artifacts"),
      undefined as never,
    );
    const payload = (await response.json()) as {
      items: Record<string, unknown>[];
    };

    expect(payload.items.length).toBeGreaterThan(0);
    for (const item of payload.items) {
      expect(item).not.toHaveProperty("rendered_prompt");
      expect(item).not.toHaveProperty("patches");
      expect(item.prompt_token_estimate).toBeTypeOf("number");
    }
  });

  it("keeps at least one domain below the threshold so the matrix shows every tone", async () => {
    const response = await worker.fetch(
      new Request("https://demo.test/api/v1/prompt-lab/readiness"),
      undefined as never,
    );
    const payload = (await response.json()) as {
      domains: { meets_threshold: boolean }[];
      blockers: string[];
    };

    expect(payload.domains.some((domain) => !domain.meets_threshold)).toBe(true);
    expect(payload.domains.some((domain) => domain.meets_threshold)).toBe(true);
    expect(payload.blockers.length).toBeGreaterThan(0);
  });

  it("requires artifact_id when listing gradients", async () => {
    const missing = await worker.fetch(
      new Request("https://demo.test/api/v1/prompt-lab/gradients"),
      undefined as never,
    );
    expect(missing.status).toBe(422);

    const scoped = await worker.fetch(
      new Request("https://demo.test/api/v1/prompt-lab/gradients?artifact_id=301"),
      undefined as never,
    );
    const payload = (await scoped.json()) as { items: { artifact_id: number }[] };
    expect(payload.items.length).toBeGreaterThan(0);
    expect(payload.items.every((item) => item.artifact_id === 301)).toBe(true);
  });

  it("rejects an out-of-range limit rather than silently clamping it", async () => {
    const response = await worker.fetch(
      new Request("https://demo.test/api/v1/prompt-lab/artifacts?limit=999"),
      undefined as never,
    );

    expect(response.status).toBe(422);
  });

  it("prices the diff against the baseline transport cost, not the bare policy", async () => {
    const response = await worker.fetch(
      new Request("https://demo.test/api/v1/prompt-lab/artifacts/301/diff"),
      undefined as never,
    );
    const payload = (await response.json()) as {
      prompt_token_estimate: number;
      fixed_token_delta: number;
      input_budget_report: {
        fixed_tokens: number;
        baseline_fixed_tokens: number;
        headroom_delta: number;
      };
    };
    const budget = payload.input_budget_report;

    expect(payload.fixed_token_delta).toBe(
      budget.fixed_tokens - budget.baseline_fixed_tokens,
    );
    // 同一件事的两种测量必须一致：多花 N 个固定 token 就是少了 N 个余量。
    expect(payload.fixed_token_delta).toBe(-budget.headroom_delta);
  });

  it("does not turn a more expensive candidate into a saving", async () => {
    // 302 的策略正文（208）比基线的固定开销（246）短，旧口径相减得到 -38，
    // 界面会把这个比基线贵 236 token 的候选涂成改进。
    const response = await worker.fetch(
      new Request("https://demo.test/api/v1/prompt-lab/artifacts/302/diff"),
      undefined as never,
    );
    const payload = (await response.json()) as {
      prompt_token_estimate: number;
      fixed_token_delta: number;
      input_budget_report: { baseline_fixed_tokens: number };
    };

    expect(payload.prompt_token_estimate).toBeLessThan(
      payload.input_budget_report.baseline_fixed_tokens,
    );
    expect(payload.fixed_token_delta).toBeGreaterThan(0);
  });

  it("returns 404 for an artifact the demo does not have", async () => {
    const response = await worker.fetch(
      new Request("https://demo.test/api/v1/prompt-lab/artifacts/999999"),
      undefined as never,
    );

    expect(response.status).toBe(404);
  });
});

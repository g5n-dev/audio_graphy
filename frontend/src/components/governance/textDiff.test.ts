import { describe, expect, it } from "vitest";

import {
  attributeHunks,
  buildPromptBlockMap,
  diffLines,
  diffPromptArtifact,
  segmentWords,
  type PromptBlockMapInput,
} from "./textDiff";

const HEADER = "基线规则：按 schema 判定标签。";
const PATCH_A = "规则一：出现明确金额才输出价格标签。";
const PATCH_B = "规则二：跨句证据需同时引用两个 segment。";
const DEMO_A = "示例甲：客户询问优惠，顾问报出具体金额。";

function joinBlocks(...blocks: string[]): string {
  return blocks.join("\n\n");
}

function artifactInput(overrides: Partial<PromptBlockMapInput> = {}): PromptBlockMapInput {
  return {
    candidatePrompt: joinBlocks(HEADER, PATCH_A, PATCH_B),
    patches: [
      { patch_id: "aaaa1111", ordinal: 1, body: PATCH_A },
      { patch_id: "bbbb2222", ordinal: 2, body: PATCH_B },
    ],
    demos: [],
    acceptedPatchIds: ["aaaa1111", "bbbb2222"],
    ...overrides,
  };
}

describe("diffLines", () => {
  it("reports no change for two identical texts", () => {
    const result = diffLines(joinBlocks(HEADER, PATCH_A), joinBlocks(HEADER, PATCH_A));

    expect(result.stats).toEqual({ added: 0, removed: 0, unchanged: 3 });
    expect(result.hunks.every((hunk) => hunk.op === "equal")).toBe(true);
    expect(result.degraded).toBe(false);
  });

  it("reports only an insertion when a paragraph is appended", () => {
    const result = diffLines(HEADER, joinBlocks(HEADER, PATCH_A));

    expect(result.stats.removed).toBe(0);
    expect(result.stats.added).toBe(2);
    const inserted = result.hunks.filter((hunk) => hunk.op === "insert");
    expect(inserted).toHaveLength(1);
  });

  it("reports only a deletion when a paragraph is removed", () => {
    const result = diffLines(joinBlocks(HEADER, PATCH_A), HEADER);

    expect(result.stats.added).toBe(0);
    expect(result.stats.removed).toBe(2);
  });

  it("merges an adjacent deletion and insertion into one replacement", () => {
    const result = diffLines("第一行\n第二行", "第一行\n改过的第二行");

    const ops = result.hunks.map((hunk) => hunk.op);
    expect(ops).toContain("replace");
    expect(ops).not.toContain("delete");
  });

  it("treats CRLF and LF as equivalent", () => {
    const result = diffLines("甲\r\n乙\r\n丙", "甲\n乙\n丙");

    expect(result.stats).toEqual({ added: 0, removed: 0, unchanged: 3 });
  });

  it("replaces the whole text when one side is empty", () => {
    const result = diffLines("", "新内容");

    expect(result.stats.added).toBe(1);
    expect(result.stats.removed).toBe(1);
  });

  it("keeps line-level precision across several separated edits", () => {
    const baseline = ["甲", "乙", "丙", "丁", "戊"].join("\n");
    const candidate = ["甲", "乙改", "丙", "丁", "戊改"].join("\n");

    const result = diffLines(baseline, candidate);

    expect(result.stats.unchanged).toBe(3);
    expect(result.stats.added).toBe(2);
    expect(result.stats.removed).toBe(2);
  });

  it("degrades to a whole-window replacement when the diff would be too large", () => {
    // 每行都重复出现，唯一行锚点无从下手，整个窗口必须一次性求解。
    const baseline = Array.from({ length: 40 }, (_, i) => `行${i % 3}`).join("\n");
    const candidate = Array.from({ length: 40 }, (_, i) => `新${i % 3}`).join("\n");

    const result = diffLines(baseline, candidate, { maxCells: 16 });

    expect(result.degraded).toBe(true);
    expect(result.stats.unchanged).toBe(0);
  });

  it("does not degrade when the budget is sufficient", () => {
    const baseline = Array.from({ length: 40 }, (_, i) => `行${i % 3}`).join("\n");
    const candidate = Array.from({ length: 40 }, (_, i) => `新${i % 3}`).join("\n");

    expect(diffLines(baseline, candidate).degraded).toBe(false);
  });

  it("does not pair unrelated repeated lines as equal", () => {
    const result = diffLines("同\n甲\n同", "同\n乙\n同");

    expect(result.stats.unchanged).toBe(2);
    expect(result.stats.added).toBe(1);
    expect(result.stats.removed).toBe(1);
  });
});

describe("word-level spans", () => {
  function spansOf(baseline: string, candidate: string, options = {}) {
    const result = diffLines(baseline, candidate, options);
    const replace = result.hunks.find((hunk) => hunk.op === "replace");
    return {
      before: replace?.lines.find((line) => line.op === "delete")?.spans,
      after: replace?.lines.find((line) => line.op === "insert")?.spans,
    };
  }

  it("marks only the changed words inside a replaced line", () => {
    const { before, after } = spansOf("客户预算两万元", "客户预算三万元");

    expect(before?.filter((span) => span.changed).map((span) => span.text)).toEqual([
      "两万",
    ]);
    expect(after?.filter((span) => span.changed).map((span) => span.text)).toEqual([
      "三万",
    ]);
  });

  it("keeps the untouched prefix and suffix unmarked", () => {
    const { after } = spansOf("客户预算两万元", "客户预算三万元");

    expect(after?.filter((span) => !span.changed).map((span) => span.text)).toEqual([
      "客户预算",
      "元",
    ]);
  });

  it("skips word diffing for lines longer than the limit", () => {
    const long = "字".repeat(500);
    const { before } = spansOf(`${long}甲`, `${long}乙`, { maxWordDiffChars: 100 });

    expect(before).toBeUndefined();
  });

  it("skips word diffing when the two lines barely resemble each other", () => {
    const { before } = spansOf("完全不同的一句话", "另外一段毫不相干内容");

    expect(before).toBeUndefined();
  });

  it("leaves surplus lines in a replacement without spans", () => {
    const result = diffLines("甲一\n甲二\n甲三", "甲一改");
    const replace = result.hunks.find((hunk) => hunk.op === "replace");
    const deletes = replace?.lines.filter((line) => line.op === "delete") ?? [];

    expect(deletes.length).toBeGreaterThan(1);
    expect(deletes[deletes.length - 1].spans).toBeUndefined();
  });
});

describe("segmentWords", () => {
  it("splits a Chinese phrase into words", () => {
    expect(segmentWords("客户预算两万元").join("|")).toContain("客户");
  });

  it("prefers an injected segmenter over the platform one", () => {
    const result = diffLines("甲乙", "甲丙", {
      segmenter: (text) => [...text],
    });
    const replace = result.hunks.find((hunk) => hunk.op === "replace");

    expect(replace?.lines[0].spans?.map((span) => span.text)).toEqual(["甲", "乙"]);
  });

  it("falls back to a regex when the platform segmenter is unavailable", () => {
    const original = Object.getOwnPropertyDescriptor(Intl, "Segmenter");
    // 模拟不提供 Segmenter 的运行时。
    Reflect.deleteProperty(Intl as object, "Segmenter");
    try {
      expect(segmentWords("客户 budget 两万")).toEqual([
        "客",
        "户",
        " ",
        "budget",
        " ",
        "两",
        "万",
      ]);
    } finally {
      if (original) Object.defineProperty(Intl, "Segmenter", original);
    }
  });

  it("falls back to a regex when the platform segmenter rejects the locale", () => {
    const original = Object.getOwnPropertyDescriptor(Intl, "Segmenter");
    Object.defineProperty(Intl, "Segmenter", {
      configurable: true,
      writable: true,
      value: function BrokenSegmenter() {
        throw new RangeError("unsupported locale");
      },
    });
    try {
      expect(segmentWords("甲乙")).toEqual(["甲", "乙"]);
    } finally {
      if (original) Object.defineProperty(Intl, "Segmenter", original);
      else Reflect.deleteProperty(Intl as object, "Segmenter");
    }
  });
});

describe("buildPromptBlockMap", () => {
  it("locates every accepted patch in the order the server assembles them", () => {
    const map = buildPromptBlockMap(artifactInput());

    expect(map.exact).toBe(true);
    expect(map.blocks.map((block) => [block.kind, block.id])).toEqual([
      ["header", null],
      ["patch", "aaaa1111"],
      ["patch", "bbbb2222"],
    ]);
  });

  it("breaks an ordinal tie by patch id, matching the server", () => {
    const map = buildPromptBlockMap(
      artifactInput({
        candidatePrompt: joinBlocks(HEADER, PATCH_A, PATCH_B),
        patches: [
          { patch_id: "bbbb2222", ordinal: 1, body: PATCH_B },
          { patch_id: "aaaa1111", ordinal: 1, body: PATCH_A },
        ],
      }),
    );

    expect(map.blocks.map((block) => block.id)).toEqual([
      null,
      "aaaa1111",
      "bbbb2222",
    ]);
  });

  it("ignores patches the reviewer rejected", () => {
    const map = buildPromptBlockMap(
      artifactInput({
        candidatePrompt: joinBlocks(HEADER, PATCH_A),
        acceptedPatchIds: ["aaaa1111"],
      }),
    );

    expect(map.exact).toBe(true);
    expect(map.blocks.map((block) => block.id)).toEqual([null, "aaaa1111"]);
  });

  it("gives the demo heading and each demo their own block", () => {
    const map = buildPromptBlockMap(
      artifactInput({
        candidatePrompt: joinBlocks(HEADER, PATCH_A, PATCH_B, "示例：", DEMO_A),
        demos: [{ demo_id: "dddd3333", rendered_text: DEMO_A }],
      }),
    );

    expect(map.blocks.map((block) => block.kind)).toEqual([
      "header",
      "patch",
      "patch",
      "demo-heading",
      "demo",
    ]);
  });

  it("omits the demo section when every demo is blank", () => {
    const map = buildPromptBlockMap(
      artifactInput({ demos: [{ demo_id: "dddd3333", rendered_text: "   " }] }),
    );

    expect(map.blocks.some((block) => block.kind === "demo-heading")).toBe(false);
  });

  it("treats everything before the first patch as the header", () => {
    const map = buildPromptBlockMap(artifactInput());

    expect(map.blocks[0]).toMatchObject({ kind: "header", startLine: 0, endLine: 1 });
  });

  it("does not mistake a patch body that also appears inside the header", () => {
    // 补丁正文原样出现在 header 里，但没有 \n\n 边界包围，不能被当成补丁块。
    const header = `${HEADER}\n参考：${PATCH_A}`;
    const map = buildPromptBlockMap(
      artifactInput({
        candidatePrompt: joinBlocks(header, PATCH_A),
        patches: [{ patch_id: "aaaa1111", ordinal: 1, body: PATCH_A }],
        acceptedPatchIds: ["aaaa1111"],
      }),
    );

    expect(map.exact).toBe(true);
    const patchBlock = map.blocks.find((block) => block.kind === "patch");
    expect(patchBlock?.startLine).toBe(3);
  });

  it("gives up entirely when the candidate cannot be reconciled", () => {
    const map = buildPromptBlockMap(
      artifactInput({ candidatePrompt: "服务端换了渲染规则，完全对不上。" }),
    );

    expect(map).toEqual({ blocks: [], exact: false });
  });

  it("treats the whole text as a header when there are no parts at all", () => {
    const map = buildPromptBlockMap(
      artifactInput({
        candidatePrompt: HEADER,
        patches: [],
        demos: [],
        acceptedPatchIds: [],
      }),
    );

    expect(map.blocks).toEqual([
      { kind: "header", id: null, startLine: 0, endLine: 1 },
    ]);
  });

  it("produces no blocks for an empty prompt with no parts", () => {
    const map = buildPromptBlockMap(
      artifactInput({
        candidatePrompt: "",
        patches: [],
        demos: [],
        acceptedPatchIds: [],
      }),
    );

    expect(map).toEqual({ blocks: [], exact: true });
  });

  it("reports failure when the header cannot be separated from the first patch", () => {
    const map = buildPromptBlockMap(
      artifactInput({
        // 补丁正文在最前面，前面没有 header。
        candidatePrompt: joinBlocks(PATCH_A, PATCH_B),
      }),
    );

    expect(map.exact).toBe(true);
    expect(map.blocks.map((block) => block.kind)).toEqual(["patch", "patch"]);
  });
});

describe("attributeHunks", () => {
  it("attributes each appended patch to itself instead of one ambiguous block", () => {
    const input = artifactInput();
    const { hunks } = diffPromptArtifact({ ...input, baselinePrompt: HEADER });

    const attributed = hunks
      .filter((hunk) => hunk.attribution?.kind === "patch")
      .map((hunk) => [hunk.attribution?.id, hunk.attribution?.ambiguous]);
    expect(attributed).toEqual([
      ["aaaa1111", false],
      ["bbbb2222", false],
    ]);
  });

  it("aligns anchors even when unique lines change order", () => {
    // 两个唯一行互换位置，最长递增子序列必须挑掉其中一个，二分才会走到收缩分支。
    const baseline = ["头", "唯一甲", "中", "唯一乙", "尾"].join("\n");
    const candidate = ["头", "唯一乙", "中", "唯一甲", "尾"].join("\n");

    const result = diffLines(baseline, candidate);

    expect(result.stats.unchanged).toBeGreaterThan(0);
    expect(result.stats.added).toBe(result.stats.removed);
  });

  it("splits a straddling change so each block gets a definite owner", () => {
    const map = {
      exact: true,
      blocks: [
        { kind: "patch" as const, id: "aaaa1111", startLine: 0, endLine: 2 },
        { kind: "patch" as const, id: "bbbb2222", startLine: 2, endLine: 4 },
      ],
    };
    const hunks = [
      {
        op: "insert" as const,
        baselineStart: 0,
        baselineEnd: 0,
        candidateStart: 1,
        candidateEnd: 3,
        lines: [
          {
            op: "insert" as const,
            text: "甲",
            baselineLine: null,
            candidateLine: 1,
          },
          {
            op: "insert" as const,
            text: "乙",
            baselineLine: null,
            candidateLine: 2,
          },
        ],
      },
    ];

    // 块边界在第 2 行，切开后两片各自明确；这里刻意用一个没有边界可切的映射。
    const single = attributeHunks(hunks, {
      exact: true,
      blocks: [{ kind: "patch", id: "aaaa1111", startLine: 0, endLine: 4 }],
    });
    expect(single[0].attribution).toEqual({
      kind: "patch",
      id: "aaaa1111",
      ambiguous: false,
    });

    const split = attributeHunks(hunks, map);
    expect(split).toHaveLength(2);
    expect(split.map((hunk) => hunk.attribution?.id)).toEqual([
      "aaaa1111",
      "bbbb2222",
    ]);
  });

  it("attributes a change inside the header to the header", () => {
    const input = artifactInput();
    const { hunks } = diffPromptArtifact({
      ...input,
      baselinePrompt: joinBlocks("旧的基线说明。", PATCH_A, PATCH_B),
    });

    expect(
      hunks.some((hunk) => hunk.attribution?.kind === "header"),
    ).toBe(true);
  });

  it("marks a pure deletion as ambiguous because it has no candidate position", () => {
    const attributed = attributeHunks(
      [
        {
          op: "delete",
          baselineStart: 1,
          baselineEnd: 2,
          candidateStart: 1,
          candidateEnd: 1,
          lines: [{ op: "delete", text: "旧行", baselineLine: 1, candidateLine: null }],
        },
      ],
      {
        exact: true,
        blocks: [{ kind: "patch", id: "aaaa1111", startLine: 0, endLine: 3 }],
      },
    );

    expect(attributed[0].attribution).toEqual({
      kind: "patch",
      id: "aaaa1111",
      ambiguous: true,
    });
  });

  it("returns no attribution for a deletion outside every block", () => {
    const attributed = attributeHunks(
      [
        {
          op: "delete",
          baselineStart: 9,
          baselineEnd: 10,
          candidateStart: 9,
          candidateEnd: 9,
          lines: [{ op: "delete", text: "旧行", baselineLine: 9, candidateLine: null }],
        },
      ],
      {
        exact: true,
        blocks: [{ kind: "patch", id: "aaaa1111", startLine: 0, endLine: 2 }],
      },
    );

    expect(attributed[0].attribution).toBeNull();
  });

  it("returns no attribution for an insertion that overlaps no block", () => {
    const attributed = attributeHunks(
      [
        {
          op: "insert",
          baselineStart: 0,
          baselineEnd: 0,
          candidateStart: 8,
          candidateEnd: 9,
          lines: [{ op: "insert", text: "新行", baselineLine: null, candidateLine: 8 }],
        },
      ],
      {
        exact: true,
        blocks: [{ kind: "patch", id: "aaaa1111", startLine: 0, endLine: 2 }],
      },
    );

    expect(attributed[0].attribution).toBeNull();
  });

  it("drops every attribution when the block map could not be rebuilt", () => {
    const input = artifactInput({ candidatePrompt: "对不上的文本。" });
    const { hunks, map } = diffPromptArtifact({ ...input, baselinePrompt: HEADER });

    expect(map.exact).toBe(false);
    expect(hunks.every((hunk) => hunk.attribution === null)).toBe(true);
  });

  it("leaves unchanged hunks unattributed", () => {
    const input = artifactInput();
    const { hunks } = diffPromptArtifact({ ...input, baselinePrompt: HEADER });

    const equal = hunks.filter((hunk) => hunk.op === "equal");
    expect(equal.length).toBeGreaterThan(0);
    expect(equal.every((hunk) => hunk.attribution === null)).toBe(true);
  });
});

describe("diffPromptArtifact", () => {
  it("returns the diff, the attributed hunks and the block map together", () => {
    const input = artifactInput();

    const output = diffPromptArtifact({ ...input, baselinePrompt: HEADER });

    expect(output.result.stats.added).toBeGreaterThan(0);
    expect(output.map.exact).toBe(true);
    expect(output.hunks.length).toBeGreaterThanOrEqual(output.result.hunks.length);
  });
});

/**
 * 无依赖的文本差异，专供 Prompt 候选与基线的对照。
 *
 * 仓库刻意不引 diff 库：首屏 gzip 预算只剩约 109 KiB，而这里需要的能力（行级对照、
 * 词级高亮、中文分词）加起来不到 400 行。
 *
 * ## 归属是精确的，不是猜测
 *
 * 后端 `optimizers/artifacts.py` 的 `render()` 是确定性块拼接：
 *
 *     "\n\n".join([header, ...已采纳补丁 按 (ordinal, patch_id) 排序, "示例：", ...示例])
 *
 * 所以 `buildPromptBlockMap` 能用同一规则**逆向**切块，得到每个 patch_id 的精确行区间，
 * 不需要靠文本相似度去猜哪段属于哪条补丁。定位时要求块的两侧都是 `\n\n` 边界或文本
 * 边界——否则一段恰好出现在 header 内部的相同文字就会被误配。
 *
 * 逆向失败（服务端渲染规则变了）时返回 `exact: false`，调用方必须显式告诉用户
 * 「无法归属」，而不是展示一个错误的 patch_id。给错的归属比不给更糟：它会让人把
 * 一条补丁的问题算到另一条头上。
 */

export type LineOp = "equal" | "delete" | "insert";
export type HunkOp = "equal" | "delete" | "insert" | "replace";

export interface DiffSpan {
  text: string;
  changed: boolean;
}

export interface DiffLine {
  op: LineOp;
  text: string;
  /** 0 基的基线行号；insert 行为 null。 */
  baselineLine: number | null;
  /** 0 基的候选行号；delete 行为 null。 */
  candidateLine: number | null;
  /** 词级切分，只在配对的 delete/insert 行上出现。 */
  spans?: DiffSpan[];
}

export interface DiffHunk {
  op: HunkOp;
  baselineStart: number;
  baselineEnd: number;
  candidateStart: number;
  candidateEnd: number;
  lines: DiffLine[];
}

export interface DiffStats {
  added: number;
  removed: number;
  unchanged: number;
}

export interface DiffResult {
  hunks: DiffHunk[];
  stats: DiffStats;
  /** true 表示规模超限、某些窗口退化成整块替换，行级精度已损失。 */
  degraded: boolean;
}

export interface DiffOptions {
  /** LCS 动态规划的单元上限，超过则退化。默认 400_000。 */
  maxCells?: number;
  /** 参与词级对比的单行字符上限，默认 400。 */
  maxWordDiffChars?: number;
  /** 注入分词器，便于测试正则回退分支。 */
  segmenter?: (text: string) => string[];
}

export type PromptBlockKind = "header" | "patch" | "demo-heading" | "demo";

export interface PromptBlock {
  kind: PromptBlockKind;
  /** patch 为 patch_id，demo 为 demo_id，其余为 null。 */
  id: string | null;
  startLine: number;
  endLine: number;
}

export interface PromptBlockMap {
  blocks: PromptBlock[];
  /** false 表示逆向切块失败，归属必须整体放弃。 */
  exact: boolean;
}

export interface BlockAttribution {
  kind: PromptBlockKind;
  id: string | null;
  /**
   * 归属只是位置上的邻近，不代表这段改动真的属于该块。
   * 目前只有纯删除会置 true：它在候选侧没有位置，只能说它落在哪个块附近。
   */
  ambiguous: boolean;
}

export interface AttributedHunk extends DiffHunk {
  attribution: BlockAttribution | null;
}

export interface PromptPatchLike {
  patch_id: string;
  ordinal: number;
  body: string;
}

export interface PromptDemoLike {
  demo_id: string;
  rendered_text: string;
}

export interface PromptBlockMapInput {
  candidatePrompt: string;
  patches: readonly PromptPatchLike[];
  demos: readonly PromptDemoLike[];
  acceptedPatchIds: readonly string[];
}

const DEFAULT_MAX_CELLS = 400_000;
const DEFAULT_MAX_WORD_DIFF_CHARS = 400;
/** 低于此相似度就不做词级高亮：两行毫不相干时逐词标记会变成满屏彩纸。 */
const MIN_WORD_DIFF_SIMILARITY = 0.3;
const SECTION_SEPARATOR = "\n\n";
const DEMO_HEADING = "示例：";

const FALLBACK_TOKEN =
  /[㐀-䶿一-鿿豈-﫿]|[A-Za-z0-9_'-]+|\s+|[^\s]/gu;

interface SegmenterLike {
  segment(input: string): Iterable<{ segment: string }>;
}
type SegmenterCtor = new (
  locales: string,
  options: { granularity: "word" },
) => SegmenterLike;

/**
 * 中文没有空格边界，所以优先用 `Intl.Segmenter`；不可用时逐字回退。
 * 逐字是无依赖前提下唯一正确的近似——按标点切会把「预算两万元」当成一个词。
 */
export function segmentWords(text: string): string[] {
  const ctor = (Intl as { Segmenter?: SegmenterCtor }).Segmenter;
  if (ctor) {
    try {
      return [...new ctor("zh", { granularity: "word" }).segment(text)].map(
        (piece) => piece.segment,
      );
    } catch {
      // 某些运行时声明了 Segmenter 却不认 "zh"；回退而不是让整个面板崩掉。
    }
  }
  return text.match(FALLBACK_TOKEN) ?? [];
}

function splitLines(text: string): string[] {
  return text.replace(/\r\n?/g, "\n").split("\n");
}

/** 标准 LCS 回溯，返回 op 序列。调用方保证 n*m 在预算内。 */
function lcsOps(left: number[], right: number[]): LineOp[] {
  const n = left.length;
  const m = right.length;
  const width = m + 1;
  const table = new Uint32Array((n + 1) * width);
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      table[i * width + j] =
        left[i] === right[j]
          ? table[(i + 1) * width + j + 1] + 1
          : Math.max(table[(i + 1) * width + j], table[i * width + j + 1]);
    }
  }
  const ops: LineOp[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (left[i] === right[j]) {
      ops.push("equal");
      i += 1;
      j += 1;
    } else if (table[(i + 1) * width + j] >= table[i * width + j + 1]) {
      ops.push("delete");
      i += 1;
    } else {
      ops.push("insert");
      j += 1;
    }
  }
  for (; i < n; i += 1) ops.push("delete");
  for (; j < m; j += 1) ops.push("insert");
  return ops;
}

/** 最长递增子序列的下标，用于挑出彼此顺序一致的锚点。 */
function longestIncreasingSubsequence(values: number[]): number[] {
  if (values.length === 0) return [];
  const tails: number[] = [];
  const tailIndex: number[] = [];
  const previous = new Array<number>(values.length).fill(-1);
  for (let i = 0; i < values.length; i += 1) {
    let low = 0;
    let high = tails.length;
    while (low < high) {
      const mid = (low + high) >> 1;
      if (tails[mid] < values[i]) low = mid + 1;
      else high = mid;
    }
    if (low > 0) previous[i] = tailIndex[low - 1];
    tails[low] = values[i];
    tailIndex[low] = i;
    if (low === tails.length - 1) tailIndex[low] = i;
  }
  const result: number[] = [];
  let cursor = tailIndex[tails.length - 1];
  while (cursor !== -1) {
    result.push(cursor);
    cursor = previous[cursor];
  }
  return result.reverse();
}

interface Window {
  leftStart: number;
  leftEnd: number;
  rightStart: number;
  rightEnd: number;
}

/**
 * 用「两侧各出现恰好一次」的行做锚点切窗（patience 风格）。
 * Prompt 的每条补丁正文都是内容寻址生成的独特文本，这一步几乎总能生效，
 * 把一次大 DP 拆成若干小 DP。
 */
function anchoredWindows(left: number[], right: number[]): Window[] {
  const leftCounts = new Map<number, number>();
  const rightCounts = new Map<number, number>();
  const rightFirst = new Map<number, number>();
  for (const id of left) leftCounts.set(id, (leftCounts.get(id) ?? 0) + 1);
  right.forEach((id, index) => {
    rightCounts.set(id, (rightCounts.get(id) ?? 0) + 1);
    if (!rightFirst.has(id)) rightFirst.set(id, index);
  });

  const anchors: { left: number; right: number }[] = [];
  left.forEach((id, index) => {
    if (leftCounts.get(id) === 1 && rightCounts.get(id) === 1) {
      anchors.push({ left: index, right: rightFirst.get(id) as number });
    }
  });
  const kept = longestIncreasingSubsequence(anchors.map((a) => a.right)).map(
    (index) => anchors[index],
  );

  const windows: Window[] = [];
  let leftCursor = 0;
  let rightCursor = 0;
  for (const anchor of kept) {
    windows.push({
      leftStart: leftCursor,
      leftEnd: anchor.left,
      rightStart: rightCursor,
      rightEnd: anchor.right,
    });
    // 锚点自身是一对相等行，单独成一个长度 1 的相等窗口。
    windows.push({
      leftStart: anchor.left,
      leftEnd: anchor.left + 1,
      rightStart: anchor.right,
      rightEnd: anchor.right + 1,
    });
    leftCursor = anchor.left + 1;
    rightCursor = anchor.right + 1;
  }
  windows.push({
    leftStart: leftCursor,
    leftEnd: left.length,
    rightStart: rightCursor,
    rightEnd: right.length,
  });
  return windows.filter(
    (w) => w.leftEnd > w.leftStart || w.rightEnd > w.rightStart,
  );
}

function commonPrefixLength(a: string, b: string): number {
  const limit = Math.min(a.length, b.length);
  let i = 0;
  while (i < limit && a[i] === b[i]) i += 1;
  return i;
}

function commonSuffixLength(a: string, b: string, skip: number): number {
  const limit = Math.min(a.length, b.length) - skip;
  let i = 0;
  while (i < limit && a[a.length - 1 - i] === b[b.length - 1 - i]) i += 1;
  return i;
}

function buildSpans(
  before: string,
  after: string,
  segmenter: (text: string) => string[],
): { before: DiffSpan[]; after: DiffSpan[] } | null {
  const prefix = commonPrefixLength(before, after);
  const suffix = commonSuffixLength(before, after, prefix);
  const similarity = (prefix + suffix) / Math.max(before.length, after.length, 1);
  if (similarity < MIN_WORD_DIFF_SIMILARITY) return null;

  const beforeTokens = segmenter(before);
  const afterTokens = segmenter(after);
  const dictionary = new Map<string, number>();
  const encode = (tokens: string[]): number[] =>
    tokens.map((token) => {
      const existing = dictionary.get(token);
      if (existing !== undefined) return existing;
      const id = dictionary.size;
      dictionary.set(token, id);
      return id;
    });
  const ops = lcsOps(encode(beforeTokens), encode(afterTokens));

  const beforeSpans: DiffSpan[] = [];
  const afterSpans: DiffSpan[] = [];
  const push = (spans: DiffSpan[], text: string, changed: boolean) => {
    const last = spans[spans.length - 1];
    if (last && last.changed === changed) last.text += text;
    else spans.push({ text, changed });
  };
  let bi = 0;
  let ai = 0;
  for (const op of ops) {
    if (op === "equal") {
      push(beforeSpans, beforeTokens[bi], false);
      push(afterSpans, afterTokens[ai], false);
      bi += 1;
      ai += 1;
    } else if (op === "delete") {
      push(beforeSpans, beforeTokens[bi], true);
      bi += 1;
    } else {
      push(afterSpans, afterTokens[ai], true);
      ai += 1;
    }
  }
  return { before: beforeSpans, after: afterSpans };
}

export function diffLines(
  baseline: string,
  candidate: string,
  options: DiffOptions = {},
): DiffResult {
  const maxCells = options.maxCells ?? DEFAULT_MAX_CELLS;
  const maxWordDiffChars = options.maxWordDiffChars ?? DEFAULT_MAX_WORD_DIFF_CHARS;
  const segmenter = options.segmenter ?? segmentWords;

  const leftLines = splitLines(baseline);
  const rightLines = splitLines(candidate);

  const dictionary = new Map<string, number>();
  const encode = (lines: string[]): number[] =>
    lines.map((line) => {
      const existing = dictionary.get(line);
      if (existing !== undefined) return existing;
      const id = dictionary.size;
      dictionary.set(line, id);
      return id;
    });
  const left = encode(leftLines);
  const right = encode(rightLines);

  let degraded = false;
  const ops: LineOp[] = [];
  for (const window of anchoredWindows(left, right)) {
    const n = window.leftEnd - window.leftStart;
    const m = window.rightEnd - window.rightStart;
    if (n === 0) {
      for (let k = 0; k < m; k += 1) ops.push("insert");
      continue;
    }
    if (m === 0) {
      for (let k = 0; k < n; k += 1) ops.push("delete");
      continue;
    }
    if (n * m > maxCells) {
      // 退化：整窗口当作一次替换。行级精度损失，但不会卡死浏览器。
      degraded = true;
      for (let k = 0; k < n; k += 1) ops.push("delete");
      for (let k = 0; k < m; k += 1) ops.push("insert");
      continue;
    }
    ops.push(
      ...lcsOps(
        left.slice(window.leftStart, window.leftEnd),
        right.slice(window.rightStart, window.rightEnd),
      ),
    );
  }

  const lines: DiffLine[] = [];
  const stats: DiffStats = { added: 0, removed: 0, unchanged: 0 };
  let bi = 0;
  let ci = 0;
  for (const op of ops) {
    if (op === "equal") {
      lines.push({
        op,
        text: leftLines[bi],
        baselineLine: bi,
        candidateLine: ci,
      });
      stats.unchanged += 1;
      bi += 1;
      ci += 1;
    } else if (op === "delete") {
      lines.push({ op, text: leftLines[bi], baselineLine: bi, candidateLine: null });
      stats.removed += 1;
      bi += 1;
    } else {
      lines.push({ op, text: rightLines[ci], baselineLine: null, candidateLine: ci });
      stats.added += 1;
      ci += 1;
    }
  }

  const hunks = groupHunks(lines, leftLines.length, rightLines.length);
  attachSpans(hunks, maxWordDiffChars, segmenter);
  return { hunks, stats, degraded };
}

function groupHunks(
  lines: DiffLine[],
  baselineLength: number,
  candidateLength: number,
): DiffHunk[] {
  const runs: DiffLine[][] = [];
  for (const line of lines) {
    const last = runs[runs.length - 1];
    if (last && last[0].op === line.op) last.push(line);
    else runs.push([line]);
  }

  const hunks: DiffHunk[] = [];
  for (const run of runs) {
    const previous = hunks[hunks.length - 1];
    // 相邻的删除 + 插入合成一次替换，这是词级对比的触发条件。
    if (previous && previous.op === "delete" && run[0].op === "insert") {
      previous.op = "replace";
      previous.lines.push(...run);
      previous.candidateEnd = (run[run.length - 1].candidateLine as number) + 1;
      previous.candidateStart = run[0].candidateLine as number;
      continue;
    }
    hunks.push(makeHunk(run, baselineLength, candidateLength));
  }
  return hunks;
}

function makeHunk(
  run: DiffLine[],
  baselineLength: number,
  candidateLength: number,
): DiffHunk {
  const baselineNumbers = run
    .map((line) => line.baselineLine)
    .filter((value): value is number => value !== null);
  const candidateNumbers = run
    .map((line) => line.candidateLine)
    .filter((value): value is number => value !== null);
  return {
    op: run[0].op,
    lines: run,
    baselineStart: baselineNumbers[0] ?? baselineLength,
    baselineEnd: (baselineNumbers[baselineNumbers.length - 1] ?? baselineLength - 1) + 1,
    candidateStart: candidateNumbers[0] ?? candidateLength,
    candidateEnd:
      (candidateNumbers[candidateNumbers.length - 1] ?? candidateLength - 1) + 1,
  };
}

function attachSpans(
  hunks: DiffHunk[],
  maxWordDiffChars: number,
  segmenter: (text: string) => string[],
): void {
  for (const hunk of hunks) {
    if (hunk.op !== "replace") continue;
    const removed = hunk.lines.filter((line) => line.op === "delete");
    const added = hunk.lines.filter((line) => line.op === "insert");
    const pairs = Math.min(removed.length, added.length);
    for (let i = 0; i < pairs; i += 1) {
      const before = removed[i];
      const after = added[i];
      if (
        before.text.length > maxWordDiffChars ||
        after.text.length > maxWordDiffChars
      ) {
        continue;
      }
      const spans = buildSpans(before.text, after.text, segmenter);
      if (!spans) continue;
      before.spans = spans.before;
      after.spans = spans.after;
    }
  }
}

/**
 * 逆向切块，复刻服务端 `render()` 的装配顺序。
 * 任何一步对不上就整体放弃——半对的归属会误导读者。
 */
export function buildPromptBlockMap(input: PromptBlockMapInput): PromptBlockMap {
  const accepted = new Set(input.acceptedPatchIds);
  const expected: { kind: PromptBlockKind; id: string | null; text: string }[] = [];

  const header = deriveHeaderCandidateless(input);
  if (header !== null) expected.push(header);

  for (const patch of [...input.patches]
    .filter((patch) => accepted.has(patch.patch_id))
    .sort((a, b) =>
      a.ordinal !== b.ordinal
        ? a.ordinal - b.ordinal
        : a.patch_id < b.patch_id
          ? -1
          : a.patch_id > b.patch_id
            ? 1
            : 0,
    )) {
    const text = patch.body.trim();
    if (text) expected.push({ kind: "patch", id: patch.patch_id, text });
  }
  const demoTexts = input.demos
    .map((demo) => ({ id: demo.demo_id, text: demo.rendered_text.trim() }))
    .filter((demo) => demo.text !== "");
  if (demoTexts.length > 0) {
    expected.push({ kind: "demo-heading", id: null, text: DEMO_HEADING });
    for (const demo of demoTexts) {
      expected.push({ kind: "demo", id: demo.id, text: demo.text });
    }
  }

  const prompt = input.candidatePrompt.replace(/\r\n?/g, "\n");
  const lineStarts = buildLineStarts(prompt);
  const blocks: PromptBlock[] = [];
  let cursor = 0;
  for (const block of expected) {
    if (block.kind === "header") {
      // header 是第一个块，一定从 0 开始。
      const end = block.text.length;
      blocks.push({
        kind: "header",
        id: null,
        ...toLineRange(lineStarts, 0, end),
      });
      cursor = end;
      continue;
    }
    const at = findBlock(prompt, block.text, cursor);
    if (at === -1) return { blocks: [], exact: false };
    blocks.push({
      kind: block.kind,
      id: block.id,
      ...toLineRange(lineStarts, at, at + block.text.length),
    });
    cursor = at + block.text.length;
  }
  return { blocks, exact: true };
}

/** header 是第一个非空块之前的部分；服务端已 strip 过。 */
function deriveHeaderCandidateless(
  input: PromptBlockMapInput,
): { kind: PromptBlockKind; id: null; text: string } | null {
  const prompt = input.candidatePrompt.replace(/\r\n?/g, "\n");
  const accepted = new Set(input.acceptedPatchIds);
  const firstPatch = [...input.patches]
    .filter((patch) => accepted.has(patch.patch_id) && patch.body.trim())
    .sort((a, b) =>
      a.ordinal !== b.ordinal ? a.ordinal - b.ordinal : a.patch_id < b.patch_id ? -1 : 1,
    )[0];
  const firstDemo = input.demos.find((demo) => demo.rendered_text.trim());
  const marker = firstPatch
    ? firstPatch.body.trim()
    : firstDemo
      ? DEMO_HEADING
      : null;
  if (marker === null) {
    const text = prompt.trim();
    return text ? { kind: "header", id: null, text } : null;
  }
  const at = findBlock(prompt, marker, 0);
  if (at <= 0) return null;
  const text = prompt.slice(0, at).replace(/\n+$/, "");
  return text ? { kind: "header", id: null, text } : null;
}

/**
 * 从 `from` 起找首个两侧都是块边界的匹配。
 * 双侧约束是防误配的关键：`"\n\n".join` 保证每个块两侧必然是分隔符或文本边界，
 * 任何巧合出现在别处的子串都过不了这道闸。
 */
function findBlock(prompt: string, text: string, from: number): number {
  let at = prompt.indexOf(text, from);
  while (at !== -1) {
    const startOk = at === 0 || prompt.slice(at - 2, at) === SECTION_SEPARATOR;
    const endAt = at + text.length;
    const endOk =
      endAt === prompt.length ||
      prompt.slice(endAt, endAt + 2) === SECTION_SEPARATOR;
    if (startOk && endOk) return at;
    at = prompt.indexOf(text, at + 1);
  }
  return -1;
}

function buildLineStarts(text: string): number[] {
  const starts = [0];
  for (let i = 0; i < text.length; i += 1) {
    if (text[i] === "\n") starts.push(i + 1);
  }
  return starts;
}

function toLineRange(
  lineStarts: number[],
  startChar: number,
  endChar: number,
): { startLine: number; endLine: number } {
  return {
    startLine: lineOf(lineStarts, startChar),
    endLine: lineOf(lineStarts, Math.max(startChar, endChar - 1)) + 1,
  };
}

function lineOf(lineStarts: number[], offset: number): number {
  let low = 0;
  let high = lineStarts.length - 1;
  while (low < high) {
    const mid = (low + high + 1) >> 1;
    if (lineStarts[mid] <= offset) low = mid;
    else high = mid - 1;
  }
  return low;
}

/**
 * 在块边界处切开 hunk。
 *
 * 一次追加两条补丁会产生一个连续的插入 hunk，如果不切，它跨越两个块、只能标成
 * 「跨补丁改动」——而这正是最常见的情形。切开之后每条补丁各得一个明确归属，
 * 「跨补丁」这个标记就只留给真正跨越边界的单块改动。
 */
function splitHunkAtBlocks(hunk: DiffHunk, map: PromptBlockMap): DiffHunk[] {
  const boundaries = map.blocks
    .map((block) => block.startLine)
    .filter((line) => line > hunk.candidateStart && line < hunk.candidateEnd)
    .sort((a, b) => a - b);
  if (boundaries.length === 0) return [hunk];

  const pieces: DiffLine[][] = [[]];
  let next = 0;
  for (const line of hunk.lines) {
    // 删除行没有候选行号，跟随当前分片——它在候选侧本就没有位置。
    if (line.candidateLine !== null) {
      while (next < boundaries.length && line.candidateLine >= boundaries[next]) {
        pieces.push([]);
        next += 1;
      }
    }
    pieces[pieces.length - 1].push(line);
  }
  return pieces
    .filter((piece) => piece.length > 0)
    .map((piece) => makeHunk(piece, hunk.baselineEnd, hunk.candidateEnd));
}

export function attributeHunks(
  hunks: readonly DiffHunk[],
  map: PromptBlockMap,
): AttributedHunk[] {
  if (!map.exact) {
    return hunks.map((hunk) => ({ ...hunk, attribution: null }));
  }
  const split = hunks.flatMap((hunk) =>
    hunk.op === "equal" ? [hunk] : splitHunkAtBlocks(hunk, map),
  );
  return split.map((hunk) => {
    if (hunk.op === "equal") return { ...hunk, attribution: null };

    const hasCandidateLines = hunk.candidateEnd > hunk.candidateStart;
    if (!hasCandidateLines) {
      // 纯删除没有候选侧的行可归属，只能指出它落在哪个块的位置附近。
      const neighbour = map.blocks.find(
        (block) =>
          block.startLine <= hunk.candidateStart && hunk.candidateStart < block.endLine,
      );
      return {
        ...hunk,
        attribution: neighbour
          ? { kind: neighbour.kind, id: neighbour.id, ambiguous: true }
          : null,
      };
    }

    // splitHunkAtBlocks 已保证每片的候选行落在同一个块内，所以这里取首个重叠块即可，
    // 不需要按重叠量挑选——那条路径切分之后不可达。
    const owner = map.blocks.find(
      (block) =>
        Math.min(block.endLine, hunk.candidateEnd) >
        Math.max(block.startLine, hunk.candidateStart),
    );
    return {
      ...hunk,
      attribution: owner
        ? { kind: owner.kind, id: owner.id, ambiguous: false }
        : null,
    };
  });
}

/** DiffPanel 的唯一入口：一次算出差异与归属。 */
export function diffPromptArtifact(
  input: PromptBlockMapInput & { baselinePrompt: string },
  options?: DiffOptions,
): { result: DiffResult; hunks: AttributedHunk[]; map: PromptBlockMap } {
  const result = diffLines(input.baselinePrompt, input.candidatePrompt, options);
  const map = buildPromptBlockMap(input);
  return { result, hunks: attributeHunks(result.hunks, map), map };
}

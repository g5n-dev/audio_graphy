import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import type {
  TagInsightEvidenceRef,
  TagInsightMatrixRow,
} from "@/types/api";
import { TagMatrix } from "./TagMatrix";

function makeRow(
  targetId: string,
  textExcerpt = "第一版证据",
): TagInsightMatrixRow {
  const evidence: TagInsightEvidenceRef = {
    ref_id: `evidence-${textExcerpt}`,
    kind: "audio",
    recording_id: "recording-1",
    start_ms: 1_000,
    end_ms: 2_000,
    text_excerpt: textExcerpt,
  };
  return {
    target_id: targetId,
    window: { start_ms: 0, end_ms: 10_000 },
    label_key: "stage.greeting",
    store_ids: ["store-1"],
    agent_ids: ["agent-1"],
    cells: [],
    merged: {
      strategy: "manual_wins",
      values: ["pass"],
      selected_group_keys: ["review"],
      confidence: 0.96,
      evidence_refs: [evidence],
    },
    conflict: false,
    missing_group_keys: [],
  };
}

function renderMatrix(rows: TagInsightMatrixRow[]) {
  return render(
    <MemoryRouter>
      <TagMatrix groups={[]} rows={rows} />
    </MemoryRouter>,
  );
}

describe("TagMatrix", () => {
  it("resets evidence selection and viewport when the dataset content changes", async () => {
    const firstRows = [makeRow("reception:42/unit:7")];
    const rendered = renderMatrix(firstRows);

    fireEvent.click(
      screen.getByRole("cell", {
        name: "查看 reception:42/unit:7 stage.greeting 的证据",
      }),
    );
    expect(screen.getByText("第一版证据")).toBeInTheDocument();

    const viewport = rendered.container.querySelector(
      ".ag-matrix__viewport",
    ) as HTMLDivElement;
    viewport.scrollTop = 180;
    fireEvent.scroll(viewport);
    expect(viewport.scrollTop).toBe(180);

    rendered.rerender(
      <MemoryRouter>
        <TagMatrix
          groups={[]}
          rows={[makeRow("reception:42/unit:7", "更新后的证据")]}
        />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(viewport.scrollTop).toBe(0);
      expect(
        screen.getByText("选择矩阵左侧的目标/标签单元查看原音与文本证据。"),
      ).toBeInTheDocument();
    });
    expect(screen.queryByText("更新后的证据")).not.toBeInTheDocument();
  });

  it.each([
    ["reception:42/unit:7", "/receptions/42/workspace?recording=recording-1&at=1000"],
    ["42", "/receptions/42/workspace?recording=recording-1&at=1000"],
  ])("links parseable target %s to its reception workspace", (targetId, href) => {
    renderMatrix([makeRow(targetId)]);

    fireEvent.click(
      screen.getByRole("cell", {
        name: `查看 ${targetId} stage.greeting 的证据`,
      }),
    );

    expect(
      screen.getByRole("link", { name: /到调听工作台定位/ }),
    ).toHaveAttribute("href", href);
  });

  it.each([
    "reception-42",
    "reception:42",
    "reception:abc/unit:unit-a",
    "reception:42/unit:7/extra",
    "snapshot:42",
  ])("does not invent a reception link for target %s", (targetId) => {
    renderMatrix([makeRow(targetId)]);

    fireEvent.click(
      screen.getByRole("cell", {
        name: `查看 ${targetId} stage.greeting 的证据`,
      }),
    );

    expect(
      screen.queryByRole("link", { name: /到调听工作台定位/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent(
      "快照未提供接待映射",
    );
  });
});

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { TagFactLineageDrawer } from "./TagFactLineageDrawer";
import type { TagFactLineageResponse } from "@/types/api";

const LINEAGE = {
  fact: {
    id: 701,
    source: "llm",
    tag_key: "intent.purchase",
    tag_value: "high",
    input_hash: "sha256:abc",
    evidence_refs: [
      { ref_id: "segment:77", recording_id: 9, start_sec: 3.1 },
    ],
  },
  is_current: true,
  schema_version: { id: 11, version: "2.1.0" },
  tagger_version: { id: 42, version: "tagger-2.1", engine: "hybrid" },
  model_version: "model-a",
  extraction_run: { id: 81, status: "completed" },
  job: { id: 88, status: "completed" },
  deployment: { id: 9, status: "production" },
} as unknown as TagFactLineageResponse;

describe("TagFactLineageDrawer", () => {
  it("shows the complete, role-filtered fact lineage", () => {
    render(
      <TagFactLineageDrawer
        factId={701}
        data={LINEAGE}
        pending={false}
        error={null}
        onRetry={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "标签事实 #701 溯源" }),
    ).toBeInTheDocument();
    expect(screen.getByText("sha256:abc")).toBeInTheDocument();
    expect(screen.getByText("2.1.0 (#11)")).toBeInTheDocument();
    expect(screen.getByText("tagger-2.1 / hybrid")).toBeInTheDocument();
    expect(screen.getByText("model-a")).toBeInTheDocument();
    expect(screen.getByText("Job 88 / Run 81")).toBeInTheDocument();
    expect(screen.getByText("#9 · production")).toBeInTheDocument();
    expect(screen.getByText("segment:77")).toBeInTheDocument();
  });

  it("keeps an accessible retry path when lineage loading fails", async () => {
    const onRetry = vi.fn();
    render(
      <TagFactLineageDrawer
        factId={701}
        pending={false}
        error={new Error("溯源服务不可用")}
        onRetry={onRetry}
        onClose={vi.fn()}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("溯源服务不可用");
    await userEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ReceptionTagAssignment, TagDefinition } from "@/types/api";
import { TagAssignmentEditor } from "./TagAssignmentEditor";

const TAG: ReceptionTagAssignment = {
  id: 701,
  dialogue_unit_id: 501,
  group_key: "sales",
  group_version: "v2",
  label_key: "objection.price",
  label_value: "medium",
  confidence: 0.82,
  source: "llm",
  is_manual: false,
  model_run_id: "fact:701",
  evidence_refs: [
    {
      ref_id: "segment:77",
      kind: "audio",
      recording_id: 101,
      start_ms: 1_000,
      end_ms: 4_000,
      timeline_start_ms: 31_000,
      timeline_end_ms: 34_000,
    },
    {
      ref_id: "segment:78",
      kind: "audio",
      recording_id: 101,
      start_ms: 5_000,
      end_ms: 8_000,
      timeline_start_ms: 35_000,
      timeline_end_ms: 38_000,
    },
  ],
};

const DEFINITION: TagDefinition = {
  key: "objection.price",
  name: "价格异议强度",
  category: "objection",
  value_type: "enum",
  allowed_values: ["low", "medium", "high"],
  subject_types: ["dialogue_unit"],
  scenarios: ["automotive"],
  evidence_required: true,
  critical: true,
  threshold: 0.75,
};

describe("TagAssignmentEditor", () => {
  it("drives values from the published schema and requires reason plus retained evidence", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    const onSeekEvidence = vi.fn();
    const onViewLineage = vi.fn();
    render(
      <TagAssignmentEditor
        tag={TAG}
        definition={DEFINITION}
        isSaving={false}
        onCancel={vi.fn()}
        onSeekEvidence={onSeekEvidence}
        onViewLineage={onViewLineage}
        onSubmit={onSubmit}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "编辑标签 · 价格异议强度" }),
    ).toBeInTheDocument();
    expect(screen.getByText("sales@v2")).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "查看事实 #701 溯源" }),
    );
    expect(onViewLineage).toHaveBeenCalledWith(701);

    await user.selectOptions(
      screen.getByRole("combobox", { name: "标签值" }),
      "high",
    );
    await user.click(
      screen.getByRole("checkbox", { name: /证据 segment:78/ }),
    );
    expect(screen.getByRole("button", { name: "保存人工更正" })).toBeDisabled();

    await user.type(
      screen.getByRole("textbox", { name: "标签编辑原因" }),
      "复核录音后确认异议强烈",
    );
    await user.click(screen.getByRole("button", { name: "保存人工更正" }));

    expect(onSubmit).toHaveBeenCalledWith({
      labelValue: "high",
      reason: "复核录音后确认异议强烈",
      evidenceRefIds: ["segment:77"],
    });

    await user.click(
      screen.getByRole("button", { name: "回听证据 segment:77" }),
    );
    expect(onSeekEvidence).toHaveBeenCalledWith(TAG.evidence_refs[0]);
  });

  it("does not allow a correction without verifiable evidence", async () => {
    const user = userEvent.setup();
    render(
      <TagAssignmentEditor
        tag={{ ...TAG, evidence_refs: [] }}
        definition={DEFINITION}
        isSaving={false}
        onCancel={vi.fn()}
        onSeekEvidence={vi.fn()}
        onSubmit={vi.fn()}
      />,
    );

    await user.type(
      screen.getByRole("textbox", { name: "标签编辑原因" }),
      "人工复核",
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "该标签没有可验证证据，不能直接覆盖",
    );
    expect(screen.getByRole("button", { name: "保存人工更正" })).toBeDisabled();
  });

  it("keeps a dirty draft until the operator confirms it can be discarded", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    const confirm = vi
      .spyOn(window, "confirm")
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true);
    render(
      <TagAssignmentEditor
        tag={TAG}
        definition={DEFINITION}
        isSaving={false}
        onCancel={onCancel}
        onSeekEvidence={vi.fn()}
        onSubmit={vi.fn()}
      />,
    );

    await user.type(
      screen.getByRole("textbox", { name: "标签编辑原因" }),
      "仍在核验证据",
    );
    await user.click(screen.getByRole("button", { name: "取消" }));
    expect(onCancel).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "取消" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    confirm.mockRestore();
  });

  it("renders boolean values from the schema as a constrained selector", async () => {
    const user = userEvent.setup();
    render(
      <TagAssignmentEditor
        tag={{ ...TAG, label_value: "true" }}
        definition={{
          ...DEFINITION,
          value_type: "boolean",
          allowed_values: [],
        }}
        isSaving={false}
        onCancel={vi.fn()}
        onSeekEvidence={vi.fn()}
        onSubmit={vi.fn()}
      />,
    );

    const value = screen.getByRole("combobox", { name: "标签值" });
    expect(value).toHaveValue("true");
    await user.selectOptions(value, "false");
    expect(value).toHaveValue("false");
  });
});

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { TagDefinition } from "@/types/api";
import { TagDefinitionTree } from "./TagDefinitionTree";

function definition(overrides: Partial<TagDefinition> = {}): TagDefinition {
  return {
    key: "intent.purchase",
    name: "购买意向",
    category: "intent",
    value_type: "enum",
    allowed_values: ["low", "high"],
    subject_types: ["dialogue_unit"],
    scenarios: [],
    evidence_required: true,
    critical: false,
    required: false,
    threshold: 0.75,
    ...overrides,
  } as TagDefinition;
}

/** 受控组件:测试里自己持有状态,才能断言编辑真的传了出去。 */
function Harness({
  initial,
  onChange,
}: {
  initial: TagDefinition[];
  onChange?: (next: TagDefinition[]) => void;
}) {
  const [definitions, setDefinitions] = useState(initial);
  return (
    <TagDefinitionTree
      definitions={definitions}
      onChange={(next) => {
        setDefinitions(next);
        onChange?.(next);
      }}
    />
  );
}

describe("TagDefinitionTree", () => {
  it("groups tags by category — the hierarchy the backend contract already has", () => {
    render(
      <Harness
        initial={[
          definition(),
          definition({ key: "objection.price", category: "objection" }),
        ]}
      />,
    );

    // 分组名可编辑,且计数按组:层级不是新造的概念,就是 category。
    expect(screen.getByLabelText("分组名 intent")).toHaveValue("intent");
    expect(screen.getByLabelText("分组名 objection")).toHaveValue("objection");
    expect(screen.getAllByText("1 个标签")).toHaveLength(2);
  });

  it("renaming a group moves every tag in it", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <Harness
        onChange={onChange}
        initial={[definition(), definition({ key: "intent.timing" })]}
      />,
    );

    const groupInput = screen.getByLabelText("分组名 intent");
    await user.clear(groupInput);
    await user.type(groupInput, "x");

    const last = onChange.mock.calls.at(-1)?.[0] as TagDefinition[];
    // 两个标签都要跟着改,否则会分裂成两个组。
    expect(last.every((item) => item.category === "x")).toBe(true);
  });

  it("only offers sibling tags as dependencies, never the tag itself", async () => {
    const user = userEvent.setup();
    render(
      <Harness
        initial={[
          definition(),
          definition({ key: "intent.timing", category: "intent" }),
        ]}
      />,
    );

    await user.click(screen.getAllByRole("button", { name: "▸" })[0]);

    const select = screen.getByLabelText("依赖标签 intent.purchase");
    const options = within(select).getAllByRole("option");
    // 引用不存在的 key 是发布后才会炸的错;自引用后端直接拒绝。
    expect(options.map((option) => option.textContent)).toEqual([
      "intent.timing",
    ]);
  });

  it("hides the value-domain field for non-enum tags", async () => {
    const user = userEvent.setup();
    render(<Harness initial={[definition({ value_type: "boolean" })]} />);

    await user.click(screen.getAllByRole("button", { name: "▸" })[0]);
    expect(
      screen.queryByLabelText("取值域 intent.purchase"),
    ).not.toBeInTheDocument();
  });

  it("adds and removes tags", async () => {
    const user = userEvent.setup();
    render(<Harness initial={[definition()]} />);

    await user.click(screen.getByRole("button", { name: "+ 添加标签" }));
    expect(screen.getAllByRole("button", { name: /^删除标签/ })).toHaveLength(2);

    await user.click(
      screen.getByRole("button", { name: "删除标签 intent.purchase" }),
    );
    expect(screen.getAllByRole("button", { name: /^删除标签/ })).toHaveLength(1);
  });
});

import { render, screen } from "@testing-library/react";
import { fireEvent } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { useTabList } from "./useTabList";

const TABS = [
  { id: "one", label: "第一" },
  { id: "two", label: "第二" },
  { id: "three", label: "第三" },
] as const;
type TabId = (typeof TABS)[number]["id"];

function Harness({ focusMode }: { focusMode?: "sync" | "animation-frame" }) {
  const [activeId, setActiveId] = useState<TabId>("one");
  const { tabProps } = useTabList<TabId>({
    tabs: TABS,
    activeId,
    onSelect: setActiveId,
    focusMode,
  });
  return (
    <div role="tablist" aria-label="测试标签页">
      {TABS.map((tab, index) => (
        <button key={tab.id} type="button" {...tabProps(tab.id, index)}>
          {tab.label}
        </button>
      ))}
    </div>
  );
}

function tab(name: string): HTMLElement {
  return screen.getByRole("tab", { name });
}

describe("useTabList", () => {
  it("moves selection and focus to the next tab on ArrowRight", () => {
    render(<Harness />);

    fireEvent.keyDown(tab("第一"), { key: "ArrowRight" });

    expect(tab("第二")).toHaveAttribute("aria-selected", "true");
    expect(tab("第二")).toHaveFocus();
  });

  it("wraps from the last tab back to the first", () => {
    render(<Harness />);

    fireEvent.keyDown(tab("第一"), { key: "End" });
    fireEvent.keyDown(tab("第三"), { key: "ArrowRight" });

    expect(tab("第一")).toHaveAttribute("aria-selected", "true");
  });

  it("wraps backwards from the first tab to the last", () => {
    render(<Harness />);

    fireEvent.keyDown(tab("第一"), { key: "ArrowLeft" });

    expect(tab("第三")).toHaveAttribute("aria-selected", "true");
  });

  it("jumps to the ends with Home and End", () => {
    render(<Harness />);

    fireEvent.keyDown(tab("第一"), { key: "End" });
    expect(tab("第三")).toHaveAttribute("aria-selected", "true");

    fireEvent.keyDown(tab("第三"), { key: "Home" });
    expect(tab("第一")).toHaveAttribute("aria-selected", "true");
  });

  it("leaves non-navigation keys to the browser", () => {
    render(<Harness />);

    const event = new KeyboardEvent("keydown", {
      key: "Enter",
      bubbles: true,
      cancelable: true,
    });
    tab("第一").dispatchEvent(event);

    expect(event.defaultPrevented).toBe(false);
    expect(tab("第一")).toHaveAttribute("aria-selected", "true");
  });

  it("keeps only the active tab in the page tab order", () => {
    render(<Harness />);

    expect(tab("第一")).toHaveAttribute("tabindex", "0");
    expect(tab("第二")).toHaveAttribute("tabindex", "-1");
    expect(tab("第三")).toHaveAttribute("tabindex", "-1");
  });

  it("defers focus to the next frame when asked", async () => {
    render(<Harness focusMode="animation-frame" />);

    fireEvent.keyDown(tab("第一"), { key: "ArrowRight" });
    expect(tab("第二")).toHaveAttribute("aria-selected", "true");

    await new Promise((resolve) => requestAnimationFrame(() => resolve(null)));
    expect(tab("第二")).toHaveFocus();
  });
});

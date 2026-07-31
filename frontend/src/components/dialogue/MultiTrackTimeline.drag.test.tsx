import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { ReceptionWorkspaceResponse } from "@/types/api";
import { MultiTrackTimeline } from "./MultiTrackTimeline";

/**
 * QA 补充测试 —— 专测工程师新增的 useDragPan 拖动平移 + 拖动/点击抑制逻辑。
 *
 * jsdom 原生不支持 PointerEvent / setPointerCapture / hasPointerCapture，
 * 这里统一 polyfill，再用原生 MouseEvent 派发 pointerdown/move/up/cancel，
 * 让 React 合成事件与 window.addEventListener 监听器都能正常触发。
 */

const EMPTY = {
  total: 0,
  returned: 0,
  limit: 100,
  truncated: false,
};

const WORKSPACE: ReceptionWorkspaceResponse = {
  reception: {
    id: 9,
    tenant_id: "tenant-a",
    scenario: "automotive",
    store_id: "store-1",
    agent_name: "顾问小林",
    status: "ready",
    merge_mode: "logical",
    merge_confidence: 0.93,
    started_at: "2026-07-25T08:00:00Z",
    ended_at: "2026-07-25T08:20:00Z",
    duration_sec: 1_200,
    merged_audio_url: null,
    playback_expires_at: null,
    version: 3,
  },
  recordings: [
    {
      id: 101,
      name: "接待录音.wav",
      sequence_no: 0,
      timeline_start_sec: 600,
      timeline_end_sec: 1_200,
      source_start_sec: 600,
      source_end_sec: 1_200,
      source_start_ms: 600_000,
      source_end_ms: 1_200_000,
      timeline_start_ms: 600_000,
      timeline_end_ms: 1_200_000,
      gap_before_ms: 0,
      time_origin_ms: 0,
      legal_source_start_ms: 600_000,
      legal_source_end_ms: 1_200_000,
      gap_before_sec: 0,
      audio_url: null,
      playback_expires_at: null,
      decision_source: "explicit",
      merge_confidence: 1,
    },
  ],
  dialogue_units: [
    {
      id: 501,
      unit_index: 4,
      version: 1,
      start_sec: 630,
      end_sec: 690,
      topic: "报价沟通",
      business_stage: "quotation",
      summary: "客户讨论落地价。",
      boundary_confidence: 0.9,
      boundary_reasons: ["topic_change"],
      edit_status: "auto",
    },
  ],
  waveform_peaks: [0.1, 0.3, 0.5, 0.7, 0.9, 0.6, 0.4, 0.2],
  transcript_items: [],
  tag_assignments: [
    {
      id: 701,
      dialogue_unit_id: 501,
      group_key: "sales",
      group_version: "v2",
      label_key: "objection.price",
      label_value: "high",
      confidence: 0.88,
      source: "llm",
      is_manual: false,
      evidence_refs: [
        {
          ref_id: "segment:77",
          kind: "audio",
          recording_id: 101,
          start_ms: 30_000,
          end_ms: 40_000,
          timeline_start_ms: 630_000,
          timeline_end_ms: 640_000,
        },
      ],
    },
  ],
  state_transitions: [],
  audit_events: [],
  window: {
    start_sec: 600,
    end_sec: 1_200,
    size_sec: 600,
    reception_duration_sec: 1_200,
    truncated: false,
    has_previous: true,
    has_next: false,
    previous_start_sec: 0,
    next_start_sec: null,
    total_dialogue_units: 5,
    protected_dialogue_units: 1,
    dialogue_units: { ...EMPTY, total: 5, returned: 1 },
    tag_assignments: { ...EMPTY, total: 1, returned: 1 },
    state_transitions: EMPTY,
    transcript_items: EMPTY,
    provenance_events: EMPTY,
  },
};

// ---------- jsdom PointerEvent polyfill ----------
const capturedPointers = new Set<number>();

interface PointerCaptureMethods {
  setPointerCapture: (pointerId: number) => void;
  hasPointerCapture: (pointerId: number) => boolean;
  releasePointerCapture: (pointerId: number) => void;
}

beforeAll(() => {
  // jsdom 无 PointerEvent 构造器，用 MouseEvent 顶替（PointerEvent extends MouseEvent）
  if (typeof (window as unknown as { PointerEvent?: unknown }).PointerEvent === "undefined") {
    (window as unknown as { PointerEvent: unknown }).PointerEvent = MouseEvent;
  }
  const proto = HTMLElement.prototype as Partial<PointerCaptureMethods>;
  proto.setPointerCapture = function (this: HTMLElement, id: number) {
    capturedPointers.add(id);
  };
  proto.hasPointerCapture = function (this: HTMLElement, id: number) {
    return capturedPointers.has(id);
  };
  proto.releasePointerCapture = function (this: HTMLElement, id: number) {
    capturedPointers.delete(id);
  };
});

afterEach(() => {
  capturedPointers.clear();
  vi.restoreAllMocks();
});

interface PointerInit {
  pointerId?: number;
  pointerType?: string;
  clientX?: number;
  clientY?: number;
  button?: number;
  bubbles?: boolean;
}

/** 在指定目标（元素或 window）上派发一个 pointer 事件（基于 MouseEvent） */
function firePointer(
  target: HTMLElement | Window,
  type: string,
  init: PointerInit = {},
) {
  const evt = new MouseEvent(type, {
    bubbles: init.bubbles ?? true,
    cancelable: true,
    clientX: init.clientX ?? 0,
    clientY: init.clientY ?? 0,
    button: init.button ?? 0,
  });
  Object.defineProperty(evt, "pointerId", {
    value: init.pointerId ?? 1,
    configurable: true,
  });
  Object.defineProperty(evt, "pointerType", {
    value: init.pointerType ?? "mouse",
    configurable: true,
  });
  act(() => {
    target.dispatchEvent(evt);
  });
  return evt;
}

function renderTimeline() {
  const onSeek = vi.fn();
  const onToggleUnit = vi.fn();
  const onSelectTag = vi.fn();
  const { container } = render(
    <MultiTrackTimeline
      workspace={WORKSPACE}
      currentTime={635}
      selectedUnitIds={new Set()}
      selectedTagId={null}
      onSeek={onSeek}
      onToggleUnit={onToggleUnit}
      onSelectTag={onSelectTag}
    />,
  );
  const scroller = container.querySelector<HTMLElement>(
    ".ag-timeline__scroller",
  )!;
  const tagButton = screen.getByRole("button", {
    name: /编辑标签 objection\.price: high/,
  });
  return { container, scroller, tagButton, onSeek, onSelectTag, onToggleUnit };
}

describe("MultiTrackTimeline 拖动平移（useDragPan）", () => {
  it("浏览器缺少 Pointer Capture API 时仍可拖动且不会抛异常", () => {
    const proto = HTMLElement.prototype as Partial<PointerCaptureMethods>;
    const original = {
      setPointerCapture: proto.setPointerCapture,
      hasPointerCapture: proto.hasPointerCapture,
      releasePointerCapture: proto.releasePointerCapture,
    };
    proto.setPointerCapture = undefined;
    proto.hasPointerCapture = undefined;
    proto.releasePointerCapture = undefined;
    try {
      const { scroller } = renderTimeline();
      scroller.scrollLeft = 200;
      expect(() => {
        firePointer(scroller, "pointerdown", {
          clientX: 100,
          clientY: 50,
        });
        firePointer(window, "pointermove", {
          clientX: 120,
          clientY: 50,
        });
        firePointer(window, "pointerup", {
          clientX: 120,
          clientY: 50,
        });
      }).not.toThrow();
      expect(scroller.scrollLeft).toBe(180);
    } finally {
      proto.setPointerCapture = original.setPointerCapture;
      proto.hasPointerCapture = original.hasPointerCapture;
      proto.releasePointerCapture = original.releasePointerCapture;
    }
  });

  it("纯点击（无位移）应正常触发标签选中", () => {
    const { scroller, tagButton, onSeek, onSelectTag } = renderTimeline();
    firePointer(scroller, "pointerdown", { clientX: 100, clientY: 50 });
    firePointer(window, "pointerup", { clientX: 100, clientY: 50 });
    act(() => {
      tagButton.click();
    });
    expect(onSeek).toHaveBeenCalledWith(630);
    expect(onSelectTag).toHaveBeenCalledTimes(1);
  });

  it("位移 4px（< 阈值 5）仍应视为点击并触发标签", () => {
    const { scroller, tagButton, onSeek, onSelectTag } = renderTimeline();
    firePointer(scroller, "pointerdown", { clientX: 100, clientY: 50 });
    firePointer(window, "pointermove", { clientX: 103, clientY: 54 });
    firePointer(window, "pointerup", { clientX: 103, clientY: 54 });
    act(() => {
      tagButton.click();
    });
    expect(onSeek).toHaveBeenCalledWith(630);
    expect(onSelectTag).toHaveBeenCalledTimes(1);
  });

  it("位移 5px（= 阈值）应判为拖动并抑制随后的标签点击", () => {
    const { scroller, tagButton, onSeek, onSelectTag } = renderTimeline();
    firePointer(scroller, "pointerdown", { clientX: 100, clientY: 50 });
    firePointer(window, "pointermove", { clientX: 105, clientY: 50 });
    firePointer(window, "pointerup", { clientX: 105, clientY: 50 });
    act(() => {
      tagButton.click();
    });
    expect(onSeek).not.toHaveBeenCalled();
    expect(onSelectTag).not.toHaveBeenCalled();
  });

  it("位移 6px（> 阈值）应判为拖动并抑制随后的标签点击", () => {
    const { scroller, tagButton, onSeek, onSelectTag } = renderTimeline();
    firePointer(scroller, "pointerdown", { clientX: 100, clientY: 50 });
    firePointer(window, "pointermove", { clientX: 106, clientY: 50 });
    firePointer(window, "pointerup", { clientX: 106, clientY: 50 });
    act(() => {
      tagButton.click();
    });
    expect(onSeek).not.toHaveBeenCalled();
    expect(onSelectTag).not.toHaveBeenCalled();
  });

  it("纯纵向位移 6px 也应判为拖动（Math.max(|dx|,|dy|)）", () => {
    const { scroller, tagButton, onSeek } = renderTimeline();
    firePointer(scroller, "pointerdown", { clientX: 100, clientY: 50 });
    firePointer(window, "pointermove", { clientX: 100, clientY: 56 });
    firePointer(window, "pointerup", { clientX: 100, clientY: 56 });
    act(() => {
      tagButton.click();
    });
    expect(onSeek).not.toHaveBeenCalled();
  });

  it("横向拖动应平移 scrollLeft（指针右移 → scrollLeft 减小）", () => {
    const { scroller } = renderTimeline();
    scroller.scrollLeft = 200;
    firePointer(scroller, "pointerdown", { clientX: 100, clientY: 50 });
    firePointer(window, "pointermove", { clientX: 140, clientY: 50 });
    // deltaX = 40 → scrollLeft = 200 - 40 = 160
    expect(scroller.scrollLeft).toBe(160);
  });

  it("右键 pointerdown（mouse + button=2）不应进入拖动", () => {
    const { scroller, tagButton, onSeek } = renderTimeline();
    scroller.scrollLeft = 200;
    firePointer(scroller, "pointerdown", {
      clientX: 100,
      clientY: 50,
      button: 2,
      pointerType: "mouse",
    });
    firePointer(window, "pointermove", { clientX: 200, clientY: 50 });
    // 未进入拖动：scrollLeft 不应变化
    expect(scroller.scrollLeft).toBe(200);
    firePointer(window, "pointerup", { clientX: 200, clientY: 50 });
    act(() => {
      tagButton.click();
    });
    // 右键未占用拖动状态，点击应正常触发
    expect(onSeek).toHaveBeenCalledWith(630);
  });

  it("拖动中应挂载 is-dragging class，结束后移除", () => {
    const { scroller } = renderTimeline();
    expect(scroller.classList.contains("is-dragging")).toBe(false);
    firePointer(scroller, "pointerdown", { clientX: 100, clientY: 50 });
    firePointer(window, "pointermove", { clientX: 120, clientY: 50 });
    expect(scroller.classList.contains("is-dragging")).toBe(true);
    firePointer(window, "pointerup", { clientX: 120, clientY: 50 });
    expect(scroller.classList.contains("is-dragging")).toBe(false);
  });

  it("pointercancel 应清理拖动状态（系统中断后 move 不再生效）", () => {
    const { scroller } = renderTimeline();
    scroller.scrollLeft = 200;
    firePointer(scroller, "pointerdown", { clientX: 100, clientY: 50 });
    firePointer(window, "pointermove", { clientX: 130, clientY: 50 });
    expect(scroller.scrollLeft).toBe(170);
    firePointer(window, "pointercancel", { clientX: 130, clientY: 50 });
    expect(scroller.classList.contains("is-dragging")).toBe(false);
    // cancel 后再 move 不应继续平移
    firePointer(window, "pointermove", { clientX: 200, clientY: 50 });
    expect(scroller.scrollLeft).toBe(170);
  });

  it("第二个指针应被忽略（多指场景不覆盖首个拖动）", () => {
    const { scroller } = renderTimeline();
    scroller.scrollLeft = 200;
    firePointer(scroller, "pointerdown", {
      clientX: 100,
      clientY: 50,
      pointerId: 1,
    });
    // 第二指 down（pointerId=2）—— 应被忽略
    firePointer(scroller, "pointerdown", {
      clientX: 500,
      clientY: 50,
      pointerId: 2,
    });
    // 第二指 move —— 因 pointerId 不匹配，应被忽略，scrollLeft 不受影响
    firePointer(window, "pointermove", {
      clientX: 520,
      clientY: 50,
      pointerId: 2,
    });
    expect(scroller.scrollLeft).toBe(200);
    // 首指 move 仍应生效
    firePointer(window, "pointermove", {
      clientX: 130,
      clientY: 50,
      pointerId: 1,
    });
    expect(scroller.scrollLeft).toBe(170);
  });

  it("未达阈值的 move 不应改变 scrollLeft", () => {
    const { scroller } = renderTimeline();
    scroller.scrollLeft = 200;
    firePointer(scroller, "pointerdown", { clientX: 100, clientY: 50 });
    firePointer(window, "pointermove", { clientX: 102, clientY: 50 });
    expect(scroller.scrollLeft).toBe(200);
  });

  it("wasDragging 标志在一次拖动后会在下一事件循环清零，不影响后续点击", async () => {
    const { scroller, tagButton, onSeek } = renderTimeline();
    firePointer(scroller, "pointerdown", { clientX: 100, clientY: 50 });
    firePointer(window, "pointermove", { clientX: 120, clientY: 50 });
    firePointer(window, "pointerup", { clientX: 120, clientY: 50 });
    // 等待 setTimeout(0) 清零 wasDraggingRef
    await new Promise((r) => setTimeout(r, 10));
    act(() => {
      tagButton.click();
    });
    expect(onSeek).toHaveBeenCalledWith(630);
  });

  it("波形点击在拖动结束后同样应被抑制", () => {
    const { container, scroller, onSeek } = renderTimeline();
    const wave = container.querySelector<HTMLButtonElement>(
      ".ag-wave-placeholder__bars",
    );
    expect(wave).not.toBeNull();
    firePointer(scroller, "pointerdown", { clientX: 100, clientY: 50 });
    firePointer(window, "pointermove", { clientX: 120, clientY: 50 });
    firePointer(window, "pointerup", { clientX: 120, clientY: 50 });
    act(() => {
      wave!.click();
    });
    expect(onSeek).not.toHaveBeenCalled();
  });
});

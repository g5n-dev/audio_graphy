import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { FloatingSubtitle, type SubtitleLine } from "./FloatingSubtitle";

const LINES: SubtitleLine[] = [
  { atSec: 0, speaker: "顾问小林", role: "agent", text: "您好，欢迎光临。" },
  { atSec: 38, speaker: "客户", role: "customer", text: "想给家人挑生日礼物。" },
  { atSec: 92, speaker: "顾问小林", role: "agent", text: "预算方便说一下吗？" },
];

/** 让锚点「滚出视口」:组件用 IntersectionObserver 判定,测试里直接驱动它。
 *  回调来自浏览器 API,React 不知情——必须包 act 才能刷到 DOM。 */
let rawNotify: ((entries: { isIntersecting: boolean }[]) => void) | null = null;

function notify(intersecting: boolean): void {
  act(() => rawNotify?.([{ isIntersecting: intersecting }]));
}

beforeEach(() => {
  rawNotify = null;
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      constructor(callback: (entries: { isIntersecting: boolean }[]) => void) {
        rawNotify = callback;
      }
      observe() {}
      disconnect() {}
    },
  );
});

function renderBar(props: Partial<Parameters<typeof FloatingSubtitle>[0]> = {}) {
  const anchorRef = createRef<HTMLDivElement>();
  const result = render(
    <>
      <div ref={anchorRef}>转写</div>
      <FloatingSubtitle
        lines={LINES}
        currentSec={40}
        durationSec={120}
        playing={false}
        anchorRef={anchorRef}
        onSeek={vi.fn()}
        onTogglePlay={vi.fn()}
        onBackToTranscript={vi.fn()}
        {...props}
      />
    </>,
  );
  return result;
}

describe("FloatingSubtitle", () => {
  it("stays hidden while the transcript is on screen", () => {
    renderBar();
    notify(true);
    expect(
      screen.queryByRole("complementary", { name: "播放中字幕" }),
    ).not.toBeInTheDocument();
  });

  it("surfaces the line at the current time with its neighbours", () => {
    renderBar();
    notify(false);

    const bar = screen.getByRole("complementary", { name: "播放中字幕" });
    // 40s 落在第二句(38s)与第三句(92s)之间——当前句必须是第二句。
    expect(bar).toHaveTextContent("想给家人挑生日礼物。");
    expect(bar).toHaveTextContent("顾问小林：您好，欢迎光临。");
    expect(bar).toHaveTextContent("顾问小林：预算方便说一下吗？");
    expect(screen.getByRole("slider", { name: "播放进度" })).toHaveAttribute(
      "aria-valuenow",
      "40",
    );
  });

  it("seeks with the keyboard so the bar is not pointer-only", async () => {
    const onSeek = vi.fn();
    const user = userEvent.setup();
    renderBar({ onSeek });
    notify(false);

    const slider = screen.getByRole("slider", { name: "播放进度" });
    slider.focus();
    await user.keyboard("{ArrowRight}");
    expect(onSeek).toHaveBeenCalledWith(45);
    await user.keyboard("{ArrowLeft}");
    expect(onSeek).toHaveBeenLastCalledWith(35);
  });

  it("can be dismissed and does not come back on its own", async () => {
    const user = userEvent.setup();
    renderBar();
    notify(false);

    await user.click(screen.getByRole("button", { name: "关闭悬浮字幕" }));
    expect(
      screen.queryByRole("complementary", { name: "播放中字幕" }),
    ).not.toBeInTheDocument();

    // 再次滚动不该把用户关掉的条塞回来。
    notify(true);
    notify(false);
    expect(
      screen.queryByRole("complementary", { name: "播放中字幕" }),
    ).not.toBeInTheDocument();
  });

  it("renders nothing when the reception has no transcript", () => {
    renderBar({ lines: [] });
    notify(false);
    expect(
      screen.queryByRole("complementary", { name: "播放中字幕" }),
    ).not.toBeInTheDocument();
  });
});

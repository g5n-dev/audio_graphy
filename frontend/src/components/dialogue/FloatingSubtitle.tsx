/**
 * 悬浮字幕(迷你播放器)—— 调听时转写卡片滚出视口后出现。
 *
 * 调听台很长:时间轴、切分编辑、标签证据都在转写上方,操作员一边听一边
 * 往下翻就看不到「现在说到哪句」了。这个条固定在视口底部,给出前一句/
 * 当前句/后一句、进度与跳转,并能一键回到转写。
 *
 * 与设计原型(ui 2)的一处必要分歧:原型的进度条是纯展示,这里的 seek 必须
 * 落到真实播放器上——所以时间与播放态由父级的 <audio> 持有,本组件只报告
 * 意图,不自己维护一份会和音频漂移的时钟。
 */

import { useEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import "./FloatingSubtitle.css";

export interface SubtitleLine {
  atSec: number;
  speaker: string;
  role: "agent" | "customer" | "unknown";
  text: string;
}

function formatClock(sec: number): string {
  const safe = Number.isFinite(sec) && sec > 0 ? sec : 0;
  const minutes = Math.floor(safe / 60);
  const seconds = Math.floor(safe % 60);
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

export function FloatingSubtitle({
  lines,
  currentSec,
  durationSec,
  playing,
  anchorRef,
  onSeek,
  onTogglePlay,
  onBackToTranscript,
}: {
  lines: SubtitleLine[];
  currentSec: number;
  durationSec: number;
  playing: boolean;
  /** 转写容器:它滚出视口才显示本条。 */
  anchorRef: RefObject<HTMLElement | null>;
  onSeek: (sec: number) => void;
  onTogglePlay: () => void;
  onBackToTranscript: () => void;
}) {
  const [anchorOffscreen, setAnchorOffscreen] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const trackRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = anchorRef.current;
    // 没有 IntersectionObserver 就不显示这条:它是纯增强,缺了不能让
    // 整个调听台崩掉(测试环境与老浏览器都会走到这里)。
    if (!element || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => setAnchorOffscreen(!entries[0]?.isIntersecting),
      { threshold: 0.08 },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [anchorRef]);

  if (!anchorOffscreen || dismissed || lines.length === 0) return null;

  const index = Math.max(
    0,
    lines.findIndex((line, position) => {
      const next = lines[position + 1];
      return currentSec >= line.atSec && (!next || currentSec < next.atSec);
    }),
  );
  const previous = lines[index - 1];
  const current = lines[index];
  const next = lines[index + 1];
  const ratio = durationSec > 0 ? Math.min(1, currentSec / durationSec) : 0;

  const seekFromPointer = (clientX: number) => {
    const rect = trackRef.current?.getBoundingClientRect();
    if (!rect || rect.width === 0 || durationSec <= 0) return;
    const position = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    onSeek(position * durationSec);
  };

  return (
    <div
      className="ag-floating-subtitle"
      role="complementary"
      aria-label="播放中字幕"
    >
      <div
        ref={trackRef}
        className="ag-floating-subtitle__track"
        role="slider"
        tabIndex={0}
        aria-label="播放进度"
        aria-valuemin={0}
        aria-valuemax={Math.round(durationSec)}
        aria-valuenow={Math.round(currentSec)}
        aria-valuetext={`${formatClock(currentSec)} / ${formatClock(durationSec)}`}
        onPointerDown={(event) => seekFromPointer(event.clientX)}
        onKeyDown={(event) => {
          // 键盘也要能定位:方向键 ±5 秒,与音频元素的常规步进一致。
          if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
            event.preventDefault();
            const delta = event.key === "ArrowLeft" ? -5 : 5;
            onSeek(Math.max(0, Math.min(durationSec, currentSec + delta)));
          }
        }}
      >
        <span
          className="ag-floating-subtitle__fill"
          style={{ width: `${ratio * 100}%` }}
        />
        <span
          className="ag-floating-subtitle__head"
          style={{ left: `${ratio * 100}%` }}
        />
      </div>
      <div className="ag-floating-subtitle__row">
        <button
          type="button"
          className="ag-floating-subtitle__play"
          onClick={onTogglePlay}
          aria-label={playing ? "暂停" : "播放"}
        >
          {playing ? "❚❚" : "▶"}
        </button>
        <div className="ag-floating-subtitle__lines">
          <small data-dim>
            {previous ? `${previous.speaker}：${previous.text}` : "已到首句"}
          </small>
          <div className="ag-floating-subtitle__current">
            <strong data-role={current?.role}>{current?.speaker}</strong>
            <span>{current?.text}</span>
            <time>
              {formatClock(currentSec)} / {formatClock(durationSec)}
            </time>
          </div>
          <small data-dim>
            {next ? `${next.speaker}：${next.text}` : "已到末句"}
          </small>
        </div>
        <button type="button" onClick={onBackToTranscript}>
          回到转写
        </button>
        <button
          type="button"
          aria-label="关闭悬浮字幕"
          onClick={() => setDismissed(true)}
        >
          ✕
        </button>
      </div>
    </div>
  );
}

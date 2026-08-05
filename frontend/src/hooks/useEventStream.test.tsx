import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useEventStream, type DomainEventFrame } from "./useEventStream";
import { useAuthStore } from "@/stores/auth";

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

function Harness({ onEvent }: { onEvent: (event: DomainEventFrame) => void }) {
  useEventStream(["recording.indexed"], onEvent);
  return null;
}

const FRAME_7 =
  'id: 7\nevent: recording.indexed\ndata: {"id":7,"event_type":"recording.indexed","aggregate_type":"recording","aggregate_id":"11","payload":{"recording_id":11},"occurred_at":null}\n\n';

describe("useEventStream", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    useAuthStore.setState({ token: null });
  });

  it("parses SSE frames across chunk boundaries and skips heartbeats", async () => {
    useAuthStore.setState({ token: "jwt-token" });
    const events: DomainEventFrame[] = [];
    // 帧被切在任意字节边界是流式读取的常态,解析必须跨 chunk 缓冲。
    const half = FRAME_7.slice(0, 40);
    const rest = FRAME_7.slice(40);
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(streamOf([": connected\n\n", half, rest, ": ping\n\n"]), {
        status: 200,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<Harness onEvent={(event) => events.push(event)} />);
    await vi.waitFor(() => expect(events).toHaveLength(1));

    expect(events[0].id).toBe(7);
    expect(events[0].payload.recording_id).toBe(11);
    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain("types=recording.indexed");
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe(
      "Bearer jwt-token",
    );
  });

  it("does not connect without a token", () => {
    useAuthStore.setState({ token: null });
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<Harness onEvent={() => {}} />);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("reconnects from the last seen cursor", async () => {
    vi.useFakeTimers();
    try {
      useAuthStore.setState({ token: "jwt-token" });
      const fetchMock = vi
        .fn()
        .mockResolvedValueOnce(
          new Response(streamOf([FRAME_7]), { status: 200 }),
        )
        // 第二条连接挂住不结束,避免测试期间无限循环。
        .mockImplementation(
          () =>
            new Promise(() => {
              /* 挂起 */
            }),
        );
      vi.stubGlobal("fetch", fetchMock);

      render(<Harness onEvent={() => {}} />);
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
      await vi.advanceTimersByTimeAsync(5_100);

      expect(fetchMock).toHaveBeenCalledTimes(2);
      // 断线不丢事件的机制:带上游标,服务端从 7 之后重放。
      expect(String(fetchMock.mock.calls[1][0])).toContain("after=7");
    } finally {
      vi.useRealTimers();
    }
  });
});

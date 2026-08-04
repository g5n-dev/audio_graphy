import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LiveAudioCapturePanel } from "./LiveAudioCapturePanel";
import type { ReceptionRecordingItem } from "@/types/api";

vi.mock("@/api/services", () => ({
  createStreamingTicket: vi.fn(),
}));

vi.mock("./liveCapture", async (importOriginal) => {
  const original = await importOriginal<typeof import("./liveCapture")>();
  return {
    ...original,
    startPcmCapture: vi.fn(),
  };
});

import { createStreamingTicket } from "@/api/services";
import { startPcmCapture } from "./liveCapture";

const mockedCreateTicket = createStreamingTicket as unknown as ReturnType<
  typeof vi.fn
>;
const mockedStartCapture = startPcmCapture as unknown as ReturnType<
  typeof vi.fn
>;

const RECORDINGS: ReceptionRecordingItem[] = [
  {
    id: 101,
    mapping_id: 88,
    recording_id: 101,
    name: "实时接待.wav",
    sequence_no: 0,
    timeline_start_sec: 0,
    timeline_end_sec: 60,
    source_start_sec: 0,
    source_end_sec: 60,
    source_start_ms: 0,
    source_end_ms: 60_000,
    timeline_start_ms: 0,
    timeline_end_ms: 60_000,
    gap_before_ms: 0,
    time_origin_ms: 0,
    legal_source_start_ms: 0,
    legal_source_end_ms: 60_000,
    gap_before_sec: 0,
    audio_url: null,
    playback_expires_at: null,
    decision_source: "explicit",
    merge_confidence: 1,
  },
];

class FakeWebSocket {
  static readonly OPEN = 1;
  static instances: FakeWebSocket[] = [];
  readonly url: string;
  readyState = FakeWebSocket.OPEN;
  bufferedAmount = 0;
  sent: Array<string | ArrayBuffer> = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(payload: string | ArrayBuffer) {
    this.sent.push(payload);
  }

  close() {
    this.readyState = 3;
    this.onclose?.();
  }

  open() {
    this.onopen?.();
  }

  message(payload: Record<string, unknown>) {
    this.onmessage?.({
      data: JSON.stringify(payload),
    } as MessageEvent<string>);
  }
}

describe("LiveAudioCapturePanel", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket);
    mockedCreateTicket.mockReset();
    mockedStartCapture.mockReset();
    mockedCreateTicket.mockResolvedValue({
      ticket: "one-time",
      expires_at: "2026-07-23T01:00:30Z",
      ws_url: "wss://audio.example/ws/stream?ticket=one-time",
    });
    mockedStartCapture.mockResolvedValue({
      stop: vi.fn(),
    });
  });

  it("uses a one-time ticket, streams sequenced PCM, and distinguishes temporary from confirmed text", async () => {
    const user = userEvent.setup();
    let emitPcm: ((pcm: Int16Array) => void) | undefined;
    mockedStartCapture.mockImplementation(
      async (onPcm: (pcm: Int16Array) => void) => {
        emitPcm = onPcm;
        return { stop: vi.fn() };
      },
    );
    const onCommitted = vi.fn();
    render(
      <LiveAudioCapturePanel recordings={RECORDINGS} onCommitted={onCommitted} />,
    );

    await user.type(
      screen.getByLabelText("实时采集同意凭证"),
      "consent-proof",
    );
    await user.click(screen.getByRole("button", { name: "开始实时采集" }));

    await waitFor(() => {
      expect(mockedCreateTicket).toHaveBeenCalledWith({
        recording_id: 101,
        consent_token: "consent-proof",
      });
    });
    const socket = FakeWebSocket.instances[0];
    expect(socket.url).toBe(
      "wss://audio.example/ws/stream?ticket=one-time",
    );
    expect(socket.url).not.toContain("token=");
    act(() => socket.open());
    const init = JSON.parse(String(socket.sent[0])) as Record<string, unknown>;
    expect(init).toMatchObject({
      type: "init",
      recording_id: 101,
      consent_token: "consent-proof",
    });
    expect(init.session_id).toEqual(expect.any(String));
    expect(init).not.toHaveProperty("resume_token");

    act(() => socket.message({ type: "session_opened" }));
    await waitFor(() => {
      expect(mockedStartCapture).toHaveBeenCalledTimes(1);
      expect(screen.getByText("正在采集")).toBeInTheDocument();
    });
    act(() => {
      emitPcm?.(new Int16Array([1, 2]));
    });
    const frame = socket.sent.find(
      (payload): payload is ArrayBuffer => payload instanceof ArrayBuffer,
    );
    expect(frame).toBeDefined();
    expect(new DataView(frame!).getUint32(0, false)).toBe(0);
    act(() => {
      socket.message({
        type: "frame_ack",
        seq: 0,
        duplicate: false,
      });
    });
    expect(
      screen.getByText(/ACK 水位 #0 · 待确认 0\/256 帧/),
    ).toBeInTheDocument();

    act(() => {
      socket.message({ type: "realtime_text", text: "临时字幕" });
    });
    expect(screen.getByText("临时字幕")).toBeInTheDocument();
    act(() => {
      socket.message({
        type: "segment_confirmed",
        segment_id: 77,
        text: "持久字幕",
        durable: true,
      });
    });
    expect(screen.queryByText("临时字幕")).not.toBeInTheDocument();
    expect(screen.getByText("持久字幕")).toBeInTheDocument();
    expect(screen.getByText("已持久化 #77")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "完成并提交" }));
    expect(socket.sent).toContain(JSON.stringify({ type: "finalize" }));
    act(() => socket.message({ type: "session_closed" }));
    expect(await screen.findByText("采集已提交")).toBeInTheDocument();
    // 落库发生在这一刻。面板拿不到工作台的数据，不回调外层就无从刷新，
    // 页面会在「已完成持久化确认」旁边继续画采集前的转写。
    expect(onCommitted).toHaveBeenCalledTimes(1);
  });

  it("stops a microphone handle that resolves after the session has already closed", async () => {
    const user = userEvent.setup();
    const stop = vi.fn();
    let resolveCapture:
      | ((capture: { stop: () => Promise<void> }) => void)
      | undefined;
    mockedStartCapture.mockReturnValue(
      new Promise((resolve) => {
        resolveCapture = resolve;
      }),
    );
    render(<LiveAudioCapturePanel recordings={RECORDINGS} />);

    await user.type(
      screen.getByLabelText("实时采集同意凭证"),
      "consent-proof",
    );
    await user.click(screen.getByRole("button", { name: "开始实时采集" }));
    const socket = FakeWebSocket.instances[0];
    act(() => {
      socket.open();
      socket.message({ type: "session_opened" });
    });
    await screen.findByText("等待麦克风授权");
    act(() => {
      socket.message({ type: "session_closed" });
    });
    expect(await screen.findByText("采集已提交")).toBeInTheDocument();
    await act(async () => {
      resolveCapture?.({ stop });
      await Promise.resolve();
    });

    expect(stop).toHaveBeenCalledTimes(1);
  });

  it("ignores callbacks from a superseded WebSocket connection", async () => {
    const user = userEvent.setup();
    render(<LiveAudioCapturePanel recordings={RECORDINGS} />);

    await user.type(
      screen.getByLabelText("实时采集同意凭证"),
      "first-proof",
    );
    await user.click(screen.getByRole("button", { name: "开始实时采集" }));
    const firstSocket = FakeWebSocket.instances[0];
    act(() => {
      firstSocket.open();
      firstSocket.message({ type: "session_opened" });
      firstSocket.message({
        type: "error",
        recoverable: false,
        message: "first failed",
      });
    });
    expect(await screen.findByText("采集失败")).toBeInTheDocument();

    await user.type(
      screen.getByLabelText("实时采集同意凭证"),
      "second-proof",
    );
    await user.click(screen.getByRole("button", { name: "开始实时采集" }));
    const secondSocket = FakeWebSocket.instances[1];
    act(() => {
      secondSocket.open();
      secondSocket.message({ type: "session_opened" });
    });
    expect(await screen.findByText("正在采集")).toBeInTheDocument();

    act(() => {
      firstSocket.onclose?.();
    });
    expect(screen.getByText("正在采集")).toBeInTheDocument();
    expect(screen.queryByText("采集失败")).not.toBeInTheDocument();
  });

  it("reuses the session id and replays only unacknowledged frames after reconnecting", async () => {
    const user = userEvent.setup();
    let emitPcm: ((pcm: Int16Array) => void) | undefined;
    mockedStartCapture.mockImplementation(
      async (onPcm: (pcm: Int16Array) => void) => {
        emitPcm = onPcm;
        return { stop: vi.fn() };
      },
    );
    render(<LiveAudioCapturePanel recordings={RECORDINGS} />);

    await user.type(
      screen.getByLabelText("实时采集同意凭证"),
      "resume-proof",
    );
    await user.click(screen.getByRole("button", { name: "开始实时采集" }));
    const firstSocket = FakeWebSocket.instances[0];
    act(() => {
      firstSocket.open();
      firstSocket.message({
        type: "session_opened",
        resume_token: "opaque/resume:token-1",
      });
    });
    await waitFor(() => expect(emitPcm).toBeDefined());
    act(() => {
      emitPcm?.(new Int16Array([1]));
      firstSocket.message({ type: "frame_ack", seq: 0, duplicate: false });
      emitPcm?.(new Int16Array([2]));
    });
    const firstInit = JSON.parse(
      String(firstSocket.sent[0]),
    ) as Record<string, unknown>;

    act(() => firstSocket.close());
    expect(await screen.findByText("连接中断，正在续传")).toBeInTheDocument();
    await waitFor(() => {
      expect(mockedCreateTicket).toHaveBeenCalledTimes(2);
      expect(FakeWebSocket.instances).toHaveLength(2);
    });

    const secondSocket = FakeWebSocket.instances[1];
    act(() => {
      secondSocket.open();
    });
    const secondInit = JSON.parse(
      String(secondSocket.sent[0]),
    ) as Record<string, unknown>;
    expect(secondInit.session_id).toBe(firstInit.session_id);
    expect(secondInit.resume_from_seq).toBe(1);
    expect(secondInit.resume_token).toBe("opaque/resume:token-1");
    expect(firstInit).not.toHaveProperty("resume_token");
    expect(mockedCreateTicket).toHaveBeenNthCalledWith(2, {
      recording_id: 101,
      consent_token: "resume-proof",
    });
    act(() => {
      secondSocket.message({
        type: "session_opened",
        resume_token: "opaque/resume:token-2",
      });
    });
    const replayedSequences = secondSocket.sent
      .filter(
        (payload): payload is ArrayBuffer =>
          payload instanceof ArrayBuffer,
      )
      .map((frame) => new DataView(frame).getUint32(0, false));
    expect(replayedSequences).toEqual([1]);

    act(() => {
      emitPcm?.(new Int16Array([3]));
    });
    const sentSequences = secondSocket.sent
      .filter(
        (payload): payload is ArrayBuffer =>
          payload instanceof ArrayBuffer,
      )
      .map((frame) => new DataView(frame).getUint32(0, false));
    expect(sentSequences).toEqual([1, 2]);
    expect(mockedStartCapture).toHaveBeenCalledTimes(1);
    expect(screen.getByText("正在采集")).toBeInTheDocument();
  });

  it(
    "stops after three reconnect attempts even when each transport briefly reopens",
    async () => {
      const user = userEvent.setup();
      const stop = vi.fn().mockResolvedValue(undefined);
      mockedStartCapture.mockResolvedValue({ stop });
      render(<LiveAudioCapturePanel recordings={RECORDINGS} />);

      await user.type(
        screen.getByLabelText("实时采集同意凭证"),
        "bounded-reconnect-proof",
      );
      await user.click(screen.getByRole("button", { name: "开始实时采集" }));

      const openSessionThenClose = async (socketIndex: number) => {
        const socket = FakeWebSocket.instances[socketIndex];
        act(() => {
          socket.open();
          socket.message({
            type: "session_opened",
            resume_token: `bounded-resume-${socketIndex}`,
          });
          socket.close();
        });
        if (socketIndex < 3) {
          await waitFor(
            () => {
              expect(FakeWebSocket.instances).toHaveLength(socketIndex + 2);
            },
            { timeout: 1_500 },
          );
        }
      };

      await openSessionThenClose(0);
      await openSessionThenClose(1);
      await openSessionThenClose(2);
      await openSessionThenClose(3);

      expect(await screen.findByText("采集失败")).toBeInTheDocument();
      expect(screen.getByRole("alert")).toHaveTextContent(
        "连续恢复 3 次仍失败",
      );
      await waitFor(() => expect(stop).toHaveBeenCalledTimes(1));
      expect(FakeWebSocket.instances).toHaveLength(4);
    },
    5_000,
  );

  it("treats an out-of-order server response as a terminal consistency failure", async () => {
    const user = userEvent.setup();
    const stop = vi.fn().mockResolvedValue(undefined);
    mockedStartCapture.mockResolvedValue({ stop });
    render(<LiveAudioCapturePanel recordings={RECORDINGS} />);

    await user.type(
      screen.getByLabelText("实时采集同意凭证"),
      "sequence-proof",
    );
    await user.click(screen.getByRole("button", { name: "开始实时采集" }));
    const socket = FakeWebSocket.instances[0];
    act(() => {
      socket.open();
      socket.message({ type: "session_opened" });
    });
    await screen.findByText("正在采集");
    act(() => {
      socket.message({
        type: "error",
        code: "OUT_OF_ORDER_SEQ",
        recoverable: true,
        message: "sequence below watermark",
      });
    });

    expect(await screen.findByText("采集失败")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "服务端拒绝倒序帧",
    );
    await waitFor(() => expect(stop).toHaveBeenCalledTimes(1));
  });

  it("fails explicitly instead of dropping PCM when the pending buffer is full", async () => {
    const user = userEvent.setup();
    let emitPcm: ((pcm: Int16Array) => void) | undefined;
    const stop = vi.fn().mockResolvedValue(undefined);
    mockedStartCapture.mockImplementation(
      async (onPcm: (pcm: Int16Array) => void) => {
        emitPcm = onPcm;
        return { stop };
      },
    );
    render(<LiveAudioCapturePanel recordings={RECORDINGS} />);

    await user.type(
      screen.getByLabelText("实时采集同意凭证"),
      "bounded-proof",
    );
    await user.click(screen.getByRole("button", { name: "开始实时采集" }));
    const socket = FakeWebSocket.instances[0];
    act(() => {
      socket.open();
      socket.message({ type: "session_opened" });
    });
    await waitFor(() => expect(emitPcm).toBeDefined());
    act(() => {
      for (let index = 0; index <= 256; index += 1) {
        emitPcm?.(new Int16Array([index]));
      }
    });

    expect(await screen.findByText("采集失败")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "已达到 256 帧上限",
    );
    await waitFor(() => expect(stop).toHaveBeenCalledTimes(1));
  });
});

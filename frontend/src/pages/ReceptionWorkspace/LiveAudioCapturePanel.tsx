import { useEffect, useMemo, useRef, useState } from "react";
import { createStreamingTicket } from "@/api/services";
import type {
  EntityId,
  ReceptionRecordingItem,
} from "@/types/api";
import {
  packPcmFrame,
  startPcmCapture,
  type PcmCaptureHandle,
} from "./liveCapture";

type CapturePhase =
  | "idle"
  | "permission"
  | "requesting"
  | "connecting"
  | "recording"
  | "reconnecting"
  | "draining"
  | "committed"
  | "failed";

interface ConfirmedCaption {
  key: string;
  text: string;
  durable: boolean;
  segmentId: EntityId | null;
}

interface LiveAudioCapturePanelProps {
  recordings: ReceptionRecordingItem[];
  disabled?: boolean;
  /**
   * 服务端确认 drain 与持久化之后调用。面板自己没有工作台的数据，
   * 不通知外层的话，页面会在「已完成持久化确认」的字样旁边继续显示
   * 采集前的转写和分段，直到操作员手动刷新。
   */
  onCommitted?: () => void;
}

interface CaptureLease {
  runEpoch: number;
  handle: PcmCaptureHandle;
}

interface LiveSession {
  runEpoch: number;
  sessionId: string;
  recordingId: number;
  consentToken: string;
  resumeToken: string | null;
  reconnectAttempt: number;
}

const MAX_PENDING_FRAMES = 256;
const MAX_RECONNECT_ATTEMPTS = 3;
const RECONNECT_DELAYS_MS = [0, 250, 750] as const;
const MAX_SOCKET_BUFFER_BYTES = 1_048_576;

const PHASE_LABEL: Record<CapturePhase, string> = {
  idle: "尚未开始",
  permission: "等待麦克风授权",
  requesting: "正在申请一次性连接凭证",
  connecting: "正在建立安全连接",
  recording: "正在采集",
  reconnecting: "连接中断，正在续传",
  draining: "正在提交最后一段音频",
  committed: "采集已提交",
  failed: "采集失败",
};

function sourceRecordingId(recording: ReceptionRecordingItem): EntityId {
  return recording.recording_id ?? recording.id;
}

function createSessionId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `capture-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function websocketUrl(rawUrl: string): string {
  const url = new URL(rawUrl, window.location.origin);
  if (url.protocol === "http:") url.protocol = "ws:";
  if (url.protocol === "https:") url.protocol = "wss:";
  if (url.protocol !== "ws:" && url.protocol !== "wss:") {
    throw new Error("服务端返回了无效的实时连接地址");
  }
  if (url.searchParams.has("token")) {
    throw new Error("拒绝把长期访问令牌放入 WebSocket URL");
  }
  if (!url.searchParams.has("ticket")) {
    throw new Error("实时连接地址缺少一次性 ticket");
  }
  return url.toString();
}

function eventText(payload: Record<string, unknown>): string {
  if (typeof payload.transcript === "string") return payload.transcript;
  if (typeof payload.text === "string") return payload.text;
  return "";
}

function integerWatermark(value: unknown): number | null {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : null;
}

export function LiveAudioCapturePanel({
  recordings,
  disabled = false,
  onCommitted,
}: LiveAudioCapturePanelProps) {
  // socket 的 handler 在连接建立时捕获闭包，走 ref 才不会调到旧的回调。
  const onCommittedRef = useRef(onCommitted);
  onCommittedRef.current = onCommitted;
  const [phase, setPhase] = useState<CapturePhase>("idle");
  const phaseRef = useRef<CapturePhase>("idle");
  const [recordingId, setRecordingId] = useState(
    recordings[0] ? String(sourceRecordingId(recordings[0])) : "",
  );
  const [consentToken, setConsentToken] = useState("");
  const [statusDetail, setStatusDetail] = useState<string | null>(null);
  const [temporaryCaption, setTemporaryCaption] = useState("");
  const [confirmedCaptions, setConfirmedCaptions] = useState<
    ConfirmedCaption[]
  >([]);
  const [ackHighWatermark, setAckHighWatermark] = useState(-1);
  const [pendingFrameCount, setPendingFrameCount] = useState(0);

  const socketRef = useRef<WebSocket | null>(null);
  const protocolReadyRef = useRef(false);
  const captureRef = useRef<CaptureLease | null>(null);
  const captureStartingEpochRef = useRef<number | null>(null);
  const runEpochRef = useRef(0);
  const sequenceRef = useRef(0);
  const ackHighWatermarkRef = useRef(-1);
  const pendingFramesRef = useRef<Map<number, ArrayBuffer>>(new Map());
  const liveSessionRef = useRef<LiveSession | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const reconnectScheduledRef = useRef(false);
  const finalizeRequestedRef = useRef(false);

  const selectedRecording = useMemo(
    () =>
      recordings.find(
        (recording) => String(sourceRecordingId(recording)) === recordingId,
      ) ?? null,
    [recordingId, recordings],
  );

  const transition = (next: CapturePhase) => {
    phaseRef.current = next;
    setPhase(next);
  };

  const clearReconnectSchedule = () => {
    reconnectScheduledRef.current = false;
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  };

  const stopMedia = async (runEpoch?: number) => {
    const capture = captureRef.current;
    if (
      capture &&
      runEpoch !== undefined &&
      capture.runEpoch !== runEpoch
    ) {
      return;
    }
    captureRef.current = null;
    if (capture) await capture.handle.stop();
  };

  const clearPendingFrames = () => {
    pendingFramesRef.current.clear();
    setPendingFrameCount(0);
  };

  const acknowledgeFrames = (
    watermark: number,
    runEpoch: number,
  ): boolean => {
    if (runEpochRef.current !== runEpoch) return false;
    if (watermark >= sequenceRef.current) {
      return false;
    }
    const nextWatermark = Math.max(
      ackHighWatermarkRef.current,
      watermark,
    );
    ackHighWatermarkRef.current = nextWatermark;
    for (const sequence of pendingFramesRef.current.keys()) {
      if (sequence <= nextWatermark) {
        pendingFramesRef.current.delete(sequence);
      }
    }
    setAckHighWatermark(nextWatermark);
    setPendingFrameCount(pendingFramesRef.current.size);
    return true;
  };

  const terminalFailure = (message: string, runEpoch: number) => {
    if (runEpochRef.current !== runEpoch) return;
    clearReconnectSchedule();
    finalizeRequestedRef.current = false;
    liveSessionRef.current = null;
    protocolReadyRef.current = false;
    transition("failed");
    setStatusDetail(message);
    clearPendingFrames();
    void stopMedia(runEpoch);
    const socket = socketRef.current;
    socketRef.current = null;
    if (socket && socket.readyState < 2) {
      socket.close();
    }
  };

  const sendPendingFrames = (
    socket: WebSocket,
    runEpoch: number,
  ): boolean => {
    if (
      runEpochRef.current !== runEpoch ||
      socketRef.current !== socket ||
      socket.readyState !== WebSocket.OPEN
    ) {
      return false;
    }
    const pending = [...pendingFramesRef.current.entries()].sort(
      ([left], [right]) => left - right,
    );
    for (const [, frame] of pending) {
      if (socket.bufferedAmount > MAX_SOCKET_BUFFER_BYTES) {
        setStatusDetail(
          "续传发送队列超过 1 MiB，正在更换连接以保持帧序连续",
        );
        socket.close();
        return false;
      }
      socket.send(frame);
    }
    return true;
  };

  const enqueuePcm = (
    pcm: Int16Array,
    runEpoch: number,
  ) => {
    if (
      runEpochRef.current !== runEpoch ||
      !["permission", "recording", "reconnecting"].includes(
        phaseRef.current,
      )
    ) {
      return;
    }
    if (pendingFramesRef.current.size >= MAX_PENDING_FRAMES) {
      terminalFailure(
        `断线待确认音频已达到 ${MAX_PENDING_FRAMES} 帧上限，已停止采集以避免静默丢帧`,
        runEpoch,
      );
      return;
    }
    const sequence = sequenceRef.current;
    const frame = packPcmFrame(sequence, pcm);
    sequenceRef.current += 1;
    pendingFramesRef.current.set(sequence, frame);
    setPendingFrameCount(pendingFramesRef.current.size);

    const socket = socketRef.current;
    if (
      phaseRef.current === "recording" &&
      protocolReadyRef.current &&
      socket?.readyState === WebSocket.OPEN
    ) {
      if (socket.bufferedAmount > MAX_SOCKET_BUFFER_BYTES) {
        transition("reconnecting");
        setStatusDetail(
          "网络发送队列超过 1 MiB，音频已进入有界续传缓冲区",
        );
        socket.close();
        return;
      }
      socket.send(frame);
    }
  };

  const ensureCapture = (runEpoch: number) => {
    if (
      captureRef.current?.runEpoch === runEpoch ||
      captureStartingEpochRef.current === runEpoch ||
      finalizeRequestedRef.current
    ) {
      return;
    }
    captureStartingEpochRef.current = runEpoch;
    void startPcmCapture((pcm) => enqueuePcm(pcm, runEpoch))
      .then(async (capture) => {
        if (captureStartingEpochRef.current === runEpoch) {
          captureStartingEpochRef.current = null;
        }
        if (
          runEpochRef.current !== runEpoch ||
          finalizeRequestedRef.current ||
          !["permission", "recording", "reconnecting"].includes(
            phaseRef.current,
          )
        ) {
          await capture.stop();
          return;
        }
        captureRef.current = { runEpoch, handle: capture };
        if (
          protocolReadyRef.current &&
          phaseRef.current === "permission"
        ) {
          transition("recording");
          setStatusDetail("16 kHz 单声道 PCM 正在安全上传");
        }
      })
      .catch((error: unknown) => {
        if (captureStartingEpochRef.current === runEpoch) {
          captureStartingEpochRef.current = null;
        }
        if (runEpochRef.current !== runEpoch) return;
        terminalFailure(
          error instanceof Error
            ? error.message
            : "无法启动麦克风采集",
          runEpoch,
        );
      });
  };

  const scheduleReconnect = (
    session: LiveSession,
    reason: string,
  ) => {
    if (
      runEpochRef.current !== session.runEpoch ||
      liveSessionRef.current !== session ||
      reconnectScheduledRef.current
    ) {
      return;
    }
    if (session.reconnectAttempt >= MAX_RECONNECT_ATTEMPTS) {
      terminalFailure(
        `实时连接连续恢复 ${MAX_RECONNECT_ATTEMPTS} 次仍失败；${pendingFramesRef.current.size} 帧未获服务端确认`,
        session.runEpoch,
      );
      return;
    }
    const attemptIndex = session.reconnectAttempt;
    session.reconnectAttempt += 1;
    reconnectScheduledRef.current = true;
    transition(
      finalizeRequestedRef.current ? "draining" : "reconnecting",
    );
    setStatusDetail(
      `${reason}；正在进行第 ${session.reconnectAttempt}/${MAX_RECONNECT_ATTEMPTS} 次安全续传`,
    );
    const reconnect = () => {
      reconnectScheduledRef.current = false;
      reconnectTimerRef.current = null;
      void openConnection(session, true);
    };
    const delay = RECONNECT_DELAYS_MS[attemptIndex] ?? 750;
    if (delay === 0) {
      queueMicrotask(reconnect);
    } else {
      reconnectTimerRef.current = window.setTimeout(reconnect, delay);
    }
  };

  const openConnection = async (
    session: LiveSession,
    reconnecting: boolean,
  ) => {
    if (
      runEpochRef.current !== session.runEpoch ||
      liveSessionRef.current !== session
    ) {
      return;
    }
    clearReconnectSchedule();
    if (!reconnecting) transition("requesting");
    try {
      const ticket = await createStreamingTicket({
        recording_id: session.recordingId,
        consent_token: session.consentToken,
      });
      if (
        runEpochRef.current !== session.runEpoch ||
        liveSessionRef.current !== session
      ) {
        return;
      }
      const url = websocketUrl(ticket.ws_url);
      if (!reconnecting) transition("connecting");
      setStatusDetail(
        reconnecting
          ? `续传 ticket 有效至 ${ticket.expires_at}`
          : `一次性凭证有效至 ${ticket.expires_at}`,
      );
      const socket = new WebSocket(url);
      socketRef.current = socket;
      protocolReadyRef.current = false;

      socket.onopen = () => {
        if (
          runEpochRef.current !== session.runEpoch ||
          liveSessionRef.current !== session ||
          socketRef.current !== socket
        ) {
          socket.close();
          return;
        }
        const initPayload: {
          type: "init";
          session_id: string;
          recording_id: number;
          consent_token: string;
          resume_from_seq: number;
          resume_token?: string;
        } = {
          type: "init",
          session_id: session.sessionId,
          recording_id: session.recordingId,
          consent_token: session.consentToken,
          resume_from_seq: ackHighWatermarkRef.current + 1,
        };
        if (reconnecting) {
          if (!session.resumeToken) {
            terminalFailure(
              "服务端未签发续传凭证，已停止重连以防并发会话抢占",
              session.runEpoch,
            );
            return;
          }
          initPayload.resume_token = session.resumeToken;
        }
        socket.send(JSON.stringify(initPayload));
        // 凭证仅保存在当前 capture session 的内存上下文中，绝不落盘。
        setConsentToken("");
      };

      socket.onmessage = (message) => {
        if (
          runEpochRef.current !== session.runEpoch ||
          liveSessionRef.current !== session ||
          socketRef.current !== socket
        ) {
          return;
        }
        let payload: Record<string, unknown>;
        try {
          payload = JSON.parse(String(message.data)) as Record<
            string,
            unknown
          >;
        } catch {
          setStatusDetail("收到无法解析的实时事件，连接已保留供后续恢复");
          return;
        }
        const type = String(payload.type ?? "");
        if (type === "session_opened") {
          if (
            typeof payload.resume_token === "string" &&
            payload.resume_token.length > 0
          ) {
            // 续传 token 是不透明的租约凭证，仅保存在当前内存会话中。
            session.resumeToken = payload.resume_token;
          }
          protocolReadyRef.current = true;
          if (!sendPendingFrames(socket, session.runEpoch)) return;
          if (finalizeRequestedRef.current) {
            transition("draining");
            setStatusDetail("续传完成，正在等待服务端持久化最后水位");
            socket.send(JSON.stringify({ type: "finalize" }));
          } else {
            if (captureRef.current?.runEpoch === session.runEpoch) {
              transition("recording");
              setStatusDetail(
                reconnecting
                  ? `连接已恢复，正在续传 ${pendingFramesRef.current.size} 帧待确认音频`
                  : "16 kHz 单声道 PCM 正在安全上传",
              );
            } else {
              transition("permission");
              setStatusDetail("请完成浏览器麦克风授权，授权前不会发送音频");
              ensureCapture(session.runEpoch);
            }
          }
        } else if (type === "frame_ack") {
          const watermark = integerWatermark(
            payload.accepted_seq_high_watermark ?? payload.seq,
          );
          if (
            watermark === null ||
            !acknowledgeFrames(watermark, session.runEpoch)
          ) {
            terminalFailure(
              "服务端返回了越界的帧 ACK，已停止采集以避免错误清理待确认音频",
              session.runEpoch,
            );
          }
        } else if (type === "realtime_text") {
          setTemporaryCaption(eventText(payload));
        } else if (type === "segment_confirmed") {
          const nestedSegment =
            typeof payload.segment === "object" && payload.segment !== null
              ? (payload.segment as Record<string, unknown>)
              : null;
          const segmentId =
            (payload.segment_id as EntityId | undefined) ??
            (nestedSegment?.id as EntityId | undefined) ??
            (nestedSegment?.idx as EntityId | undefined) ??
            null;
          const caption: ConfirmedCaption = {
            key: `${String(segmentId ?? "pending")}-${String(payload.timestamp_ms ?? Date.now())}`,
            text: eventText(payload),
            durable: payload.durable === true,
            segmentId,
          };
          setConfirmedCaptions((current) => [
            ...current.slice(-19),
            caption,
          ]);
          setTemporaryCaption("");
        } else if (type === "backpressure") {
          setStatusDetail(
            `服务端正在限流（队列 ${String(payload.queue_depth ?? "未知")}），本地待确认 ${pendingFramesRef.current.size} 帧`,
          );
        } else if (type === "ping") {
          socket.send(
            JSON.stringify({ type: "pong", ts: payload.ts ?? Date.now() }),
          );
        } else if (type === "error") {
          if (payload.code === "OUT_OF_ORDER_SEQ") {
            terminalFailure(
              `服务端拒绝倒序帧：${String(payload.message ?? "序号低于确认水位")}`,
              session.runEpoch,
            );
            return;
          }
          setStatusDetail(
            `${String(payload.message ?? "实时处理错误")}${
              payload.recoverable === true ? "；连接可继续" : ""
            }`,
          );
          if (payload.recoverable !== true) {
            terminalFailure(
              String(payload.message ?? "实时处理发生不可恢复错误"),
              session.runEpoch,
            );
          }
        } else if (type === "session_closed") {
          clearReconnectSchedule();
          protocolReadyRef.current = false;
          finalizeRequestedRef.current = false;
          liveSessionRef.current = null;
          transition("committed");
          setStatusDetail("服务端已完成 drain 与持久化确认");
          clearPendingFrames();
          void stopMedia(session.runEpoch);
          // 这一刻转写与分段才真的落库；不通知外层，工作台会在这句
          // 「已完成持久化确认」旁边继续画采集前的数据。
          onCommittedRef.current?.();
        }
      };

      socket.onerror = () => {
        if (
          runEpochRef.current !== session.runEpoch ||
          socketRef.current !== socket
        ) {
          return;
        }
        setStatusDetail(
          "实时连接发生网络错误，待确认音频仍保留在有界内存缓冲区",
        );
      };

      socket.onclose = () => {
        if (
          runEpochRef.current !== session.runEpoch ||
          liveSessionRef.current !== session ||
          socketRef.current !== socket
        ) {
          return;
        }
        socketRef.current = null;
        protocolReadyRef.current = false;
        if (
          phaseRef.current === "committed" ||
          phaseRef.current === "failed" ||
          phaseRef.current === "idle"
        ) {
          return;
        }
        scheduleReconnect(
          session,
          "连接在服务端持久化确认前关闭",
        );
      };
    } catch (error) {
      if (
        runEpochRef.current !== session.runEpoch ||
        liveSessionRef.current !== session
      ) {
        return;
      }
      scheduleReconnect(
        session,
        error instanceof Error
          ? error.message
          : "实时连接初始化失败",
      );
    }
  };

  useEffect(
    () => () => {
      runEpochRef.current += 1;
      clearReconnectSchedule();
      captureStartingEpochRef.current = null;
      void captureRef.current?.handle.stop();
      captureRef.current = null;
      liveSessionRef.current = null;
      pendingFramesRef.current.clear();
      const socket = socketRef.current;
      socketRef.current = null;
      if (socket && socket.readyState < 2) socket.close();
    },
    [],
  );

  const start = async () => {
    if (!selectedRecording || !consentToken.trim() || disabled) return;
    const numericRecordingId = Number(sourceRecordingId(selectedRecording));
    if (!Number.isSafeInteger(numericRecordingId) || numericRecordingId <= 0) {
      transition("failed");
      setStatusDetail("实时采集要求服务端持久化的数字 Recording ID");
      return;
    }
    const runEpoch = runEpochRef.current + 1;
    runEpochRef.current = runEpoch;
    clearReconnectSchedule();
    const previousSocket = socketRef.current;
    socketRef.current = null;
    if (
      previousSocket &&
      previousSocket.readyState < 2
    ) {
      previousSocket.close();
    }
    await stopMedia();
    if (runEpochRef.current !== runEpoch) return;

    sequenceRef.current = 0;
    ackHighWatermarkRef.current = -1;
    pendingFramesRef.current.clear();
    setAckHighWatermark(-1);
    setPendingFrameCount(0);
    setStatusDetail(null);
    setTemporaryCaption("");
    setConfirmedCaptions([]);
    finalizeRequestedRef.current = false;
    const session: LiveSession = {
      runEpoch,
      sessionId: createSessionId(),
      recordingId: numericRecordingId,
      consentToken: consentToken.trim(),
      resumeToken: null,
      reconnectAttempt: 0,
    };
    liveSessionRef.current = session;
    await openConnection(session, false);
  };

  const finalize = async () => {
    if (!["recording", "reconnecting"].includes(phase)) return;
    const session = liveSessionRef.current;
    if (!session || runEpochRef.current !== session.runEpoch) return;
    finalizeRequestedRef.current = true;
    transition("draining");
    await stopMedia(session.runEpoch);
    const socket = socketRef.current;
    if (
      socket?.readyState === WebSocket.OPEN &&
      protocolReadyRef.current
    ) {
      socket.send(JSON.stringify({ type: "finalize" }));
      setStatusDetail("正在等待服务端持久化确认");
    } else {
      setStatusDetail("正在恢复连接并提交待确认音频");
      scheduleReconnect(session, "提交时连接不可用");
    }
  };

  const canStart =
    !disabled &&
    Boolean(selectedRecording) &&
    Boolean(consentToken.trim()) &&
    (phase === "idle" || phase === "failed" || phase === "committed");
  const canFinalize =
    phase === "recording" || phase === "reconnecting";

  return (
    <section
      className="ag-live-capture"
      aria-labelledby="live-capture-title"
    >
      <div className="ag-live-capture__heading">
        <div>
          <strong id="live-capture-title">实时音频采集</strong>
          <span>{PHASE_LABEL[phase]}</span>
        </div>
        {canFinalize ? (
          <button type="button" onClick={() => void finalize()}>
            {phase === "reconnecting" ? "停止并安全提交" : "完成并提交"}
          </button>
        ) : phase === "draining" ? (
          <button type="button" disabled>
            正在提交
          </button>
        ) : (
          <button
            type="button"
            disabled={!canStart}
            onClick={() => void start()}
          >
            开始实时采集
          </button>
        )}
      </div>
      <div className="ag-live-capture__controls">
        <label>
          目标源录音
          <select
            aria-label="实时采集目标录音"
            value={recordingId}
            disabled={!["idle", "failed", "committed"].includes(phase)}
            onChange={(event) => setRecordingId(event.target.value)}
          >
            {recordings.map((recording) => (
              <option
                key={String(sourceRecordingId(recording))}
                value={String(sourceRecordingId(recording))}
              >
                {recording.name} · #{String(sourceRecordingId(recording))}
              </option>
            ))}
          </select>
        </label>
        <label>
          同意凭证
          <input
            type="password"
            autoComplete="off"
            aria-label="实时采集同意凭证"
            value={consentToken}
            disabled={!["idle", "failed", "committed"].includes(phase)}
            onChange={(event) => setConsentToken(event.target.value)}
          />
        </label>
      </div>
      {phase !== "idle" && (
        <p className="ag-live-capture__watermark" role="status">
          ACK 水位{" "}
          {ackHighWatermark >= 0 ? `#${ackHighWatermark}` : "尚无"} ·
          待确认 {pendingFrameCount}/{MAX_PENDING_FRAMES} 帧
        </p>
      )}
      {statusDetail && (
        <p
          className={phase === "failed" ? "is-error" : undefined}
          role={phase === "failed" ? "alert" : "status"}
        >
          {statusDetail}
        </p>
      )}
      {(temporaryCaption || confirmedCaptions.length > 0) && (
        <div className="ag-live-capture__captions" aria-live="polite">
          {temporaryCaption && (
            <p className="is-temporary">
              <span>临时</span>
              {temporaryCaption}
            </p>
          )}
          {confirmedCaptions.map((caption) => (
            <p key={caption.key} className="is-confirmed">
              <span>
                {caption.durable
                  ? `已持久化 #${String(caption.segmentId ?? "待回执")}`
                  : "已确认，等待持久化"}
              </span>
              {caption.text}
            </p>
          ))}
        </div>
      )}
    </section>
  );
}

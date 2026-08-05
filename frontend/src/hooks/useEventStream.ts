/**
 * Subscribe to the backend's domain-event feed (SSE over fetch).
 *
 * fetch-streaming instead of EventSource because EventSource cannot send an
 * Authorization header. The hook keeps the last seen event id and reconnects
 * with `after=<id>` after a backoff, so a dropped connection misses nothing —
 * the server replays from the cursor.
 *
 * This is an ADDITIVE channel: consumers keep their polling as the fallback
 * and use events to react faster. Nothing on screen may depend on the stream
 * being connected.
 */

import { useEffect, useRef } from "react";
import { useAuthStore } from "@/stores/auth";

export interface DomainEventFrame {
  id: number;
  event_type: string;
  aggregate_type: string;
  aggregate_id: string;
  payload: Record<string, unknown>;
  occurred_at: string | null;
}

const RECONNECT_DELAY_MS = 5_000;

export function useEventStream(
  types: readonly string[],
  onEvent: (event: DomainEventFrame) => void,
): void {
  const token = useAuthStore((state) => state.token);
  // 回调走 ref:连接的生命周期不应跟着每次 render 的新闭包重建。
  const handlerRef = useRef(onEvent);
  handlerRef.current = onEvent;
  const typesKey = types.join(",");

  useEffect(() => {
    if (!token) return;
    const controller = new AbortController();
    let lastId: number | null = null;
    let stopped = false;

    async function connect(): Promise<void> {
      const params = new URLSearchParams();
      if (typesKey) params.set("types", typesKey);
      if (lastId !== null) params.set("after", String(lastId));
      const query = params.toString();
      const response = await fetch(
        `/api/v1/events/stream${query ? `?${query}` : ""}`,
        {
          headers: { Authorization: `Bearer ${token}` },
          signal: controller.signal,
        },
      );
      if (!response.ok || !response.body) {
        throw new Error(`event stream HTTP ${response.status}`);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) return;
        buffer += decoder.decode(value, { stream: true });
        // SSE 帧以空行结尾;最后一段可能不完整,留在缓冲区。
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";
        for (const frame of frames) {
          const dataLine = frame
            .split("\n")
            .find((line) => line.startsWith("data: "));
          if (!dataLine) continue; // 心跳注释帧
          try {
            const event = JSON.parse(
              dataLine.slice("data: ".length),
            ) as DomainEventFrame;
            lastId = event.id;
            handlerRef.current(event);
          } catch {
            // 半截帧解析失败只跳过这一帧;游标未推进,重连后服务端会重放它。
          }
        }
      }
    }

    async function loop(): Promise<void> {
      while (!stopped) {
        try {
          await connect();
        } catch {
          // 连接失败/中断:退避后带游标重连。轮询兜底仍在,页面不会因此变盲。
        }
        if (stopped) return;
        await new Promise((resolve) =>
          window.setTimeout(resolve, RECONNECT_DELAY_MS),
        );
      }
    }

    void loop();
    return () => {
      stopped = true;
      controller.abort();
    };
  }, [token, typesKey]);
}

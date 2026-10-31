"use client";

import { useEffect, useRef, useState } from "react";

import { getAccessToken, getTenantId, onTokenChange, refreshAccessToken, wsUrl } from "@/lib/api";

/** What the API publishes for each new or updated event (see cloud api/realtime.py). */
export type LiveEventMessage = {
  type: "event";
  event_id: string;
  status: string;
  event_type: string;
  severity: string;
  branch_id: string;
  camera_id: string;
  started_at: string;
};

export type FeedStatus = "connecting" | "live" | "offline";

const CLOSE_UNAUTHORIZED = 4401;
const MAX_RETRY_MS = 30_000;

/**
 * Subscribes to /v1/ws while `enabled`. The access token is sent as the first message
 * (and again after every refresh); the socket reconnects with backoff.
 */
export function useLiveEvents(onEvent: (event: LiveEventMessage) => void, enabled = true): FeedStatus {
  const [status, setStatus] = useState<FeedStatus>("connecting");
  const handler = useRef(onEvent);

  useEffect(() => {
    handler.current = onEvent;
  }, [onEvent]);

  useEffect(() => {
    if (!enabled) return;
    let socket: WebSocket | null = null;
    let stopped = false;
    let retryMs = 1000;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;

    const authMessage = () =>
      JSON.stringify({ type: "auth", token: getAccessToken(), tenant_id: getTenantId() ?? undefined });

    const connect = () => {
      setStatus("connecting");
      const ws = new WebSocket(wsUrl("/v1/ws"));
      socket = ws;
      ws.onopen = () => ws.send(authMessage());
      ws.onmessage = (message) => {
        let data: { type?: string };
        try {
          data = JSON.parse(String(message.data));
        } catch {
          return;
        }
        if (data.type === "ready") {
          retryMs = 1000;
          setStatus("live");
        } else if (data.type === "event") {
          handler.current(data as LiveEventMessage);
        }
      };
      ws.onclose = async (close) => {
        if (stopped) return;
        setStatus("offline");
        if (close.code === CLOSE_UNAUTHORIZED) await refreshAccessToken();
        retryTimer = setTimeout(connect, retryMs);
        retryMs = Math.min(retryMs * 2, MAX_RETRY_MS);
      };
    };

    const stopListening = onTokenChange((token) => {
      if (token && socket?.readyState === WebSocket.OPEN) socket.send(authMessage());
    });
    connect();
    return () => {
      stopped = true;
      clearTimeout(retryTimer);
      stopListening();
      socket?.close();
    };
  }, [enabled]);

  return status;
}

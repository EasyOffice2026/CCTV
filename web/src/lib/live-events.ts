"use client";

import { useEffect, useRef, useSyncExternalStore } from "react";

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

/** An alert routed to this user (cloud services/alerts.py), shown as a toast. */
export type LiveAlertMessage = {
  type: "alert";
  event_id: string;
  kind: "alert" | "resolved";
  severity: string;
  branch_id: string;
  title: string;
  body: string;
  url: string;
};

type LiveMessage = LiveEventMessage | LiveAlertMessage;

export type FeedStatus = "connecting" | "live" | "offline";

const CLOSE_UNAUTHORIZED = 4401;
const MAX_RETRY_MS = 30_000;

/**
 * One socket to /v1/ws per tab, shared by every listener (the event feed and the alert
 * toasts), open while anyone listens. The access token is sent as the first message (and
 * again after every refresh); the socket reconnects with backoff.
 */
const listeners = new Set<(message: LiveMessage) => void>();
const statusListeners = new Set<() => void>();
let status: FeedStatus = "connecting";
let stop: (() => void) | null = null;

function setStatus(next: FeedStatus) {
  status = next;
  statusListeners.forEach((listener) => listener());
}

function start(): () => void {
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
      } else if (data.type === "event" || data.type === "alert") {
        listeners.forEach((listener) => listener(data as LiveMessage));
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
    setStatus("connecting");
  };
}

function useLiveMessages(onMessage: (message: LiveMessage) => void, enabled: boolean): FeedStatus {
  const handler = useRef(onMessage);
  useEffect(() => {
    handler.current = onMessage;
  }, [onMessage]);

  useEffect(() => {
    if (!enabled) return;
    const listener = (message: LiveMessage) => handler.current(message);
    listeners.add(listener);
    stop ??= start();
    return () => {
      listeners.delete(listener);
      if (listeners.size === 0) {
        stop?.();
        stop = null;
      }
    };
  }, [enabled]);

  return useSyncExternalStore(
    (notify) => {
      statusListeners.add(notify);
      return () => statusListeners.delete(notify);
    },
    () => status,
    (): FeedStatus => "connecting",
  );
}

/** New and updated events of the user's branches while `enabled`. */
export function useLiveEvents(onEvent: (event: LiveEventMessage) => void, enabled = true): FeedStatus {
  return useLiveMessages((message) => message.type === "event" && onEvent(message), enabled);
}

/** Alerts routed to the signed-in user (their alert rules, channel "web"). */
export function useLiveAlerts(onAlert: (alert: LiveAlertMessage) => void, enabled = true): void {
  useLiveMessages((message) => message.type === "alert" && onAlert(message), enabled);
}

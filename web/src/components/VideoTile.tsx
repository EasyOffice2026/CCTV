"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui";
import { useI18n } from "@/lib/i18n";
import { startWebRtc } from "@/lib/webrtc";

/** Live view and playback stop after this long without the viewer touching the page. */
export const INACTIVITY_MINUTES = 5;

type State = { kind: "idle" } | { kind: "connecting" } | { kind: "playing" } | { kind: "paused" } | { kind: "error"; message: string };

/**
 * A WebRTC video from the edge box, started on demand. `exchange` sends the offer to the
 * API (live or playback). Stops when the tab is hidden, after inactivity, and on unmount,
 * so the box only streams while someone is watching.
 */
export function VideoTile({
  title,
  exchange,
  autoStart = false,
  footer,
}: {
  title?: string;
  exchange: (offerSdp: string) => Promise<string>;
  autoStart?: boolean;
  footer?: React.ReactNode;
}) {
  const { t } = useI18n();
  const video = useRef<HTMLVideoElement>(null);
  const connection = useRef<RTCPeerConnection | null>(null);
  const idleTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const [state, setState] = useState<State>({ kind: "idle" });

  const stop = useCallback((next: State = { kind: "idle" }) => {
    clearTimeout(idleTimer.current);
    connection.current?.close();
    connection.current = null;
    if (video.current) video.current.srcObject = null;
    setState(next);
  }, []);

  const armIdleTimer = useCallback(() => {
    clearTimeout(idleTimer.current);
    idleTimer.current = setTimeout(() => stop({ kind: "paused" }), INACTIVITY_MINUTES * 60 * 1000);
  }, [stop]);

  const start = useCallback(async () => {
    stop({ kind: "connecting" });
    try {
      const pc = await startWebRtc(exchange, (stream) => {
        if (video.current) video.current.srcObject = stream;
      });
      connection.current = pc;
      pc.addEventListener("connectionstatechange", () => {
        if (pc.connectionState === "failed") stop({ kind: "error", message: "connection failed" });
      });
      setState({ kind: "playing" });
      armIdleTimer();
    } catch (error) {
      stop({ kind: "error", message: error instanceof Error ? error.message : String(error) });
    }
  }, [exchange, stop, armIdleTimer]);

  // Activity keeps the stream alive; a hidden tab stops it.
  useEffect(() => {
    if (state.kind !== "playing") return;
    const onActivity = () => armIdleTimer();
    const onVisibility = () => document.hidden && stop({ kind: "paused" });
    const events = ["pointermove", "pointerdown", "keydown", "wheel"] as const;
    events.forEach((name) => window.addEventListener(name, onActivity, { passive: true }));
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      events.forEach((name) => window.removeEventListener(name, onActivity));
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [state.kind, armIdleTimer, stop]);

  useEffect(() => {
    // Starting on mount is the point of autoStart (playback after a timeline click).
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (autoStart) void start();
    return () => stop();
  }, [autoStart, start, stop]);

  const playing = state.kind === "playing";
  return (
    <div className="overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm">
      <div className="video-frame relative bg-slate-900">
        <video ref={video} autoPlay muted playsInline className={`size-full object-contain ${playing ? "" : "hidden"}`} />
        {!playing && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 p-3 text-center text-sm text-slate-300">
            {state.kind === "connecting" && <span>{t("live.connecting")}</span>}
            {state.kind === "paused" && <span>{t("live.paused", { minutes: INACTIVITY_MINUTES })}</span>}
            {state.kind === "error" && <span className="text-red-300">{t("live.failed", { error: state.message })}</span>}
            {state.kind !== "connecting" && (
              <Button variant="primary" size="sm" onClick={() => void start()}>
                ▶ {t("live.start")}
              </Button>
            )}
          </div>
        )}
      </div>
      {(title || footer || playing) && (
        <div className="flex items-center justify-between gap-2 px-3 py-2 text-sm">
          <span className="truncate font-medium text-slate-800">{title}</span>
          <div className="flex items-center gap-2">
            {footer}
            {playing && (
              <Button size="sm" onClick={() => stop()}>
                {t("live.stop")}
              </Button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

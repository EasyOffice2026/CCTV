"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui";
import { useI18n } from "@/lib/i18n";
import { useLiveAlerts, type LiveAlertMessage } from "@/lib/live-events";

const SHOW_MS = 15_000;
const MAX_TOASTS = 4;

const TONE: Record<string, string> = {
  critical: "border-s-red-600",
  warning: "border-s-amber-500",
  info: "border-s-brand-600",
};

type Toast = LiveAlertMessage & { key: string };

/** In-app alert toasts (plan 6.2) while a dashboard tab is open. Critical alerts stay
 * until closed; others fade after a while. */
export function AlertToasts({ enabled }: { enabled: boolean }) {
  const { t } = useI18n();
  const [toasts, setToasts] = useState<Toast[]>([]);
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>());

  const dismiss = useCallback((key: string) => {
    clearTimeout(timers.current.get(key));
    timers.current.delete(key);
    setToasts((current) => current.filter((toast) => toast.key !== key));
  }, []);

  useLiveAlerts(
    useCallback(
      (alert: LiveAlertMessage) => {
        const key = `${alert.event_id}:${alert.kind}`;
        setToasts((current) => [{ ...alert, key }, ...current.filter((x) => x.key !== key)].slice(0, MAX_TOASTS));
        if (alert.severity !== "critical" || alert.kind === "resolved") {
          clearTimeout(timers.current.get(key));
          timers.current.set(
            key,
            setTimeout(() => dismiss(key), SHOW_MS),
          );
        }
      },
      [dismiss],
    ),
    enabled,
  );

  useEffect(() => {
    const pending = timers.current;
    return () => pending.forEach((timer) => clearTimeout(timer));
  }, []);

  if (!toasts.length) return null;
  return (
    <div className="pointer-events-none fixed end-4 top-16 z-40 flex w-80 max-w-[calc(100%-2rem)] flex-col gap-2" aria-live="assertive">
      {toasts.map((toast) => {
        const path = new URL(toast.url, window.location.origin);
        return (
          <div
            key={toast.key}
            role="alert"
            className={`pointer-events-auto rounded-md border border-s-4 border-slate-200 bg-white p-3 text-sm shadow-lg ${TONE[toast.severity] ?? TONE.info}`}
          >
            <div className="flex items-start gap-2">
              <p className="flex-1 font-semibold text-slate-900">{toast.title}</p>
              <Button variant="ghost" size="sm" aria-label={t("common.close")} onClick={() => dismiss(toast.key)}>
                ✕
              </Button>
            </div>
            <p className="mt-0.5 text-slate-600">{toast.body}</p>
            <Link
              href={`${path.pathname}${path.search}`}
              onClick={() => dismiss(toast.key)}
              className="mt-2 inline-block text-xs font-medium text-brand-700 hover:underline"
            >
              {t("alerts.openEvent")}
            </Link>
          </div>
        );
      })}
    </div>
  );
}

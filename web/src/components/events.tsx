"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { Badge, Button, ErrorText, Modal, Spinner, Textarea } from "@/components/ui";
import { api, unwrap, type Schemas } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useBranches } from "@/lib/branches";
import { intlLocale, useI18n } from "@/lib/i18n";
import { REVIEW_TONE, SEVERITY_TONE, key } from "@/lib/labels";
import type { MessageKey } from "@/lib/messages/en";
import { can } from "@/lib/permissions";
import { useCameraNames } from "@/lib/queries";
import { formatDateTime, formatDuration, formatTime } from "@/lib/time";

export type EventOut = Schemas["EventOut"];
type MediaKind = "snapshot" | "clip";

/** A pre-signed media link. Every fetch is audited by the API, so only load what is shown. */
function useMediaUrl(eventId: string, kind: MediaKind, enabled: boolean) {
  const { tenantId } = useAuth();
  return useQuery({
    queryKey: ["media", tenantId, eventId, kind],
    queryFn: () =>
      unwrap(
        api.GET("/v1/events/{event_id}/media/{kind}", { params: { path: { event_id: eventId, kind } } }),
      ),
    enabled,
    staleTime: 4 * 60 * 1000, // links last 5 minutes
    gcTime: 4 * 60 * 1000,
    retry: false,
  });
}

/** Loads when scrolled into view. */
function useVisible<T extends Element>(): [React.RefObject<T | null>, boolean] {
  const ref = useRef<T>(null);
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const element = ref.current;
    if (!element || visible) return;
    const observer = new IntersectionObserver(([entry]) => entry?.isIntersecting && setVisible(true), {
      rootMargin: "200px",
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [visible]);
  return [ref, visible];
}

export function Snapshot({ event, className = "" }: { event: EventOut; className?: string }) {
  const { t } = useI18n();
  const { me } = useAuth();
  const [ref, visible] = useVisible<HTMLDivElement>();
  const allowed = event.has_snapshot && can(me, "media_view");
  const { data, isError } = useMediaUrl(event.id, "snapshot", allowed && visible);
  const [broken, setBroken] = useState(false);
  return (
    <div ref={ref} className={`video-frame flex items-center justify-center overflow-hidden bg-slate-200 ${className}`}>
      {data && !broken ? (
        // eslint-disable-next-line @next/next/no-img-element -- pre-signed storage URL
        <img src={data.url} alt="" className="size-full object-cover" onError={() => setBroken(true)} />
      ) : (
        <span className="text-xs text-slate-500">{allowed && !isError && !broken ? "…" : t("events.noSnapshot")}</span>
      )}
    </div>
  );
}

export function EventCard({
  event,
  onOpen,
  highlight = false,
}: {
  event: EventOut;
  onOpen: (event: EventOut) => void;
  highlight?: boolean;
}) {
  const { t, locale } = useI18n();
  const { branchId, timezoneOf, nameOf } = useBranches();
  const cameraName = useCameraNames();
  const tz = timezoneOf(event.branch_id);
  const duration = event.ended_at
    ? formatDuration((Date.parse(event.ended_at) - Date.parse(event.started_at)) / 1000)
    : t("events.ongoing");
  return (
    <button
      type="button"
      onClick={() => onOpen(event)}
      className={`overflow-hidden rounded-lg border bg-white text-start shadow-sm transition hover:shadow-md focus-visible:outline-2 focus-visible:outline-brand-500 ${highlight ? "border-brand-500 ring-2 ring-brand-500/30" : "border-slate-200"}`}
    >
      <Snapshot event={event} />
      <div className="flex flex-col gap-1.5 p-3">
        <div className="flex items-start justify-between gap-2">
          <p className="font-medium text-slate-900">{t(key.eventType(event.event_type))}</p>
          <Badge tone={SEVERITY_TONE[event.severity]}>{t(key.severity(event.severity))}</Badge>
        </div>
        <p className="text-xs text-slate-600">
          {cameraName(event.camera_id)}
          {!branchId && nameOf(event.branch_id) ? ` · ${nameOf(event.branch_id)}` : ""}
        </p>
        <div className="flex items-center justify-between gap-2 text-xs text-slate-500">
          <span dir="ltr">{formatTime(event.started_at, tz, intlLocale(locale))}</span>
          <span>{duration}</span>
        </div>
        <div>
          <Badge tone={REVIEW_TONE[event.review_status]}>{t(key.review(event.review_status))}</Badge>
        </div>
      </div>
    </button>
  );
}

function ClipPlayer({ event }: { event: EventOut }) {
  const { t } = useI18n();
  const { data, isLoading, error } = useMediaUrl(event.id, "clip", event.has_clip);
  if (!event.has_clip) return <p className="text-sm text-slate-500">{t("events.noClip")}</p>;
  if (isLoading) return <Spinner />;
  if (error) return <ErrorText error={error} />;
  return <video src={data?.url} controls autoPlay muted playsInline className="video-frame w-full rounded bg-black" />;
}

async function download(eventId: string, kind: MediaKind) {
  const link = await unwrap(
    api.GET("/v1/events/{event_id}/media/{kind}", {
      params: { path: { event_id: eventId, kind }, query: { download: true } },
    }),
  );
  window.location.assign(link.url); // the link sets Content-Disposition: attachment
}

/** Snapshot, clip and review buttons for one event. */
export function EventDetail({ event, onClose }: { event: EventOut | null; onClose: () => void }) {
  const { t } = useI18n();
  return (
    <Modal open={!!event} onClose={onClose} title={event ? t(key.eventType(event.event_type)) : ""} wide>
      {event && <EventDetailBody key={event.id} event={event} />}
    </Modal>
  );
}

function EventDetailBody({ event }: { event: EventOut }) {
  const { t, locale } = useI18n();
  const { me } = useAuth();
  const { timezoneOf, nameOf } = useBranches();
  const cameraName = useCameraNames();
  const queryClient = useQueryClient();
  const [note, setNote] = useState(event.review_note ?? "");
  const [current, setCurrent] = useState<EventOut>(event);

  const review = useMutation({
    mutationFn: (status: Schemas["ReviewStatus"]) =>
      unwrap(
        api.PATCH("/v1/events/{event_id}/review", {
          params: { path: { event_id: current.id } },
          body: { review_status: status, note: note || null },
        }),
      ),
    onSuccess: (updated) => {
      setCurrent(updated);
      void queryClient.invalidateQueries({ queryKey: ["events"] });
    },
  });

  const tz = timezoneOf(current.branch_id);
  const canReview = can(me, "events_review");
  const canExport = can(me, "media_export");

  return (
    <div className="grid gap-4 md:grid-cols-[3fr_2fr]">
        <div className="flex flex-col gap-3">
          {can(me, "media_view") && <ClipPlayer event={current} />}
          {can(me, "media_view") && current.has_snapshot && <Snapshot event={current} className="rounded" />}
          <p className="text-xs text-slate-500">{t("events.auditNote")}</p>
        </div>
        <div className="flex flex-col gap-3 text-sm">
          <div className="flex flex-wrap gap-1.5">
            <Badge tone={SEVERITY_TONE[current.severity]}>{t(key.severity(current.severity))}</Badge>
            <Badge>{t(key.feature(current.feature))}</Badge>
            <Badge tone={current.status === "open" ? "amber" : "slate"}>{t(key.eventStatus(current.status))}</Badge>
            <Badge tone={REVIEW_TONE[current.review_status]}>{t(key.review(current.review_status))}</Badge>
          </div>
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
            <dt className="text-slate-500">{t("common.branch")}</dt>
            <dd>{nameOf(current.branch_id)}</dd>
            <dt className="text-slate-500">{t("common.camera")}</dt>
            <dd>{cameraName(current.camera_id)}</dd>
            <dt className="text-slate-500">{t("common.time")}</dt>
            <dd dir="ltr" className="text-start">
              {formatDateTime(current.started_at, tz, intlLocale(locale))}
            </dd>
            <dt className="text-slate-500">{t("events.duration")}</dt>
            <dd>
              {current.ended_at
                ? formatDuration((Date.parse(current.ended_at) - Date.parse(current.started_at)) / 1000)
                : t("events.ongoing")}
            </dd>
            <dt className="text-slate-500">{t("events.confidence")}</dt>
            <dd>{Math.round(current.confidence * 100)}%</dd>
          </dl>
          {current.reviewed_at && (
            <p className="text-xs text-slate-500">
              {t("events.reviewedBy", { time: formatDateTime(current.reviewed_at, tz, intlLocale(locale)) })}
            </p>
          )}
          {canReview && (
            <div className="flex flex-col gap-2 border-t border-slate-200 pt-3">
              <Textarea
                aria-label={t("events.note")}
                placeholder={t("events.note")}
                rows={2}
                maxLength={2000}
                value={note}
                onChange={(e) => setNote(e.target.value)}
              />
              <div className="flex flex-wrap gap-2">
                <Button variant="danger" disabled={review.isPending} onClick={() => review.mutate("valid")}>
                  {t("events.markValid")}
                </Button>
                <Button disabled={review.isPending} onClick={() => review.mutate("false_alarm")}>
                  {t("events.markFalseAlarm")}
                </Button>
                <Button disabled={review.isPending} onClick={() => review.mutate("reviewed")}>
                  {t("events.markReviewed")}
                </Button>
              </div>
              <ErrorText error={review.error} />
            </div>
          )}
          <EventAlerts eventId={current.id} timeZone={tz} />
          {canExport && (
            <div className="flex flex-wrap gap-2 border-t border-slate-200 pt-3">
              {current.has_clip && (
                <Button size="sm" onClick={() => void download(current.id, "clip")}>
                  {t("events.downloadClip")}
                </Button>
              )}
              {current.has_snapshot && (
                <Button size="sm" onClick={() => void download(current.id, "snapshot")}>
                  {t("events.downloadSnapshot")}
                </Button>
              )}
            </div>
          )}
        </div>
      </div>
  );
}

const DELIVERY_TONE: Record<Schemas["DeliveryStatus"], "slate" | "green" | "red" | "blue"> = {
  queued: "slate",
  sent: "blue",
  delivered: "green",
  failed: "red",
};

/** Who was alerted, on which channel, and how fast (plan 6.7). */
function EventAlerts({ eventId, timeZone }: { eventId: string; timeZone: string }) {
  const { t, locale } = useI18n();
  const { tenantId } = useAuth();
  const deliveries = useQuery({
    queryKey: ["event-alerts", tenantId, eventId],
    queryFn: () => unwrap(api.GET("/v1/events/{event_id}/alerts", { params: { path: { event_id: eventId } } })),
  });
  if (!deliveries.data?.length) return null;
  return (
    <div className="border-t border-slate-200 pt-3">
      <p className="mb-1 font-medium">{t("alerts.sentTitle")}</p>
      <ul className="flex flex-col gap-1 text-xs">
        {deliveries.data.map((d, i) => (
          <li key={i} className="flex flex-wrap items-center gap-1.5" title={d.error ?? undefined}>
            <span className="text-slate-700">{d.user_name}</span>
            <Badge>{t(`alerts.channel.${d.channel}` as MessageKey)}</Badge>
            {d.kind === "resolved" && <Badge>{t("alerts.resolved")}</Badge>}
            <Badge tone={DELIVERY_TONE[d.status]}>{t(`alerts.status.${d.status}` as MessageKey)}</Badge>
            {d.latency_ms != null && <span className="text-slate-500">{(d.latency_ms / 1000).toFixed(1)} s</span>}
            {d.sent_at && (
              <span dir="ltr" className="text-slate-400">
                {formatTime(d.sent_at, timeZone, intlLocale(locale))}
              </span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

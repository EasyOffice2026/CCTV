"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { EventDetail, type EventOut } from "@/components/events";
import { RequirePermission } from "@/components/RequirePermission";
import { Timeline } from "@/components/Timeline";
import { Card, ErrorText, Field, Input, Notice, PageHeader, Select, Spinner } from "@/components/ui";
import { VideoTile } from "@/components/VideoTile";
import { api, unwrap } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useBranches } from "@/lib/branches";
import { intlLocale, useI18n } from "@/lib/i18n";
import { key } from "@/lib/labels";
import { useCameras } from "@/lib/queries";
import { dayRange, formatDateTime, formatTime, localDate } from "@/lib/time";

/** How much is played per request (the API allows up to 2 hours). */
const PLAY_MINUTES = 30;
/** Clicking an event starts a little before it. */
const EVENT_LEAD_SECONDS = 10;

function Playback() {
  const { t, locale } = useI18n();
  const { tenantId } = useAuth();
  const { branchId, timezoneOf, nameOf } = useBranches();
  const cameras = useCameras(branchId);
  const [cameraId, setCameraId] = useState("");
  const camera = cameras.data?.find((c) => c.id === cameraId);
  const tz = timezoneOf(camera?.branch_id ?? branchId);
  const [day, setDay] = useState(() => localDate(new Date(), tz));
  const [playFrom, setPlayFrom] = useState<Date | null>(null);
  const [open, setOpen] = useState<EventOut | null>(null);
  const { start, end } = dayRange(day, tz);

  const recordings = useQuery({
    queryKey: ["recordings", tenantId, cameraId, day, tz],
    queryFn: () =>
      unwrap(
        api.GET("/v1/cameras/{camera_id}/recordings", {
          params: { path: { camera_id: cameraId }, query: { from: start.toISOString(), to: end.toISOString() } },
        }),
      ),
    enabled: !!cameraId,
    retry: false,
  });
  const events = useQuery({
    queryKey: ["events", tenantId, "camera-day", cameraId, day, tz],
    queryFn: () =>
      unwrap(
        api.GET("/v1/events", {
          params: {
            query: { camera_id: cameraId, from: start.toISOString(), to: end.toISOString(), limit: 200 },
          },
        }),
      ),
    enabled: !!cameraId,
  });

  const exchange = async (offer: string) => {
    const from = playFrom!;
    const until = new Date(from.getTime() + PLAY_MINUTES * 60 * 1000);
    const reply = await unwrap(
      api.POST("/v1/cameras/{camera_id}/playback", {
        params: { path: { camera_id: cameraId } },
        body: { sdp_offer: offer, start: from.toISOString(), end: until.toISOString() },
      }),
    );
    return reply.sdp_answer;
  };

  return (
    <>
      <PageHeader title={t("playback.title")} />
      <Card className="mb-4 flex flex-wrap items-end gap-3 p-4">
        <Field label={t("common.camera")} className="w-64">
          {(id) => (
            <Select
              id={id}
              value={cameraId}
              onChange={(e) => {
                setCameraId(e.target.value);
                setPlayFrom(null);
              }}
            >
              <option value="">—</option>
              {(cameras.data ?? []).map((c) => (
                <option key={c.id} value={c.id}>
                  {branchId ? c.name : `${c.name} · ${nameOf(c.branch_id)}`}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <Field label={t("playback.day")} className="w-44">
          {(id) => (
            <Input
              id={id}
              type="date"
              value={day}
              max={localDate(new Date(), tz)}
              onChange={(e) => {
                if (e.target.value) setDay(e.target.value);
                setPlayFrom(null);
              }}
            />
          )}
        </Field>
      </Card>

      {!cameraId ? (
        <Notice>{t("zones.chooseCamera")}</Notice>
      ) : (
        <div className="flex flex-col gap-4">
          <Card className="p-4">
            <p className="mb-2 text-sm text-slate-600">{t("playback.clickTimeline")}</p>
            {recordings.isLoading || events.isLoading ? (
              <Spinner />
            ) : (
              <Timeline
                start={start}
                end={end}
                recordings={recordings.data ?? []}
                events={events.data?.items ?? []}
                cursor={playFrom}
                onPick={setPlayFrom}
                onEvent={(event) =>
                  setPlayFrom(new Date(Date.parse(event.started_at) - EVENT_LEAD_SECONDS * 1000))
                }
              />
            )}
            {!!events.data?.items.length && (
              <div className="mt-3">
                <p className="mb-1 text-xs font-medium text-slate-500">{t("playback.events")}</p>
                <ul className="flex flex-wrap gap-2">
                  {events.data.items.map((event) => (
                    <li key={event.id}>
                      <button
                        type="button"
                        onClick={() => setOpen(event)}
                        className="rounded border border-slate-200 px-2 py-1 text-xs hover:bg-slate-50"
                      >
                        <span dir="ltr">{formatTime(event.started_at, tz, intlLocale(locale))}</span> ·{" "}
                        {t(key.eventType(event.event_type))}
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {recordings.error && (
              <p className="mt-2 text-sm text-amber-700">
                {t("playback.recordingsFailed", { error: (recordings.error as Error).message })}
              </p>
            )}
            {recordings.data?.length === 0 && <p className="mt-2 text-sm text-slate-500">{t("playback.noRecordings")}</p>}
          </Card>
          {playFrom && (
            <div className="max-w-4xl">
              <VideoTile
                key={playFrom.toISOString()}
                autoStart
                title={t("playback.playingFrom", { time: formatDateTime(playFrom, tz, intlLocale(locale)) })}
                exchange={exchange}
              />
            </div>
          )}
          <ErrorText error={events.error} />
        </div>
      )}
      <EventDetail event={open} onClose={() => setOpen(null)} />
    </>
  );
}

export default function PlaybackPage() {
  return (
    <RequirePermission anyOf={["live_view"]}>
      <Playback />
    </RequirePermission>
  );
}

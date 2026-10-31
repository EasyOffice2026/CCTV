"use client";

import { useInfiniteQuery, useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { useCallback, useState } from "react";

import { EventCard, EventDetail, type EventOut } from "@/components/events";
import { RequirePermission } from "@/components/RequirePermission";
import { Badge, Button, Empty, ErrorText, Field, PageHeader, Select, Spinner } from "@/components/ui";
import { api, unwrap, type Schemas } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useBranches } from "@/lib/branches";
import { useI18n } from "@/lib/i18n";
import { EVENT_FEATURE, FEATURES, SEVERITIES, key, type FeatureKey } from "@/lib/labels";
import { useLiveEvents, type LiveEventMessage } from "@/lib/live-events";

const PAGE_SIZE = 48;
const HIGHLIGHT_MS = 15_000;

type Page = Schemas["EventPage"];

function Feed() {
  const { t } = useI18n();
  const { tenantId } = useAuth();
  const { branchId } = useBranches();
  const queryClient = useQueryClient();
  const [feature, setFeature] = useState<FeatureKey | "">("");
  const [severity, setSeverity] = useState<Schemas["Severity"] | "">("");
  const [newOnly, setNewOnly] = useState(true);
  const [open, setOpen] = useState<EventOut | null>(null);
  const [fresh, setFresh] = useState<Set<string>>(new Set());

  const queryKey = ["events", tenantId, "feed", branchId, feature, severity, newOnly];
  const feed = useInfiniteQuery({
    queryKey,
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/v1/events", {
          params: {
            query: {
              branch_id: branchId ?? undefined,
              feature: feature || undefined,
              severity: severity || undefined,
              review_status: newOnly ? "new" : undefined,
              limit: PAGE_SIZE,
              cursor: pageParam,
            },
          },
        }),
      ),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

  const onLiveEvent = useCallback(
    async (message: LiveEventMessage) => {
      if (branchId && message.branch_id !== branchId) return;
      if (feature && EVENT_FEATURE[message.event_type as keyof typeof EVENT_FEATURE] !== feature) return;
      if (severity && message.severity !== severity) return;
      let event: EventOut;
      try {
        event = await unwrap(
          api.GET("/v1/events/{event_id}", { params: { path: { event_id: message.event_id } } }),
        );
      } catch {
        return; // e.g. no longer visible
      }
      queryClient.setQueryData<InfiniteData<Page>>(queryKey, (data) => {
        if (!data) return data;
        const exists = data.pages.some((p) => p.items.some((e) => e.id === event.id));
        const pages = exists
          ? data.pages.map((p) => ({ ...p, items: p.items.map((e) => (e.id === event.id ? event : e)) }))
          : data.pages.map((p, i) => (i === 0 ? { ...p, items: [event, ...p.items] } : p));
        return { ...data, pages };
      });
      setFresh((current) => new Set(current).add(event.id));
      setTimeout(
        () =>
          setFresh((current) => {
            const next = new Set(current);
            next.delete(event.id);
            return next;
          }),
        HIGHLIGHT_MS,
      );
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps -- queryKey is derived from these
    [branchId, feature, severity, newOnly, tenantId, queryClient],
  );
  const status = useLiveEvents(onLiveEvent);

  const events = feed.data?.pages.flatMap((p) => p.items) ?? [];
  const statusBadge = {
    live: <Badge tone="green">● {t("events.connected")}</Badge>,
    connecting: <Badge>{t("events.connecting")}</Badge>,
    offline: <Badge tone="amber">{t("events.offline")}</Badge>,
  }[status];

  return (
    <>
      <PageHeader title={t("events.title")}>{statusBadge}</PageHeader>
      <div className="mb-4 flex flex-wrap items-end gap-3">
        <Field label={t("search.feature")} className="w-48">
          {(id) => (
            <Select id={id} value={feature} onChange={(e) => setFeature(e.target.value as FeatureKey | "")}>
              <option value="">{t("common.all")}</option>
              {FEATURES.map((f) => (
                <option key={f} value={f}>
                  {t(key.feature(f))}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <Field label={t("search.severity")} className="w-36">
          {(id) => (
            <Select id={id} value={severity} onChange={(e) => setSeverity(e.target.value as Schemas["Severity"] | "")}>
              <option value="">{t("common.all")}</option>
              {SEVERITIES.map((s) => (
                <option key={s} value={s}>
                  {t(key.severity(s))}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <label className="flex items-center gap-2 pb-2 text-sm text-slate-700">
          <input type="checkbox" checked={newOnly} onChange={(e) => setNewOnly(e.target.checked)} />
          {t("events.newOnly")}
        </label>
      </div>

      {feed.isLoading ? (
        <Spinner />
      ) : feed.error ? (
        <ErrorText error={feed.error} />
      ) : events.length === 0 ? (
        <Empty>{t("events.empty")}</Empty>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {events.map((event) => (
            <EventCard key={event.id} event={event} onOpen={setOpen} highlight={fresh.has(event.id)} />
          ))}
        </div>
      )}
      {feed.hasNextPage && (
        <div className="mt-4 flex justify-center">
          <Button onClick={() => void feed.fetchNextPage()} disabled={feed.isFetchingNextPage}>
            {t("events.loadMore")}
          </Button>
        </div>
      )}
      <EventDetail event={open} onClose={() => setOpen(null)} />
    </>
  );
}

export default function EventsPage() {
  return (
    <RequirePermission anyOf={["events_view"]}>
      <Feed />
    </RequirePermission>
  );
}

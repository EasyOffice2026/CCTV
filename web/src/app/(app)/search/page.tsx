"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { useState } from "react";

import { EventCard, EventDetail, type EventOut } from "@/components/events";
import { RequirePermission } from "@/components/RequirePermission";
import { Button, Card, Empty, ErrorText, Field, Input, PageHeader, Select, Spinner } from "@/components/ui";
import type { paths } from "@/api/schema";
import { api, ensureOk, rawFetch, unwrap, type Schemas } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useBranches } from "@/lib/branches";
import { useI18n } from "@/lib/i18n";
import { EVENT_FEATURE, EVENT_TYPES, FEATURES, REVIEW_STATUSES, SEVERITIES, key } from "@/lib/labels";
import { can } from "@/lib/permissions";
import { useCameras, useStaff, useZones } from "@/lib/queries";
import { zonedToUtc } from "@/lib/time";

type Filters = {
  from: string; // datetime-local, in the branch's time zone
  to: string;
  camera_id: string;
  zone_id: string;
  feature: string;
  event_type: string[];
  severity: string;
  status: string;
  review_status: string;
  staff_id: string;
  pos_match: string;
};

const EMPTY: Filters = {
  from: "",
  to: "",
  camera_id: "",
  zone_id: "",
  feature: "",
  event_type: [],
  severity: "",
  status: "",
  review_status: "",
  staff_id: "",
  pos_match: "",
};

type Query = NonNullable<paths["/v1/events"]["get"]["parameters"]["query"]>;

function toUtc(local: string, timeZone: string): string | undefined {
  if (!local) return undefined;
  const [date, time] = local.split("T") as [string, string];
  return zonedToUtc(date, time.slice(0, 5), timeZone).toISOString();
}

function SearchEvents() {
  const { t } = useI18n();
  const { me, tenantId } = useAuth();
  const { branchId, timezoneOf } = useBranches();
  const [draft, setDraft] = useState<Filters>(EMPTY);
  const [applied, setApplied] = useState<Filters>(EMPTY);
  const [open, setOpen] = useState<EventOut | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<unknown>(null);
  const cameras = useCameras(branchId);
  const zones = useZones(draft.camera_id || null);
  const canStaff = can(me, "staff_manage");
  const staff = useStaff(branchId, canStaff);
  const tz = timezoneOf(branchId);

  const query: Query = {
    from: toUtc(applied.from, tz),
    to: toUtc(applied.to, tz),
    branch_id: branchId ?? undefined,
    camera_id: applied.camera_id || undefined,
    zone_id: applied.zone_id || undefined,
    feature: (applied.feature || undefined) as Schemas["FeatureKey"] | undefined,
    event_type: applied.event_type.length ? (applied.event_type as Schemas["EventType"][]) : undefined,
    severity: (applied.severity || undefined) as Schemas["Severity"] | undefined,
    status: (applied.status || undefined) as Schemas["EventStatus"] | undefined,
    review_status: (applied.review_status || undefined) as Schemas["ReviewStatus"] | undefined,
    staff_id: applied.staff_id || undefined,
    pos_match: (applied.pos_match || undefined) as Schemas["PosMatch"] | undefined,
  };

  const results = useInfiniteQuery({
    queryKey: ["events", tenantId, "search", query],
    queryFn: ({ pageParam }) =>
      unwrap(api.GET("/v1/events", { params: { query: { ...query, limit: 48, cursor: pageParam } } })),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

  async function exportCsv() {
    setExporting(true);
    setExportError(null);
    try {
      const params = new URLSearchParams();
      for (const [name, value] of Object.entries(query)) {
        if (Array.isArray(value)) value.forEach((v) => params.append(name, v));
        else if (value) params.set(name, String(value));
      }
      const response = await ensureOk(await rawFetch(`/v1/events/export?${params}`));
      const blob = await response.blob();
      const name = /filename="([^"]+)"/.exec(response.headers.get("content-disposition") ?? "")?.[1];
      const url = URL.createObjectURL(blob);
      const link = Object.assign(document.createElement("a"), { href: url, download: name ?? "events.csv" });
      link.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      setExportError(error);
    } finally {
      setExporting(false);
    }
  }

  const set = <K extends keyof Filters>(name: K, value: Filters[K]) => setDraft((d) => ({ ...d, [name]: value }));
  const types = draft.feature ? EVENT_TYPES.filter((e) => EVENT_FEATURE[e] === draft.feature) : EVENT_TYPES;
  const events = results.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <>
      <PageHeader title={t("search.title")}>
        {can(me, "media_export") && (
          <Button onClick={() => void exportCsv()} disabled={exporting}>
            {exporting ? t("search.exporting") : t("search.exportCsv")}
          </Button>
        )}
      </PageHeader>
      <Card className="mb-4 p-4">
        <form
          className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4"
          onSubmit={(e) => {
            e.preventDefault();
            setApplied(draft);
          }}
        >
          <Field label={t("common.from")}>
            {(id) => <Input id={id} type="datetime-local" value={draft.from} onChange={(e) => set("from", e.target.value)} />}
          </Field>
          <Field label={t("common.to")}>
            {(id) => <Input id={id} type="datetime-local" value={draft.to} onChange={(e) => set("to", e.target.value)} />}
          </Field>
          <Field label={t("common.camera")}>
            {(id) => (
              <Select
                id={id}
                value={draft.camera_id}
                onChange={(e) => setDraft((d) => ({ ...d, camera_id: e.target.value, zone_id: "" }))}
              >
                <option value="">{t("common.all")}</option>
                {(cameras.data ?? []).map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field label={t("common.zone")}>
            {(id) => (
              <Select id={id} value={draft.zone_id} disabled={!draft.camera_id} onChange={(e) => set("zone_id", e.target.value)}>
                <option value="">{t("common.all")}</option>
                {(zones.data ?? []).map((z) => (
                  <option key={z.id} value={z.id}>
                    {z.name}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field label={t("search.feature")}>
            {(id) => (
              <Select
                id={id}
                value={draft.feature}
                onChange={(e) => setDraft((d) => ({ ...d, feature: e.target.value, event_type: [] }))}
              >
                <option value="">{t("common.all")}</option>
                {FEATURES.map((f) => (
                  <option key={f} value={f}>
                    {t(key.feature(f))}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field label={t("search.eventTypes")}>
            {(id) => (
              <Select
                id={id}
                multiple
                size={3}
                value={draft.event_type}
                onChange={(e) => set("event_type", Array.from(e.target.selectedOptions, (o) => o.value))}
              >
                {types.map((type) => (
                  <option key={type} value={type}>
                    {t(key.eventType(type))}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field label={t("search.severity")}>
            {(id) => (
              <Select id={id} value={draft.severity} onChange={(e) => set("severity", e.target.value)}>
                <option value="">{t("common.all")}</option>
                {SEVERITIES.map((s) => (
                  <option key={s} value={s}>
                    {t(key.severity(s))}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field label={t("search.status")}>
            {(id) => (
              <Select id={id} value={draft.status} onChange={(e) => set("status", e.target.value)}>
                <option value="">{t("common.all")}</option>
                {(["open", "closed"] as const).map((s) => (
                  <option key={s} value={s}>
                    {t(key.eventStatus(s))}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field label={t("search.review")}>
            {(id) => (
              <Select id={id} value={draft.review_status} onChange={(e) => set("review_status", e.target.value)}>
                <option value="">{t("common.all")}</option>
                {REVIEW_STATUSES.map((r) => (
                  <option key={r} value={r}>
                    {t(key.review(r))}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field label={t("search.till")}>
            {(id) => (
              <Select id={id} value={draft.pos_match} onChange={(e) => set("pos_match", e.target.value)}>
                <option value="">{t("common.all")}</option>
                <option value="no_sale">{t("events.posNoSale")}</option>
                <option value="sale">{t("search.tillSale")}</option>
              </Select>
            )}
          </Field>
          {canStaff && (
            <Field label={t("search.staff")}>
              {(id) => (
                <Select id={id} value={draft.staff_id} onChange={(e) => set("staff_id", e.target.value)}>
                  <option value="">{t("common.all")}</option>
                  {(staff.data ?? []).map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.full_name} ({s.employee_code})
                    </option>
                  ))}
                </Select>
              )}
            </Field>
          )}
          <div className="flex items-end gap-2 sm:col-span-2 lg:col-span-4">
            <Button type="submit" variant="primary">
              {t("common.search")}
            </Button>
            <Button
              onClick={() => {
                setDraft(EMPTY);
                setApplied(EMPTY);
              }}
            >
              {t("common.clear")}
            </Button>
          </div>
        </form>
      </Card>
      <ErrorText error={exportError} />

      {results.isLoading ? (
        <Spinner />
      ) : results.error ? (
        <ErrorText error={results.error} />
      ) : events.length === 0 ? (
        <Empty>{t("events.empty")}</Empty>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {events.map((event) => (
            <EventCard key={event.id} event={event} onOpen={setOpen} />
          ))}
        </div>
      )}
      {results.hasNextPage && (
        <div className="mt-4 flex justify-center">
          <Button onClick={() => void results.fetchNextPage()} disabled={results.isFetchingNextPage}>
            {t("events.loadMore")}
          </Button>
        </div>
      )}
      <EventDetail event={open} onClose={() => setOpen(null)} />
    </>
  );
}

export default function SearchPage() {
  return (
    <RequirePermission anyOf={["events_view"]}>
      <SearchEvents />
    </RequirePermission>
  );
}

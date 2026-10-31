"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";

import { RequirePermission } from "@/components/RequirePermission";
import { Badge, Button, Card, Empty, ErrorText, Field, Input, PageHeader, Select, Spinner, Table, type Tone } from "@/components/ui";
import { api, ensureOk, rawFetch, unwrap, type Schemas } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useBranches } from "@/lib/branches";
import { intlLocale, useI18n } from "@/lib/i18n";
import { AUDIT_ACTIONS, key } from "@/lib/labels";
import { en, type MessageKey } from "@/lib/messages/en";
import { can } from "@/lib/permissions";
import { dayRange, formatDateTime } from "@/lib/time";

type Entry = Schemas["AuditLogOut"];
type Action = Schemas["AuditAction"];
type Filters = { log: "tenant" | "platform"; action: Action | ""; userId: string; target: string; from: string; to: string };

const PAGE = 100;
const known = (k: MessageKey) => k in en;
const EMPTY: Filters = { log: "tenant", action: "", userId: "", target: "", from: "", to: "" };

const ACTION_TONE: Partial<Record<Action, Tone>> = {
  login_failed: "red",
  export_clip: "amber",
  export_snapshot: "amber",
  export_events: "amber",
  export_report: "amber",
  export_audit: "amber",
  face_enrol: "blue",
  face_delete: "blue",
  consent_change: "blue",
};

/** Query string for the API: the filters, with dates as the branch's local days. */
function params(f: Filters, timeZone: string): URLSearchParams {
  const q = new URLSearchParams();
  if (f.action) q.append("action", f.action);
  if (f.userId) q.set("user_id", f.userId);
  const target = f.target.trim();
  if (target) q.set(/^[0-9a-f-]{36}$/i.test(target) ? "target_id" : "target_type", target);
  if (f.from) q.set("since", dayRange(f.from, timeZone).start.toISOString());
  if (f.to) q.set("until", dayRange(f.to, timeZone).end.toISOString());
  return q;
}

async function exportCsv(query: URLSearchParams) {
  const response = await ensureOk(await rawFetch(`/v1/audit-logs/export?${query}`));
  const name = /filename="([^"]+)"/.exec(response.headers.get("content-disposition") ?? "")?.[1] ?? "audit-log.csv";
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function Target({ entry }: { entry: Entry }) {
  const { t } = useI18n();
  if (!entry.target_type) return <span className="text-slate-400">—</span>;
  const label = known(key.auditTarget(entry.target_type)) ? t(key.auditTarget(entry.target_type)) : entry.target_type;
  if (entry.target_type === "event" && entry.target_id) {
    return (
      <Link href={`/events/?event=${entry.target_id}`} className="text-sky-700 hover:underline">
        {label}
      </Link>
    );
  }
  return (
    <span title={entry.target_id ?? undefined}>
      {label}
      {entry.target_id && (
        <span dir="ltr" className="ms-1 font-mono text-xs text-slate-500">
          {entry.target_id.slice(0, 8)}
        </span>
      )}
    </span>
  );
}

function Details({ details }: { details: Entry["details"] }) {
  const text = Object.keys(details).length ? JSON.stringify(details) : "";
  if (!text) return null;
  return (
    <code dir="ltr" className="block max-w-xs truncate text-start text-xs text-slate-600" title={text}>
      {text}
    </code>
  );
}

function AuditLog() {
  const { t, locale } = useI18n();
  const { me, tenantId } = useAuth();
  const { timezoneOf } = useBranches();
  const tz = timezoneOf(null);
  const platform = can(me, "platform_admin");
  // A super-admin who hasn't picked a client starts on the platform log.
  const start: Filters = { ...EMPTY, log: tenantId || !platform ? "tenant" : "platform" };
  const [draft, setDraft] = useState<Filters>(start);
  const [filters, setFilters] = useState<Filters>(start);
  const [exporting, setExporting] = useState<unknown>(null);
  const tenantLog = filters.log === "tenant";

  const users = useQuery({
    queryKey: ["users", tenantId],
    queryFn: () => unwrap(api.GET("/v1/users")),
    enabled: !!tenantId,
  });
  const entries = useInfiniteQuery({
    queryKey: ["audit-logs", tenantId, filters],
    initialPageParam: null as number | null,
    queryFn: async ({ pageParam }) => {
      const query = params(filters, tz);
      query.set("limit", String(PAGE));
      if (pageParam) query.set("before_id", String(pageParam));
      const response = await ensureOk(await rawFetch(`/v1/${tenantLog ? "" : "platform/"}audit-logs?${query}`));
      return (await response.json()) as Entry[];
    },
    getNextPageParam: (last) => (last.length === PAGE ? (last.at(-1)?.id ?? null) : null),
    enabled: tenantLog ? !!tenantId : platform,
  });
  const rows = entries.data?.pages.flat() ?? [];

  const who = (e: Entry) => {
    if (e.user_name) return { name: e.user_name, sub: e.user_email };
    if (e.user_id) return { name: t("audit.support"), sub: null };
    return { name: t("audit.system"), sub: null };
  };

  return (
    <>
      <PageHeader title={t("audit.title")}>
        {tenantLog && (
          <Button
            onClick={async () => {
              setExporting(null);
              try {
                await exportCsv(params(filters, tz));
              } catch (error) {
                setExporting(error);
              }
            }}
          >
            {t("audit.export")}
          </Button>
        )}
      </PageHeader>
      <p className="mb-3 text-sm text-slate-600">{t("audit.help")}</p>

      <Card>
        <form
          className="grid grid-cols-2 gap-3 md:grid-cols-6"
          onSubmit={(e) => {
            e.preventDefault();
            setFilters(draft);
          }}
        >
          {platform && (
            <Field label={t("audit.log")}>
              {(id) => (
                <Select id={id} value={draft.log} onChange={(e) => setDraft({ ...draft, log: e.target.value as Filters["log"], userId: "" })}>
                  <option value="tenant">{t("audit.logTenant")}</option>
                  <option value="platform">{t("audit.logPlatform")}</option>
                </Select>
              )}
            </Field>
          )}
          <Field label={t("audit.action")}>
            {(id) => (
              <Select id={id} value={draft.action} onChange={(e) => setDraft({ ...draft, action: e.target.value as Action | "" })}>
                <option value="">{t("common.all")}</option>
                {AUDIT_ACTIONS.map((a) => (
                  <option key={a} value={a}>
                    {t(key.auditAction(a))}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          {draft.log === "tenant" && (
            <Field label={t("audit.user")}>
              {(id) => (
                <Select id={id} value={draft.userId} onChange={(e) => setDraft({ ...draft, userId: e.target.value })}>
                  <option value="">{t("common.all")}</option>
                  {(users.data ?? []).map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.full_name}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
          )}
          <Field label={t("audit.target")}>
            {(id) => (
              <Input id={id} dir="ltr" placeholder={t("audit.targetHint")} value={draft.target} onChange={(e) => setDraft({ ...draft, target: e.target.value })} />
            )}
          </Field>
          <Field label={t("common.from")}>
            {(id) => <Input id={id} type="date" value={draft.from} onChange={(e) => setDraft({ ...draft, from: e.target.value })} />}
          </Field>
          <Field label={t("common.to")}>
            {(id) => <Input id={id} type="date" value={draft.to} min={draft.from || undefined} onChange={(e) => setDraft({ ...draft, to: e.target.value })} />}
          </Field>
          <div className="col-span-2 flex items-end gap-2 md:col-span-6">
            <Button type="submit" variant="primary">
              {t("common.apply")}
            </Button>
            <Button
              type="button"
              variant="ghost"
              onClick={() => {
                setDraft({ ...EMPTY, log: draft.log });
                setFilters({ ...EMPTY, log: draft.log });
              }}
            >
              {t("common.clear")}
            </Button>
          </div>
        </form>
      </Card>

      <Card className="mt-3">
        {entries.isLoading ? (
          <Spinner />
        ) : rows.length === 0 ? (
          <Empty>{t("audit.empty")}</Empty>
        ) : (
          <Table>
            <thead>
              <tr>
                <th>{t("common.time")}</th>
                <th>{t("audit.user")}</th>
                <th>{t("audit.action")}</th>
                <th>{t("audit.target")}</th>
                <th>{t("audit.ip")}</th>
                <th>{t("audit.details")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((e) => {
                const user = who(e);
                return (
                  <tr key={e.id}>
                    <td dir="ltr" className="whitespace-nowrap text-start text-xs">
                      {formatDateTime(e.at, tz, intlLocale(locale))}
                    </td>
                    <td>
                      <p className="font-medium">{user.name}</p>
                      {user.sub && (
                        <p dir="ltr" className="text-start text-xs text-slate-500">
                          {user.sub}
                        </p>
                      )}
                    </td>
                    <td>
                      <Badge tone={ACTION_TONE[e.action as Action] ?? "slate"}>{known(key.auditAction(e.action)) ? t(key.auditAction(e.action)) : e.action}</Badge>
                    </td>
                    <td className="text-sm">
                      <Target entry={e} />
                    </td>
                    <td dir="ltr" className="text-start font-mono text-xs" title={e.user_agent ?? undefined}>
                      {e.ip ?? "—"}
                    </td>
                    <td>
                      <Details details={e.details} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        )}
        {entries.hasNextPage && (
          <div className="mt-3 text-center">
            <Button onClick={() => void entries.fetchNextPage()} disabled={entries.isFetchingNextPage}>
              {t("audit.more")}
            </Button>
          </div>
        )}
        <ErrorText error={entries.error ?? users.error ?? exporting} />
      </Card>
    </>
  );
}

export default function AuditPage() {
  return (
    <RequirePermission anyOf={["tenant_admin"]}>
      <AuditLog />
    </RequirePermission>
  );
}

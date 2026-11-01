"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { RequirePermission } from "@/components/RequirePermission";
import { Badge, Button, Card, Empty, ErrorText, Field, Input, Modal, Notice, PageHeader, Select, Spinner, Table, Textarea, Toggle } from "@/components/ui";
import { api, unwrap, type Schemas } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useBranches } from "@/lib/branches";
import { intlLocale, useI18n } from "@/lib/i18n";
import { SEVERITIES, SEVERITY_TONE, key } from "@/lib/labels";
import type { MessageKey } from "@/lib/messages/en";
import { can } from "@/lib/permissions";
import { formatDateTime } from "@/lib/time";

type Topic = Schemas["WebhookTopic"];
type Severity = Schemas["Severity"];
type Algorithm = Schemas["SsoAlgorithm"];

const SCOPES = ["kpis:read", "events:read"] as const;
const TOPICS: Topic[] = ["event.new", "event.closed", "report.ready"];
const ALGORITHMS: Algorithm[] = ["HS256", "RS256", "ES256"];

/** A secret shown once, right after it was made. */
function Secret({ label, value, onDone }: { label: string; value: string; onDone: () => void }) {
  const { t } = useI18n();
  return (
    <Notice tone="green">
      <p className="mb-1 font-medium">{label}</p>
      <code dir="ltr" className="block rounded bg-white px-2 py-1 text-xs break-all select-all">
        {value}
      </code>
      <p className="mt-1 text-xs">{t("integrations.shownOnce")}</p>
      <Button size="sm" variant="ghost" onClick={onDone}>
        {t("common.close")}
      </Button>
    </Notice>
  );
}

function useWhen() {
  const { locale } = useI18n();
  const { timezoneOf } = useBranches();
  const { t } = useI18n();
  return (iso: string | null | undefined) => (iso ? formatDateTime(iso, timezoneOf(null), intlLocale(locale)) : t("common.never"));
}

function ApiKeys() {
  const { t } = useI18n();
  const { tenantId } = useAuth();
  const when = useWhen();
  const queryClient = useQueryClient();
  const keys = useQuery({ queryKey: ["api-keys", tenantId], queryFn: () => unwrap(api.GET("/v1/api-keys")) });
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>(["kpis:read"]);
  const [made, setMade] = useState<string | null>(null);
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["api-keys"] });
  const create = useMutation({
    mutationFn: () => unwrap(api.POST("/v1/api-keys", { body: { name, scopes } })),
    onSuccess: (r) => {
      setMade(r.key);
      setName("");
      void refresh();
    },
  });
  const revoke = useMutation({
    mutationFn: (id: string) => unwrap(api.DELETE("/v1/api-keys/{api_key_id}", { params: { path: { api_key_id: id } } })),
    onSuccess: () => void refresh(),
  });
  return (
    <Card className="flex flex-col gap-3 p-4">
      <h2 className="font-semibold">{t("integrations.apiKeys")}</h2>
      <p className="text-sm text-slate-600">{t("integrations.apiKeysHelp")}</p>
      {made && <Secret label={t("integrations.newKey")} value={made} onDone={() => setMade(null)} />}
      {keys.isLoading ? (
        <Spinner />
      ) : keys.data?.length ? (
        <Table>
          <thead>
            <tr>
              <th>{t("common.name")}</th>
              <th>{t("integrations.scopes")}</th>
              <th>{t("integrations.lastUsed")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {keys.data.map((k) => (
              <tr key={k.id} className={k.revoked_at ? "opacity-50" : ""}>
                <td>
                  <p className="font-medium">{k.name}</p>
                  <p dir="ltr" className="text-start text-xs text-slate-500">
                    cctv_{k.prefix}_…
                  </p>
                </td>
                <td className="text-xs">{k.scopes.map((s) => t(`integrations.scope.${s}` as MessageKey)).join(", ")}</td>
                <td className="text-xs">{when(k.last_used_at)}</td>
                <td className="text-end">
                  {k.revoked_at ? (
                    <Badge>{t("integrations.revoked")}</Badge>
                  ) : (
                    <Button size="sm" variant="ghost" onClick={() => window.confirm(t("common.confirmDelete", { name: k.name })) && revoke.mutate(k.id)}>
                      {t("integrations.revoke")}
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      ) : (
        <Empty />
      )}
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <Field label={t("common.name")}>
          {(id) => <Input id={id} required maxLength={100} value={name} onChange={(e) => setName(e.target.value)} />}
        </Field>
        <fieldset className="flex flex-col gap-1">
          <legend className="mb-1 text-xs font-medium text-slate-600">{t("integrations.scopes")}</legend>
          {SCOPES.map((s) => (
            <label key={s} className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={scopes.includes(s)}
                onChange={(e) => setScopes(e.target.checked ? [...scopes, s] : scopes.filter((x) => x !== s))}
              />
              {t(`integrations.scope.${s}`)}
            </label>
          ))}
        </fieldset>
        <Button type="submit" variant="primary" disabled={create.isPending || !scopes.length}>
          + {t("integrations.createKey")}
        </Button>
      </form>
      <ErrorText error={keys.error ?? create.error ?? revoke.error} />
    </Card>
  );
}

type HookForm = { name: string; url: string; topics: Topic[]; branch_id: string; min_severity: Severity };

function Deliveries({ webhookId }: { webhookId: string }) {
  const { t } = useI18n();
  const when = useWhen();
  const deliveries = useQuery({
    queryKey: ["webhook-deliveries", webhookId],
    queryFn: () => unwrap(api.GET("/v1/webhooks/{webhook_id}/deliveries", { params: { path: { webhook_id: webhookId }, query: { limit: 20 } } })),
  });
  if (deliveries.isLoading) return <Spinner />;
  if (!deliveries.data?.length) return <Empty>{t("integrations.noDeliveries")}</Empty>;
  return (
    <Table>
      <tbody>
        {deliveries.data.map((d) => (
          <tr key={d.id}>
            <td className="text-xs">{when(d.created_at)}</td>
            <td dir="ltr" className="text-start text-xs">
              {d.dedup_key}
            </td>
            <td>
              <Badge tone={d.status === "sent" ? "green" : d.status === "failed" ? "red" : "amber"}>{t(`integrations.delivery.${d.status}` as MessageKey)}</Badge>
            </td>
            <td className="text-xs text-slate-500">
              {d.attempts}× {d.response_status ?? ""} {d.error ?? ""}
            </td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

function Webhooks() {
  const { t } = useI18n();
  const { tenantId } = useAuth();
  const { branches, nameOf } = useBranches();
  const when = useWhen();
  const queryClient = useQueryClient();
  const hooks = useQuery({ queryKey: ["webhooks", tenantId], queryFn: () => unwrap(api.GET("/v1/webhooks")) });
  const [form, setForm] = useState<HookForm | null>(null);
  const [secret, setSecret] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [tested, setTested] = useState<Record<string, Schemas["WebhookTestResult"]>>({});
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["webhooks"] });
  const create = useMutation({
    mutationFn: (f: HookForm) =>
      unwrap(api.POST("/v1/webhooks", { body: { name: f.name, url: f.url, topics: f.topics, branch_id: f.branch_id || null, min_severity: f.min_severity } })),
    onSuccess: (r) => {
      setSecret(r.secret ?? null);
      setForm(null);
      void refresh();
    },
  });
  const update = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Partial<Schemas["WebhookUpdate"]> }) =>
      unwrap(
        api.PATCH("/v1/webhooks/{webhook_id}", {
          params: { path: { webhook_id: id } },
          body: { all_branches: false, new_secret: false, ...body },
        }),
      ),
    onSuccess: (r) => {
      if (r.secret) setSecret(r.secret);
      void refresh();
    },
  });
  const remove = useMutation({
    mutationFn: (id: string) => unwrap(api.DELETE("/v1/webhooks/{webhook_id}", { params: { path: { webhook_id: id } } })),
    onSuccess: () => void refresh(),
  });
  const test = useMutation({
    mutationFn: (id: string) => unwrap(api.POST("/v1/webhooks/{webhook_id}/test", { params: { path: { webhook_id: id } } })),
    onSuccess: (r, id) => {
      setTested((all) => ({ ...all, [id]: r }));
      void refresh();
    },
  });

  return (
    <Card className="flex flex-col gap-3 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-semibold">{t("integrations.webhooks")}</h2>
        <Button
          variant="primary"
          onClick={() => setForm({ name: "", url: "https://", topics: ["event.new"], branch_id: "", min_severity: "warning" })}
        >
          + {t("integrations.addWebhook")}
        </Button>
      </div>
      <p className="text-sm text-slate-600">{t("integrations.webhooksHelp")}</p>
      {secret && <Secret label={t("integrations.webhookSecret")} value={secret} onDone={() => setSecret(null)} />}
      {hooks.isLoading ? (
        <Spinner />
      ) : hooks.data?.length ? (
        <ul className="flex flex-col divide-y divide-slate-100">
          {hooks.data.map((h) => (
            <li key={h.id} className="flex flex-col gap-2 py-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <p className="font-medium">{h.name}</p>
                  <p dir="ltr" className="text-start text-xs break-all text-slate-500">
                    {h.url}
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-1">
                  <Toggle
                    checked={h.enabled}
                    label={t("integrations.enabled")}
                    onChange={(enabled) => update.mutate({ id: h.id, body: { enabled } })}
                  />
                  <Button size="sm" variant="ghost" onClick={() => test.mutate(h.id)} disabled={test.isPending}>
                    {t("integrations.test")}
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setOpen(open === h.id ? null : h.id)}>
                    {t("integrations.deliveries")}
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => window.confirm(t("integrations.newSecretConfirm")) && update.mutate({ id: h.id, body: { new_secret: true } })}
                  >
                    {t("integrations.newSecret")}
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => window.confirm(t("common.confirmDelete", { name: h.name })) && remove.mutate(h.id)}>
                    {t("common.delete")}
                  </Button>
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-1 text-xs text-slate-600">
                {h.topics.map((topic) => (
                  <Badge key={topic}>{t(`integrations.topic.${topic}` as MessageKey)}</Badge>
                ))}
                <span>· {h.branch_id ? nameOf(h.branch_id) : t("shell.allBranches")}</span>
                <span>
                  · {t("integrations.minSeverity")}: <Badge tone={SEVERITY_TONE[h.min_severity]}>{t(key.severity(h.min_severity))}</Badge>
                </span>
                <span>· {t("integrations.lastSuccess", { time: when(h.last_success_at) })}</span>
              </div>
              {h.last_error && h.last_failure_at && (
                <p className="text-xs text-red-700">{t("integrations.lastError", { time: when(h.last_failure_at), error: h.last_error })}</p>
              )}
              {tested[h.id] && (
                <Notice tone={tested[h.id]?.ok ? "green" : "amber"}>
                  {tested[h.id]?.ok
                    ? t("integrations.testOk", { status: tested[h.id]?.status ?? "" })
                    : t("integrations.testFailed", { error: tested[h.id]?.error ?? "" })}
                </Notice>
              )}
              {open === h.id && <Deliveries webhookId={h.id} />}
            </li>
          ))}
        </ul>
      ) : (
        <Empty />
      )}
      <ErrorText error={hooks.error ?? update.error ?? remove.error ?? test.error} />

      <Modal open={!!form} onClose={() => setForm(null)} title={t("integrations.addWebhook")}>
        {form && (
          <form
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              create.mutate(form);
            }}
          >
            <Field label={t("common.name")}>
              {(id) => <Input id={id} required maxLength={100} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />}
            </Field>
            <Field label={t("integrations.url")} hint={t("integrations.urlHint")}>
              {(id) => (
                <Input id={id} type="url" dir="ltr" required maxLength={1000} value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} />
              )}
            </Field>
            <fieldset className="flex flex-col gap-1">
              <legend className="mb-1 text-xs font-medium text-slate-600">{t("integrations.topics")}</legend>
              {TOPICS.map((topic) => (
                <label key={topic} className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={form.topics.includes(topic)}
                    onChange={(e) =>
                      setForm({ ...form, topics: e.target.checked ? [...form.topics, topic] : form.topics.filter((x) => x !== topic) })
                    }
                  />
                  {t(`integrations.topic.${topic}`)}
                </label>
              ))}
            </fieldset>
            <Field label={t("common.branch")}>
              {(id) => (
                <Select id={id} value={form.branch_id} onChange={(e) => setForm({ ...form, branch_id: e.target.value })}>
                  <option value="">{t("shell.allBranches")}</option>
                  {branches.map((b) => (
                    <option key={b.id} value={b.id}>
                      {b.name}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field label={t("integrations.minSeverity")}>
              {(id) => (
                <Select id={id} value={form.min_severity} onChange={(e) => setForm({ ...form, min_severity: e.target.value as Severity })}>
                  {SEVERITIES.map((s) => (
                    <option key={s} value={s}>
                      {t(key.severity(s))}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <ErrorText error={create.error} />
            <Button type="submit" variant="primary" disabled={create.isPending || !form.topics.length}>
              {t("common.save")}
            </Button>
          </form>
        )}
      </Modal>
    </Card>
  );
}

type SsoForm = { name: string; issuer: string; algorithm: Algorithm; public_key: string; require_mfa: boolean };

function SignIn() {
  const { t } = useI18n();
  const { me, tenantId } = useAuth();
  const when = useWhen();
  const queryClient = useQueryClient();
  const ours = can(me, "platform_admin");
  const list = useQuery({ queryKey: ["sso-integrations", tenantId], queryFn: () => unwrap(api.GET("/v1/sso-integrations")) });
  const [form, setForm] = useState<SsoForm | null>(null);
  const [secret, setSecret] = useState<string | null>(null);
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["sso-integrations"] });
  const create = useMutation({
    mutationFn: (f: SsoForm) =>
      unwrap(
        api.POST("/v1/sso-integrations", {
          body: { name: f.name, issuer: f.issuer, algorithm: f.algorithm, require_mfa: f.require_mfa, public_key: f.algorithm === "HS256" ? null : f.public_key },
        }),
      ),
    onSuccess: (r) => {
      setSecret(r.secret ?? null);
      setForm(null);
      void refresh();
    },
  });
  const update = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Partial<Schemas["SsoIntegrationUpdate"]> }) =>
      unwrap(
        api.PATCH("/v1/sso-integrations/{integration_id}", {
          params: { path: { integration_id: id } },
          body: { new_secret: false, ...body },
        }),
      ),
    onSuccess: (r) => {
      if (r.secret) setSecret(r.secret);
      void refresh();
    },
  });
  const remove = useMutation({
    mutationFn: (id: string) => unwrap(api.DELETE("/v1/sso-integrations/{integration_id}", { params: { path: { integration_id: id } } })),
    onSuccess: () => void refresh(),
  });

  return (
    <Card className="flex flex-col gap-3 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-semibold">{t("integrations.sso")}</h2>
        {ours && (
          <Button variant="primary" onClick={() => setForm({ name: "", issuer: "", algorithm: "HS256", public_key: "", require_mfa: true })}>
            + {t("integrations.addSso")}
          </Button>
        )}
      </div>
      <p className="text-sm text-slate-600">{t(ours ? "integrations.ssoHelpAdmin" : "integrations.ssoHelp")}</p>
      {secret && <Secret label={t("integrations.ssoSecret")} value={secret} onDone={() => setSecret(null)} />}
      {list.isLoading ? (
        <Spinner />
      ) : list.data?.length ? (
        <Table>
          <thead>
            <tr>
              <th>{t("common.name")}</th>
              <th>{t("integrations.issuer")}</th>
              <th>{t("integrations.ownerMfa")}</th>
              <th>{t("integrations.lastUsed")}</th>
              <th>{t("common.status")}</th>
              {ours && <th />}
            </tr>
          </thead>
          <tbody>
            {list.data.map((i) => (
              <tr key={i.id}>
                <td>
                  <p className="font-medium">{i.name}</p>
                  <p className="text-xs text-slate-500">
                    {i.algorithm} · aud <span dir="ltr">{i.audience}</span>
                  </p>
                </td>
                <td dir="ltr" className="text-start text-xs break-all">
                  {i.issuer}
                </td>
                <td>{i.require_mfa ? <Badge tone="green">{t("common.yes")}</Badge> : <Badge tone="amber">{t("common.no")}</Badge>}</td>
                <td className="text-xs">{when(i.last_used_at)}</td>
                <td>
                  {ours ? (
                    <Toggle checked={i.enabled} label={t("integrations.enabled")} onChange={(enabled) => update.mutate({ id: i.id, body: { enabled } })} />
                  ) : (
                    <Badge tone={i.enabled ? "green" : "slate"}>{i.enabled ? t("integrations.enabled") : t("integrations.disabled")}</Badge>
                  )}
                </td>
                {ours && (
                  <td className="text-end whitespace-nowrap">
                    <Button size="sm" variant="ghost" onClick={() => update.mutate({ id: i.id, body: { require_mfa: !i.require_mfa } })}>
                      {i.require_mfa ? t("integrations.dropMfa") : t("integrations.requireMfa")}
                    </Button>
                    {i.algorithm === "HS256" && (
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => window.confirm(t("integrations.newSecretConfirm")) && update.mutate({ id: i.id, body: { new_secret: true } })}
                      >
                        {t("integrations.newSecret")}
                      </Button>
                    )}
                    <Button size="sm" variant="ghost" onClick={() => window.confirm(t("common.confirmDelete", { name: i.name })) && remove.mutate(i.id)}>
                      {t("common.delete")}
                    </Button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </Table>
      ) : (
        <Empty>{t("integrations.noSso")}</Empty>
      )}
      <ErrorText error={list.error ?? update.error ?? remove.error} />

      <Modal open={!!form} onClose={() => setForm(null)} title={t("integrations.addSso")}>
        {form && (
          <form
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              create.mutate(form);
            }}
          >
            <Field label={t("common.name")}>
              {(id) => <Input id={id} required maxLength={100} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />}
            </Field>
            <Field label={t("integrations.issuer")} hint={t("integrations.issuerHint")}>
              {(id) => (
                <Input id={id} dir="ltr" required minLength={3} maxLength={200} value={form.issuer} onChange={(e) => setForm({ ...form, issuer: e.target.value.trim() })} />
              )}
            </Field>
            <Field label={t("integrations.algorithm")}>
              {(id) => (
                <Select id={id} value={form.algorithm} onChange={(e) => setForm({ ...form, algorithm: e.target.value as Algorithm })}>
                  {ALGORITHMS.map((a) => (
                    <option key={a} value={a}>
                      {t(`integrations.algorithm.${a}`)}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            {form.algorithm !== "HS256" && (
              <Field label={t("integrations.publicKey")}>
                {(id) => (
                  <Textarea
                    id={id}
                    dir="ltr"
                    required
                    rows={6}
                    className="font-mono text-xs"
                    placeholder="-----BEGIN PUBLIC KEY-----"
                    value={form.public_key}
                    onChange={(e) => setForm({ ...form, public_key: e.target.value })}
                  />
                )}
              </Field>
            )}
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={form.require_mfa} onChange={(e) => setForm({ ...form, require_mfa: e.target.checked })} />
              {t("integrations.requireMfaHelp")}
            </label>
            <ErrorText error={create.error} />
            <Button type="submit" variant="primary" disabled={create.isPending}>
              {t("common.save")}
            </Button>
          </form>
        )}
      </Modal>
    </Card>
  );
}

export default function IntegrationsPage() {
  const { t } = useI18n();
  return (
    <RequirePermission anyOf={["tenant_admin"]}>
      <PageHeader title={t("integrations.title")} />
      <p className="mb-4 max-w-3xl text-sm text-slate-600">{t("integrations.help")}</p>
      <div className="flex flex-col gap-4">
        <SignIn />
        <ApiKeys />
        <Webhooks />
      </div>
    </RequirePermission>
  );
}

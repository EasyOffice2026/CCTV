"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { RequirePermission } from "@/components/RequirePermission";
import { Badge, Button, Card, ErrorText, Field, Input, Modal, Notice, PageHeader, Select, Spinner, Table, Toggle } from "@/components/ui";
import { api, unwrap, type Schemas } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useBranches } from "@/lib/branches";
import { useI18n, type Locale } from "@/lib/i18n";
import { FEATURES, SEVERITIES, SEVERITY_TONE, key, type FeatureKey } from "@/lib/labels";
import type { MessageKey } from "@/lib/messages/en";
import { can } from "@/lib/permissions";
import { disablePush, enablePush, pushState, type PushState } from "@/lib/push";

type Rule = Schemas["AlertRuleOut"];
type RuleIn = Schemas["AlertRuleIn"];
type Channel = Schemas["AlertChannel"];
type Profile = Schemas["AlertProfileOut"];

const CHANNELS: Channel[] = ["web", "whatsapp", "email"];
const channelKey = (c: string) => `alerts.channel.${c}` as MessageKey;
const hhmm = (value: string | null | undefined) => (value ? value.slice(0, 5) : "");

/** A sensible first rule: critical security alerts (intruders after hours) everywhere. */
const RECOMMENDED: RuleIn = {
  feature_key: "security",
  branch_id: null,
  min_severity: "critical",
  channels: ["web", "whatsapp"],
  notify_resolved: false,
  enabled: true,
};

function useProfile() {
  const { tenantId } = useAuth();
  return useQuery({
    queryKey: ["alert-profile", tenantId],
    queryFn: () => unwrap(api.GET("/v1/me/alert-profile")),
  });
}

function TestButton({ channel, disabled }: { channel: Channel; disabled?: boolean }) {
  const { t } = useI18n();
  const test = useMutation({
    mutationFn: () => unwrap(api.POST("/v1/me/alert-test", { body: { channel } })),
  });
  return (
    <span className="inline-flex flex-col gap-1">
      <Button size="sm" disabled={disabled || test.isPending} onClick={() => test.mutate()}>
        {t("alerts.test", { channel: t(channelKey(channel)) })}
      </Button>
      {test.isSuccess && <span className="text-xs text-emerald-700">{t("alerts.testSent")}</span>}
      <ErrorText error={test.error} />
    </span>
  );
}

function BrowserCard({ profile }: { profile: Profile }) {
  const { t } = useI18n();
  const [state, setState] = useState<PushState | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void pushState(profile.push_public_key).then(setState);
  }, [profile.push_public_key]);

  const run = async (action: () => Promise<PushState>) => {
    setBusy(true);
    setError(null);
    try {
      setState(await action());
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="flex flex-col gap-3 p-4 text-sm">
      <h2 className="font-semibold">{t("alerts.browserTitle")}</h2>
      <p className="text-slate-600">{t("alerts.browserHelp")}</p>
      {state && (
        <Notice tone={state === "on" ? "green" : state === "off" ? "slate" : "amber"}>{t(`alerts.push.${state}` as MessageKey)}</Notice>
      )}
      <div className="flex flex-wrap items-start gap-2">
        {state === "off" && profile.push_public_key && (
          <Button variant="primary" disabled={busy} onClick={() => void run(() => enablePush(profile.push_public_key!))}>
            {t("alerts.enablePush")}
          </Button>
        )}
        {state === "on" && (
          <Button disabled={busy} onClick={() => void run(disablePush)}>
            {t("alerts.disablePush")}
          </Button>
        )}
        <TestButton channel="web" />
      </div>
      <ErrorText error={error} />
    </Card>
  );
}

function ProfileCard({ profile }: { profile: Profile }) {
  const { t } = useI18n();
  const { me, tenantId } = useAuth();
  const queryClient = useQueryClient();
  const [phone, setPhone] = useState(profile.phone ?? "");
  const [optIn, setOptIn] = useState(profile.whatsapp_opt_in ?? false);
  const [locale, setMessageLocale] = useState<Locale>(profile.locale ?? "en");
  const [reports, setReports] = useState<string[]>(profile.report_channels ?? []);
  const canReports = can(me, "reports_view");

  const save = useMutation({
    mutationFn: () =>
      unwrap(
        api.PUT("/v1/me/alert-profile", {
          body: {
            phone: phone.trim() || null,
            whatsapp_opt_in: optIn,
            locale,
            report_channels: reports as ("email" | "whatsapp")[],
          },
        }),
      ),
    onSuccess: (updated) => queryClient.setQueryData(["alert-profile", tenantId], updated),
  });
  const toggleReport = (channel: string, on: boolean) =>
    setReports((current) => (on ? [...current, channel] : current.filter((c) => c !== channel)));

  return (
    <Card className="p-4 text-sm">
      <h2 className="mb-3 font-semibold">{t("alerts.profileTitle")}</h2>
      {!profile.whatsapp_configured && (
        <div className="mb-3">
          <Notice tone="amber">{t("alerts.whatsappNotConfigured")}</Notice>
        </div>
      )}
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <Field label={t("alerts.phone")} hint={t("alerts.phoneHint")}>
          {(id) => (
            <Input id={id} dir="ltr" type="tel" inputMode="tel" placeholder="+965…" value={phone} onChange={(e) => setPhone(e.target.value)} />
          )}
        </Field>
        <label className="flex items-start gap-2 text-slate-700">
          <input type="checkbox" className="mt-1" checked={optIn} onChange={(e) => setOptIn(e.target.checked)} />
          <span>{t("alerts.whatsappOptIn")}</span>
        </label>
        <Field label={t("alerts.language")}>
          {(id) => (
            <Select id={id} value={locale} onChange={(e) => setMessageLocale(e.target.value as Locale)}>
              <option value="en">English</option>
              <option value="ar">العربية</option>
            </Select>
          )}
        </Field>
        {canReports && (
          <fieldset className="flex flex-col gap-1">
            <legend className="mb-1 text-sm font-medium text-slate-700">{t("alerts.reportLinks")}</legend>
            <p className="text-xs text-slate-500">{t("alerts.reportLinksHelp")}</p>
            {(["email", "whatsapp"] as const).map((channel) => (
              <label key={channel} className="flex items-center gap-2 text-slate-700">
                <input type="checkbox" checked={reports.includes(channel)} onChange={(e) => toggleReport(channel, e.target.checked)} />
                {t(channelKey(channel))}
              </label>
            ))}
          </fieldset>
        )}
        <ErrorText error={save.error} />
        {save.isSuccess && <Notice tone="green">{t("common.saved")}</Notice>}
        <div className="flex flex-wrap items-start gap-2">
          <Button type="submit" variant="primary" disabled={save.isPending}>
            {t("common.save")}
          </Button>
          <TestButton channel="whatsapp" disabled={!profile.whatsapp_opt_in} />
          <TestButton channel="email" />
        </div>
      </form>
    </Card>
  );
}

function RuleDialog({ rule, onClose }: { rule: Rule | "new" | null; onClose: () => void }) {
  const { t } = useI18n();
  const { branches } = useBranches();
  const queryClient = useQueryClient();
  const initial: RuleIn = rule && rule !== "new" ? rule : { ...RECOMMENDED, feature_key: null, min_severity: "warning", channels: ["web"] };
  const [form, setForm] = useState<RuleIn>(initial);
  const [quiet, setQuiet] = useState(!!initial.quiet_from);

  const save = useMutation({
    mutationFn: () => {
      const body: RuleIn = {
        ...form,
        quiet_from: quiet ? form.quiet_from || "22:00" : null,
        quiet_until: quiet ? form.quiet_until || "07:00" : null,
      };
      return rule && rule !== "new"
        ? unwrap(api.PUT("/v1/me/alert-rules/{rule_id}", { params: { path: { rule_id: rule.id } }, body }))
        : unwrap(api.POST("/v1/me/alert-rules", { body }));
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["alert-rules"] });
      onClose();
    },
  });
  const toggleChannel = (channel: Channel, on: boolean) =>
    setForm((f) => ({ ...f, channels: on ? [...(f.channels ?? []), channel] : (f.channels ?? []).filter((c) => c !== channel) }));

  return (
    <Modal open={!!rule} onClose={onClose} title={t(rule === "new" ? "alerts.newRule" : "alerts.editRule")}>
      <form
        className="grid grid-cols-2 gap-3 text-sm"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <Field label={t("search.feature")}>
          {(id) => (
            <Select id={id} value={form.feature_key ?? ""} onChange={(e) => setForm({ ...form, feature_key: (e.target.value || null) as FeatureKey | null })}>
              <option value="">{t("alerts.allFeatures")}</option>
              {FEATURES.map((f) => (
                <option key={f} value={f}>
                  {t(key.feature(f))}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <Field label={t("common.branch")}>
          {(id) => (
            <Select id={id} value={form.branch_id ?? ""} onChange={(e) => setForm({ ...form, branch_id: e.target.value || null })}>
              <option value="">{t("alerts.allBranches")}</option>
              {branches.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.name}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <Field label={t("alerts.minSeverity")} className="col-span-2">
          {(id) => (
            <Select id={id} value={form.min_severity ?? "warning"} onChange={(e) => setForm({ ...form, min_severity: e.target.value as Schemas["Severity"] })}>
              {SEVERITIES.map((s) => (
                <option key={s} value={s}>
                  {t("alerts.severityAtLeast", { severity: t(key.severity(s)) })}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <fieldset className="col-span-2 flex flex-wrap gap-4">
          <legend className="mb-1 text-sm font-medium text-slate-700">{t("alerts.channels")}</legend>
          {CHANNELS.map((channel) => (
            <label key={channel} className="flex items-center gap-2 text-slate-700">
              <input type="checkbox" checked={form.channels?.includes(channel) ?? false} onChange={(e) => toggleChannel(channel, e.target.checked)} />
              {t(channelKey(channel))}
            </label>
          ))}
        </fieldset>
        <label className="col-span-2 flex items-center gap-2 text-slate-700">
          <input type="checkbox" checked={quiet} onChange={(e) => setQuiet(e.target.checked)} />
          {t("alerts.quietHours")}
        </label>
        {quiet && (
          <>
            <Field label={t("common.from")}>
              {(id) => <Input id={id} type="time" required value={hhmm(form.quiet_from) || "22:00"} onChange={(e) => setForm({ ...form, quiet_from: e.target.value })} />}
            </Field>
            <Field label={t("common.to")}>
              {(id) => <Input id={id} type="time" required value={hhmm(form.quiet_until) || "07:00"} onChange={(e) => setForm({ ...form, quiet_until: e.target.value })} />}
            </Field>
            <p className="col-span-2 text-xs text-slate-500">{t("alerts.quietHelp")}</p>
          </>
        )}
        <label className="col-span-2 flex items-center gap-2 text-slate-700">
          <input type="checkbox" checked={form.notify_resolved ?? false} onChange={(e) => setForm({ ...form, notify_resolved: e.target.checked })} />
          {t("alerts.notifyResolved")}
        </label>
        <div className="col-span-2">
          <ErrorText error={save.error} />
        </div>
        <Button type="submit" variant="primary" className="col-span-2" disabled={save.isPending || !form.channels?.length}>
          {t("common.save")}
        </Button>
      </form>
    </Modal>
  );
}

function Rules({ profile }: { profile: Profile }) {
  const { t } = useI18n();
  const { tenantId } = useAuth();
  const { nameOf } = useBranches();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<Rule | "new" | null>(null);
  const rules = useQuery({
    queryKey: ["alert-rules", tenantId],
    queryFn: () => unwrap(api.GET("/v1/me/alert-rules")),
  });
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["alert-rules"] });
  const update = useMutation({
    mutationFn: (rule: Rule) => unwrap(api.PUT("/v1/me/alert-rules/{rule_id}", { params: { path: { rule_id: rule.id } }, body: rule })),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: (id: string) => unwrap(api.DELETE("/v1/me/alert-rules/{rule_id}", { params: { path: { rule_id: id } } })),
    onSuccess: refresh,
  });
  const recommend = useMutation({
    mutationFn: () => unwrap(api.POST("/v1/me/alert-rules", { body: RECOMMENDED })),
    onSuccess: refresh,
  });
  const needsNumber = rules.data?.some((r) => r.enabled && r.channels.includes("whatsapp")) && !profile.whatsapp_opt_in;

  return (
    <Card className="p-4 text-sm">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h2 className="font-semibold">{t("alerts.rulesTitle")}</h2>
        <Button size="sm" variant="primary" className="ms-auto" onClick={() => setEditing("new")}>
          {t("alerts.addRule")}
        </Button>
      </div>
      <p className="mb-3 text-slate-600">{t("alerts.rulesHelp")}</p>
      {needsNumber && (
        <div className="mb-3">
          <Notice tone="amber">{t("alerts.noWhatsappNumber")}</Notice>
        </div>
      )}
      {rules.isLoading ? (
        <Spinner />
      ) : !rules.data?.length ? (
        <div className="flex flex-col items-start gap-2 text-slate-600">
          <p>{t("alerts.noRules")}</p>
          <Button onClick={() => recommend.mutate()} disabled={recommend.isPending}>
            {t("alerts.addRecommended")}
          </Button>
        </div>
      ) : (
        <Table>
          <thead>
            <tr>
              <th>{t("search.feature")}</th>
              <th>{t("common.branch")}</th>
              <th>{t("alerts.minSeverity")}</th>
              <th>{t("alerts.channels")}</th>
              <th>{t("alerts.enabled")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rules.data.map((rule) => (
              <tr key={rule.id}>
                <td>{rule.feature_key ? t(key.feature(rule.feature_key)) : t("alerts.allFeatures")}</td>
                <td>{rule.branch_id ? nameOf(rule.branch_id) : t("alerts.allBranches")}</td>
                <td>
                  <Badge tone={SEVERITY_TONE[rule.min_severity]}>{t("alerts.severityAtLeast", { severity: t(key.severity(rule.min_severity)) })}</Badge>
                  {rule.quiet_from && (
                    <p className="mt-1 text-xs text-slate-500">{t("alerts.quietRange", { from: hhmm(rule.quiet_from), until: hhmm(rule.quiet_until) })}</p>
                  )}
                </td>
                <td>
                  <div className="flex flex-wrap gap-1">
                    {rule.channels.map((c) => (
                      <Badge key={c}>{t(channelKey(c))}</Badge>
                    ))}
                    {rule.notify_resolved && <Badge tone="green">{t("alerts.resolved")}</Badge>}
                  </div>
                </td>
                <td>
                  <Toggle label={t("alerts.enabled")} checked={rule.enabled} onChange={(enabled) => update.mutate({ ...rule, enabled })} />
                </td>
                <td className="text-end whitespace-nowrap">
                  <Button size="sm" variant="ghost" onClick={() => setEditing(rule)}>
                    {t("common.edit")}
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => window.confirm(t("alerts.deleteConfirm")) && remove.mutate(rule.id)}>
                    {t("common.delete")}
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      <ErrorText error={rules.error ?? update.error ?? remove.error ?? recommend.error} />
      {editing && <RuleDialog key={editing === "new" ? "new" : editing.id} rule={editing} onClose={() => setEditing(null)} />}
    </Card>
  );
}

function AlertSettings() {
  const { t } = useI18n();
  const profile = useProfile();
  if (profile.isLoading) return <Spinner />;
  if (!profile.data) return <ErrorText error={profile.error} />;
  return (
    <div className="flex max-w-5xl flex-col gap-4">
      <p className="text-sm text-slate-600">{t("alerts.help")}</p>
      <Rules profile={profile.data} />
      <div className="grid gap-4 md:grid-cols-2">
        <BrowserCard profile={profile.data} />
        <ProfileCard profile={profile.data} />
      </div>
    </div>
  );
}

export default function AlertsPage() {
  const { t } = useI18n();
  return (
    <RequirePermission anyOf={["events_view"]}>
      <PageHeader title={t("alerts.title")} />
      <AlertSettings />
    </RequirePermission>
  );
}

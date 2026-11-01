"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { RequirePermission } from "@/components/RequirePermission";
import { Badge, Button, Card, Empty, ErrorText, Field, Input, Modal, Notice, PageHeader, Select, Spinner, Table, Textarea } from "@/components/ui";
import { api, unwrap } from "@/lib/api";
import { DEFAULT_TIMEZONE } from "@/lib/branches";
import { intlLocale, useI18n } from "@/lib/i18n";
import { HEALTH_TONE, key, UPDATE_LABEL, UPDATE_TONE } from "@/lib/labels";
import { formatDateTime } from "@/lib/time";

const EMPTY_RELEASE = { version: "", image: "", notes: "" };

/** Edge software releases and their rollout to the boxes, for our team (plan 2.10). */
function Software() {
  const { t, locale } = useI18n();
  const queryClient = useQueryClient();
  const releases = useQuery({
    queryKey: ["edge-releases"],
    queryFn: () => unwrap(api.GET("/v1/platform/edge-releases")),
    refetchInterval: 30_000,
  });
  const devices = useQuery({
    queryKey: ["platform-edge-devices"],
    queryFn: () => unwrap(api.GET("/v1/platform/edge-devices")),
    refetchInterval: 30_000,
  });
  const tenants = useQuery({ queryKey: ["platform-tenants"], queryFn: () => unwrap(api.GET("/v1/platform/tenants")) });
  const [form, setForm] = useState<typeof EMPTY_RELEASE | null>(null);
  const [rollout, setRollout] = useState<{ version: string; tenant_id: string; device_id: string } | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const when = (iso: string | null | undefined) => (iso ? formatDateTime(iso, DEFAULT_TIMEZONE, intlLocale(locale)) : "—");

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["edge-releases"] });
    void queryClient.invalidateQueries({ queryKey: ["platform-edge-devices"] });
  };
  const add = useMutation({
    mutationFn: (body: typeof EMPTY_RELEASE) => unwrap(api.POST("/v1/platform/edge-releases", { body })),
    onSuccess: () => {
      setForm(null);
      refresh();
    },
  });
  const start = useMutation({
    mutationFn: (r: NonNullable<typeof rollout>) =>
      unwrap(
        api.POST("/v1/platform/edge-releases/{version}/rollout", {
          params: { path: { version: r.version } },
          body: { tenant_id: r.tenant_id || null, device_id: r.device_id || null },
        }),
      ),
    onSuccess: (result, r) => {
      setRollout(null);
      setDone(t("software.rolledOut", { count: result.devices, version: r.version }));
      refresh();
    },
  });
  const withdraw = useMutation({
    mutationFn: (version: string) => unwrap(api.POST("/v1/platform/edge-releases/{version}/withdraw", { params: { path: { version } } })),
    onSuccess: refresh,
  });
  const boxesOf = (tenantId: string) => (devices.data ?? []).filter((d) => d.tenant_id === tenantId);

  return (
    <>
      <PageHeader title={t("software.title")}>
        <Button variant="primary" onClick={() => setForm(EMPTY_RELEASE)}>
          + {t("software.add")}
        </Button>
      </PageHeader>
      <p className="mb-4 max-w-3xl text-sm text-slate-600">{t("software.help")}</p>
      {done && <Notice tone="green">{done}</Notice>}

      <Card className="mt-3">
        {releases.isPending ? (
          <Spinner />
        ) : releases.data?.length === 0 ? (
          <Empty>{t("software.none")}</Empty>
        ) : (
          <Table>
            <thead>
              <tr>
                <th>{t("software.version")}</th>
                <th>{t("software.image")}</th>
                <th>{t("software.notes")}</th>
                <th>{t("software.added")}</th>
                <th>{t("software.running")}</th>
                <th>{t("software.updating")}</th>
                <th>{t("software.failed")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(releases.data ?? []).map((r) => (
                <tr key={r.version} className={r.withdrawn_at ? "opacity-50" : ""}>
                  <td dir="ltr" className="text-start font-medium">
                    {r.version} {r.withdrawn_at && <Badge tone="slate">{t("software.withdrawn")}</Badge>}
                  </td>
                  <td dir="ltr" className="text-start font-mono text-xs" title={r.image}>
                    {r.image.replace(/sha256:([0-9a-f]{12})[0-9a-f]+$/, "sha256:$1…")}
                  </td>
                  <td className="max-w-xs text-xs">{r.notes}</td>
                  <td dir="ltr" className="text-start text-xs">
                    {when(r.created_at)}
                  </td>
                  <td>{r.running}</td>
                  <td>{r.updating}</td>
                  <td>{r.failed > 0 ? <Badge tone="red">{r.failed}</Badge> : 0}</td>
                  <td className="text-end whitespace-nowrap">
                    {!r.withdrawn_at && (
                      <>
                        <Button size="sm" onClick={() => setRollout({ version: r.version, tenant_id: "", device_id: "" })}>
                          {t("software.rollout")}
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => window.confirm(t("software.withdrawConfirm", { version: r.version })) && withdraw.mutate(r.version)}
                        >
                          {t("software.withdraw")}
                        </Button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
        <ErrorText error={releases.error ?? withdraw.error} />
      </Card>

      <h2 className="mt-6 mb-2 text-sm font-semibold text-slate-700">{t("software.boxes")}</h2>
      <Card>
        {devices.isPending ? (
          <Spinner />
        ) : (
          <Table>
            <thead>
              <tr>
                <th>{t("health.client")}</th>
                <th>{t("common.branch")}</th>
                <th>{t("common.name")}</th>
                <th>{t("common.status")}</th>
                <th>{t("software.version")}</th>
                <th>{t("software.target")}</th>
                <th>{t("software.state")}</th>
                <th>{t("devices.lastSeen")}</th>
              </tr>
            </thead>
            <tbody>
              {(devices.data ?? []).map((d) => (
                <tr key={d.id}>
                  <td>{d.tenant}</td>
                  <td>{d.branch}</td>
                  <td className="font-medium">{d.name}</td>
                  <td>
                    <Badge tone={HEALTH_TONE[d.status]}>{t(key.deviceStatus(d.status))}</Badge>
                  </td>
                  <td dir="ltr" className="text-start">
                    {d.software_version ?? "—"}
                  </td>
                  <td dir="ltr" className="text-start">
                    {d.target_version ?? "—"}
                  </td>
                  <td className="text-xs">
                    {d.update_state && (
                      <>
                        <Badge tone={UPDATE_TONE[d.update_state]}>
                          {t(UPDATE_LABEL[d.update_state])} {d.update_version}
                        </Badge>{" "}
                        {d.update_detail}
                      </>
                    )}
                  </td>
                  <td dir="ltr" className="text-start text-xs">
                    {when(d.last_seen_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
        <ErrorText error={devices.error} />
      </Card>

      <Modal open={!!form} onClose={() => setForm(null)} title={t("software.add")}>
        {form && (
          <form
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              add.mutate(form);
            }}
          >
            <Field label={t("software.version")}>
              {(id) => <Input id={id} dir="ltr" required maxLength={50} placeholder="1.4.0" value={form.version} onChange={(e) => setForm({ ...form, version: e.target.value })} />}
            </Field>
            <Field label={t("software.image")} hint={t("software.imageHint")}>
              {(id) => (
                <Input
                  id={id}
                  dir="ltr"
                  required
                  maxLength={330}
                  placeholder="ghcr.io/…/cctv-edge@sha256:…"
                  value={form.image}
                  onChange={(e) => setForm({ ...form, image: e.target.value.trim() })}
                />
              )}
            </Field>
            <Field label={t("software.notes")}>
              {(id) => <Textarea id={id} rows={3} maxLength={2000} value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} />}
            </Field>
            <ErrorText error={add.error} />
            <Button type="submit" variant="primary" disabled={add.isPending}>
              {t("common.save")}
            </Button>
          </form>
        )}
      </Modal>

      <Modal open={!!rollout} onClose={() => setRollout(null)} title={t("software.rolloutTitle", { version: rollout?.version ?? "" })}>
        {rollout && (
          <form
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              start.mutate(rollout);
            }}
          >
            <p className="text-sm text-slate-600">{t("software.rolloutHelp")}</p>
            <Field label={t("health.client")}>
              {(id) => (
                <Select id={id} value={rollout.tenant_id} onChange={(e) => setRollout({ ...rollout, tenant_id: e.target.value, device_id: "" })}>
                  <option value="">{t("software.allClients")}</option>
                  {(tenants.data ?? []).map((tenant) => (
                    <option key={tenant.id} value={tenant.id}>
                      {tenant.name}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            {rollout.tenant_id && (
              <Field label={t("software.box")}>
                {(id) => (
                  <Select id={id} value={rollout.device_id} onChange={(e) => setRollout({ ...rollout, device_id: e.target.value })}>
                    <option value="">{t("software.allBoxes")}</option>
                    {boxesOf(rollout.tenant_id).map((d) => (
                      <option key={d.id} value={d.id}>
                        {d.branch} · {d.name} ({d.software_version ?? "—"})
                      </option>
                    ))}
                  </Select>
                )}
              </Field>
            )}
            <ErrorText error={start.error} />
            <Button type="submit" variant="primary" disabled={start.isPending}>
              {t("software.rollout")}
            </Button>
          </form>
        )}
      </Modal>
    </>
  );
}

export default function SoftwarePage() {
  return (
    <RequirePermission anyOf={["platform_admin"]}>
      <Software />
    </RequirePermission>
  );
}

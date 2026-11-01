"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Incidents } from "@/components/Incidents";
import { RequirePermission } from "@/components/RequirePermission";
import { StreamTest } from "@/components/StreamTest";
import { Badge, Button, Card, ErrorText, Field, Input, Modal, Notice, PageHeader, Select, Spinner, Table } from "@/components/ui";
import { api, unwrap, type Schemas } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useBranches } from "@/lib/branches";
import { intlLocale, useI18n } from "@/lib/i18n";
import { HEALTH_TONE, key } from "@/lib/labels";
import { formatDateTime } from "@/lib/time";

function Devices() {
  const { t, locale } = useI18n();
  const { tenantId } = useAuth();
  const { branches, branchId, nameOf, timezoneOf } = useBranches();
  const queryClient = useQueryClient();
  const devices = useQuery({
    queryKey: ["edge-devices", tenantId, branchId],
    queryFn: () => unwrap(api.GET("/v1/edge-devices", { params: { query: branchId ? { branch_id: branchId } : {} } })),
    refetchInterval: 30_000, // status follows heartbeats
  });
  const [form, setForm] = useState<{ branch_id: string; name: string; serial: string } | null>(null);
  const [code, setCode] = useState<Schemas["PairingCode"] | null>(null);
  const latestVersion = (id: string) => branches.find((b) => b.id === id)?.config_version;

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["edge-devices"] });
  const register = useMutation({
    mutationFn: (body: NonNullable<typeof form>) => unwrap(api.POST("/v1/edge-devices", { body })),
    onSuccess: (result) => {
      setForm(null);
      setCode(result);
      void refresh();
    },
  });
  const renew = useMutation({
    mutationFn: (id: string) =>
      unwrap(api.POST("/v1/edge-devices/{device_id}/pairing-code", { params: { path: { device_id: id } } })),
    onSuccess: (result) => {
      setCode(result);
      void refresh();
    },
  });
  const retire = useMutation({
    mutationFn: (id: string) => unwrap(api.DELETE("/v1/edge-devices/{device_id}", { params: { path: { device_id: id } } })),
    onSuccess: () => void refresh(),
  });

  return (
    <>
      <PageHeader title={t("devices.title")}>
        <Button variant="primary" onClick={() => setForm({ branch_id: branchId ?? branches[0]?.id ?? "", name: "", serial: "" })}>
          + {t("devices.register")}
        </Button>
      </PageHeader>
      <Card>
        {devices.isLoading ? (
          <Spinner />
        ) : (
          <Table>
            <thead>
              <tr>
                <th>{t("common.name")}</th>
                <th>{t("common.branch")}</th>
                <th>{t("devices.serial")}</th>
                <th>{t("common.status")}</th>
                <th>{t("devices.version")}</th>
                <th>{t("devices.config")}</th>
                <th>{t("devices.lastSeen")}</th>
                <th>{t("devices.stats")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(devices.data ?? []).map((d) => {
                const latest = latestVersion(d.branch_id);
                const behind = latest !== undefined && d.config_version_applied !== null && d.config_version_applied < latest;
                return (
                  <tr key={d.id} className={d.status === "retired" ? "opacity-50" : ""}>
                    <td className="font-medium">{d.name}</td>
                    <td>{nameOf(d.branch_id)}</td>
                    <td dir="ltr" className="text-start">
                      {d.serial}
                    </td>
                    <td>
                      <Badge tone={HEALTH_TONE[d.status]}>{t(key.deviceStatus(d.status))}</Badge>
                    </td>
                    <td dir="ltr" className="text-start">
                      {d.software_version ?? "—"}
                    </td>
                    <td className="text-xs">
                      {d.config_version_applied ?? "—"}{" "}
                      {d.config_version_applied !== null &&
                        (behind ? (
                          <Badge tone="amber">{t("devices.configBehind", { version: latest ?? "" })}</Badge>
                        ) : (
                          <Badge tone="green">{t("devices.configCurrent")}</Badge>
                        ))}
                    </td>
                    <td dir="ltr" className="text-start text-xs">
                      {d.last_seen_at ? formatDateTime(d.last_seen_at, timezoneOf(d.branch_id), intlLocale(locale)) : t("common.never")}
                    </td>
                    <td className="text-xs whitespace-nowrap" dir="ltr">
                      {d.disk_free_bytes != null ? `${Math.round(d.disk_free_bytes / 1024 ** 3)} GB · ` : ""}
                      {d.temperature_c != null ? `${Math.round(d.temperature_c)} °C · ` : ""}
                      {d.outbox_pending != null ? t("devices.pending", { count: d.outbox_pending }) : "—"}
                    </td>
                    <td className="text-end whitespace-nowrap">
                      {d.status !== "retired" && (
                        <Button size="sm" variant="ghost" onClick={() => renew.mutate(d.id)}>
                          {t("devices.renewCode")}
                        </Button>
                      )}
                      {d.status !== "retired" && (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => window.confirm(t("devices.retireConfirm", { name: d.name })) && retire.mutate(d.id)}
                        >
                          {t("devices.retire")}
                        </Button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        )}
        <ErrorText error={devices.error ?? renew.error ?? retire.error} />
      </Card>

      <Modal open={!!form} onClose={() => setForm(null)} title={t("devices.register")}>
        {form && (
          <form
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              register.mutate(form);
            }}
          >
            <Field label={t("common.branch")}>
              {(id) => (
                <Select id={id} required value={form.branch_id} onChange={(e) => setForm({ ...form, branch_id: e.target.value })}>
                  {branches.map((b) => (
                    <option key={b.id} value={b.id}>
                      {b.name}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field label={t("common.name")}>
              {(id) => <Input id={id} required maxLength={100} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />}
            </Field>
            <Field label={t("devices.serial")}>
              {(id) => <Input id={id} dir="ltr" required maxLength={100} value={form.serial} onChange={(e) => setForm({ ...form, serial: e.target.value })} />}
            </Field>
            <ErrorText error={register.error} />
            <Button type="submit" variant="primary" disabled={register.isPending}>
              {t("common.save")}
            </Button>
          </form>
        )}
      </Modal>

      <Modal open={!!code} onClose={() => setCode(null)} title={t("devices.pairingCode")}>
        {code && (
          <div className="flex flex-col items-center gap-3 text-center">
            <code dir="ltr" className="rounded bg-slate-100 px-4 py-3 font-mono text-3xl tracking-widest select-all">
              {code.pairing_code}
            </code>
            <p className="text-sm text-slate-600">
              {t("devices.pairingHelp", {
                time: formatDateTime(code.expires_at, timezoneOf(code.device.branch_id), intlLocale(locale)),
              })}
            </p>
          </div>
        )}
      </Modal>
      <div className="mt-4">
        <Incidents branchId={branchId} timeZone={timezoneOf(branchId)} />
      </div>
      <div className="mt-4">
        {branchId ? (
          <StreamTest key={branchId} branchId={branchId} timeZone={timezoneOf(branchId)} />
        ) : (
          <Notice tone="slate">{t("streamTest.chooseBranch")}</Notice>
        )}
      </div>
    </>
  );
}

export default function DevicesPage() {
  return (
    <RequirePermission anyOf={["devices_manage"]}>
      <Devices />
    </RequirePermission>
  );
}

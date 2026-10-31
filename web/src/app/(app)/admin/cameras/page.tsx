"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { RequirePermission } from "@/components/RequirePermission";
import { Badge, Button, Card, ErrorText, Field, Input, Modal, PageHeader, Select, Spinner, Table } from "@/components/ui";
import { api, unwrap, type Schemas } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useBranches } from "@/lib/branches";
import { intlLocale, useI18n } from "@/lib/i18n";
import { CAMERA_ROLES, HEALTH_TONE, key } from "@/lib/labels";
import { useCameras } from "@/lib/queries";
import { formatDateTime } from "@/lib/time";

type CameraForm = {
  id?: string;
  branch_id: string;
  name: string;
  role: Schemas["CameraRole"];
  nvr_id: string;
  channel: string;
  rtsp_main: string;
  rtsp_sub: string;
  credentials_ref: string;
  enabled: boolean;
};

type NvrForm = {
  branch_id: string;
  name: string;
  vendor: string;
  model: string;
  host: string;
  http_port: number;
  rtsp_port: number;
  credentials_ref: string;
};

function Cameras() {
  const { t, locale } = useI18n();
  const { tenantId } = useAuth();
  const { branches, branchId, nameOf, timezoneOf } = useBranches();
  const queryClient = useQueryClient();
  const cameras = useCameras(branchId);
  const nvrs = useQuery({ queryKey: ["nvrs", tenantId], queryFn: () => unwrap(api.GET("/v1/nvrs")) });
  const [camera, setCamera] = useState<CameraForm | null>(null);
  const [nvr, setNvr] = useState<NvrForm | null>(null);

  const visibleNvrs = (nvrs.data ?? []).filter((n) => !branchId || n.branch_id === branchId);
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["cameras"] });
    void queryClient.invalidateQueries({ queryKey: ["nvrs"] });
  };

  const saveCamera = useMutation({
    mutationFn: (form: CameraForm) => {
      const common = {
        name: form.name,
        role: form.role,
        nvr_id: form.nvr_id || null,
        channel: form.channel ? Number(form.channel) : null,
        rtsp_main: form.rtsp_main,
        rtsp_sub: form.rtsp_sub || null,
        credentials_ref: form.credentials_ref || null,
        enabled: form.enabled,
      };
      return form.id
        ? unwrap(api.PATCH("/v1/cameras/{camera_id}", { params: { path: { camera_id: form.id } }, body: common }))
        : unwrap(api.POST("/v1/cameras", { body: { ...common, branch_id: form.branch_id } }));
    },
    onSuccess: () => {
      setCamera(null);
      refresh();
    },
  });
  const deleteCamera = useMutation({
    mutationFn: (id: string) => unwrap(api.DELETE("/v1/cameras/{camera_id}", { params: { path: { camera_id: id } } })),
    onSuccess: refresh,
  });
  const saveNvr = useMutation({
    mutationFn: (form: NvrForm) =>
      unwrap(api.POST("/v1/nvrs", { body: { ...form, model: form.model || null, credentials_ref: form.credentials_ref || null } })),
    onSuccess: () => {
      setNvr(null);
      refresh();
    },
  });
  const deleteNvr = useMutation({
    mutationFn: (id: string) => unwrap(api.DELETE("/v1/nvrs/{nvr_id}", { params: { path: { nvr_id: id } } })),
    onSuccess: refresh,
  });

  const editCamera = (c?: Schemas["CameraOut"]) =>
    setCamera(
      c
        ? {
            id: c.id,
            branch_id: c.branch_id,
            name: c.name,
            role: c.role ?? "other",
            nvr_id: c.nvr_id ?? "",
            channel: c.channel ? String(c.channel) : "",
            rtsp_main: c.rtsp_main,
            rtsp_sub: c.rtsp_sub ?? "",
            credentials_ref: c.credentials_ref ?? "",
            enabled: c.enabled ?? true,
          }
        : {
            branch_id: branchId ?? branches[0]?.id ?? "",
            name: "",
            role: "other",
            nvr_id: "",
            channel: "",
            rtsp_main: "rtsp://",
            rtsp_sub: "",
            credentials_ref: "",
            enabled: true,
          },
    );

  const formNvrs = (nvrs.data ?? []).filter((n) => n.branch_id === camera?.branch_id);

  return (
    <>
      <PageHeader title={t("cameras.title")}>
        <Button onClick={() => setNvr({ branch_id: branchId ?? branches[0]?.id ?? "", name: "", vendor: "hikvision", model: "", host: "", http_port: 80, rtsp_port: 554, credentials_ref: "" })}>
          + {t("cameras.addNvr")}
        </Button>
        <Button variant="primary" onClick={() => editCamera()}>
          + {t("cameras.add")}
        </Button>
      </PageHeader>

      <Card className="mb-6">
        {cameras.isLoading ? (
          <Spinner />
        ) : (
          <Table>
            <thead>
              <tr>
                <th>{t("common.name")}</th>
                {!branchId && <th>{t("common.branch")}</th>}
                <th>{t("cameras.role")}</th>
                <th>{t("cameras.nvr")}</th>
                <th>{t("cameras.health")}</th>
                <th>{t("cameras.lastSeen")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(cameras.data ?? []).map((c) => (
                <tr key={c.id} className={c.enabled === false ? "opacity-50" : ""}>
                  <td className="font-medium">{c.name}</td>
                  {!branchId && <td>{nameOf(c.branch_id)}</td>}
                  <td>{t(key.cameraRole(c.role ?? "other"))}</td>
                  <td>
                    {(nvrs.data ?? []).find((n) => n.id === c.nvr_id)?.name}
                    {c.channel ? ` · ${t("cameras.channel")} ${c.channel}` : ""}
                  </td>
                  <td>
                    <Badge tone={HEALTH_TONE[c.health]}>{t(key.health(c.health))}</Badge>
                  </td>
                  <td dir="ltr" className="text-start text-xs">
                    {c.last_seen_at ? formatDateTime(c.last_seen_at, timezoneOf(c.branch_id), intlLocale(locale)) : t("common.never")}
                  </td>
                  <td className="text-end whitespace-nowrap">
                    <Button size="sm" variant="ghost" onClick={() => editCamera(c)}>
                      {t("common.edit")}
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => window.confirm(t("common.confirmDelete", { name: c.name })) && deleteCamera.mutate(c.id)}
                    >
                      {t("common.delete")}
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
        <ErrorText error={cameras.error ?? deleteCamera.error} />
      </Card>

      <h2 className="mb-2 font-semibold">{t("cameras.nvrs")}</h2>
      <Card>
        <Table>
          <thead>
            <tr>
              <th>{t("common.name")}</th>
              {!branchId && <th>{t("common.branch")}</th>}
              <th>{t("cameras.host")}</th>
              <th>{t("cameras.vendor")}</th>
              <th>{t("cameras.credentialsRef")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {visibleNvrs.map((n) => (
              <tr key={n.id}>
                <td className="font-medium">{n.name}</td>
                {!branchId && <td>{nameOf(n.branch_id)}</td>}
                <td dir="ltr" className="text-start">
                  {n.host}:{n.rtsp_port}
                </td>
                <td>
                  {n.vendor} {n.model}
                </td>
                <td dir="ltr" className="text-start">
                  {n.credentials_ref}
                </td>
                <td className="text-end">
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => window.confirm(t("common.confirmDelete", { name: n.name })) && deleteNvr.mutate(n.id)}
                  >
                    {t("common.delete")}
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
        <ErrorText error={nvrs.error ?? deleteNvr.error} />
      </Card>

      <Modal open={!!camera} onClose={() => setCamera(null)} title={camera?.id ? t("common.edit") : t("cameras.add")}>
        {camera && (
          <form
            className="grid grid-cols-2 gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              saveCamera.mutate(camera);
            }}
          >
            <Field label={t("common.branch")} className="col-span-2">
              {(id) => (
                <Select
                  id={id}
                  required
                  disabled={!!camera.id}
                  value={camera.branch_id}
                  onChange={(e) => setCamera({ ...camera, branch_id: e.target.value, nvr_id: "" })}
                >
                  {branches.map((b) => (
                    <option key={b.id} value={b.id}>
                      {b.name}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field label={t("common.name")}>
              {(id) => <Input id={id} required maxLength={100} value={camera.name} onChange={(e) => setCamera({ ...camera, name: e.target.value })} />}
            </Field>
            <Field label={t("cameras.role")}>
              {(id) => (
                <Select id={id} value={camera.role} onChange={(e) => setCamera({ ...camera, role: e.target.value as Schemas["CameraRole"] })}>
                  {CAMERA_ROLES.map((r) => (
                    <option key={r} value={r}>
                      {t(key.cameraRole(r))}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field label={t("cameras.nvr")}>
              {(id) => (
                <Select id={id} value={camera.nvr_id} onChange={(e) => setCamera({ ...camera, nvr_id: e.target.value })}>
                  <option value="">{t("common.none")}</option>
                  {formNvrs.map((n) => (
                    <option key={n.id} value={n.id}>
                      {n.name}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field label={t("cameras.channel")}>
              {(id) => (
                <Input id={id} type="number" min={1} max={256} value={camera.channel} onChange={(e) => setCamera({ ...camera, channel: e.target.value })} />
              )}
            </Field>
            <Field label={t("cameras.rtspMain")} hint={t("cameras.rtspHelp")} className="col-span-2">
              {(id) => (
                <Input id={id} dir="ltr" required pattern="rtsps?://[^@\s]+" value={camera.rtsp_main} onChange={(e) => setCamera({ ...camera, rtsp_main: e.target.value })} />
              )}
            </Field>
            <Field label={t("cameras.rtspSub")} hint={t("common.optional")} className="col-span-2">
              {(id) => (
                <Input id={id} dir="ltr" pattern="rtsps?://[^@\s]+" value={camera.rtsp_sub} onChange={(e) => setCamera({ ...camera, rtsp_sub: e.target.value })} />
              )}
            </Field>
            <Field label={t("cameras.credentialsRef")} hint={t("cameras.credentialsHelp")} className="col-span-2">
              {(id) => (
                <Input id={id} dir="ltr" maxLength={100} value={camera.credentials_ref} onChange={(e) => setCamera({ ...camera, credentials_ref: e.target.value })} />
              )}
            </Field>
            <label className="col-span-2 flex items-center gap-2 text-sm">
              <input type="checkbox" checked={camera.enabled} onChange={(e) => setCamera({ ...camera, enabled: e.target.checked })} />
              {t("cameras.enabled")}
            </label>
            <div className="col-span-2">
              <ErrorText error={saveCamera.error} />
            </div>
            <Button type="submit" variant="primary" className="col-span-2" disabled={saveCamera.isPending}>
              {t("common.save")}
            </Button>
          </form>
        )}
      </Modal>

      <Modal open={!!nvr} onClose={() => setNvr(null)} title={t("cameras.addNvr")}>
        {nvr && (
          <form
            className="grid grid-cols-2 gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              saveNvr.mutate(nvr);
            }}
          >
            <Field label={t("common.branch")} className="col-span-2">
              {(id) => (
                <Select id={id} required value={nvr.branch_id} onChange={(e) => setNvr({ ...nvr, branch_id: e.target.value })}>
                  {branches.map((b) => (
                    <option key={b.id} value={b.id}>
                      {b.name}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field label={t("common.name")}>
              {(id) => <Input id={id} required maxLength={100} value={nvr.name} onChange={(e) => setNvr({ ...nvr, name: e.target.value })} />}
            </Field>
            <Field label={t("cameras.model")}>
              {(id) => <Input id={id} maxLength={100} value={nvr.model} onChange={(e) => setNvr({ ...nvr, model: e.target.value })} />}
            </Field>
            <Field label={t("cameras.host")} className="col-span-2">
              {(id) => <Input id={id} dir="ltr" required pattern="[A-Za-z0-9.\-]+" value={nvr.host} onChange={(e) => setNvr({ ...nvr, host: e.target.value })} />}
            </Field>
            <Field label={t("cameras.httpPort")}>
              {(id) => <Input id={id} type="number" min={1} max={65535} value={nvr.http_port} onChange={(e) => setNvr({ ...nvr, http_port: Number(e.target.value) })} />}
            </Field>
            <Field label={t("cameras.rtspPort")}>
              {(id) => <Input id={id} type="number" min={1} max={65535} value={nvr.rtsp_port} onChange={(e) => setNvr({ ...nvr, rtsp_port: Number(e.target.value) })} />}
            </Field>
            <Field label={t("cameras.credentialsRef")} hint={t("cameras.credentialsHelp")} className="col-span-2">
              {(id) => <Input id={id} dir="ltr" maxLength={100} value={nvr.credentials_ref} onChange={(e) => setNvr({ ...nvr, credentials_ref: e.target.value })} />}
            </Field>
            <div className="col-span-2">
              <ErrorText error={saveNvr.error} />
            </div>
            <Button type="submit" variant="primary" className="col-span-2" disabled={saveNvr.isPending}>
              {t("common.save")}
            </Button>
          </form>
        )}
      </Modal>
    </>
  );
}

export default function CamerasPage() {
  return (
    <RequirePermission anyOf={["devices_manage"]}>
      <Cameras />
    </RequirePermission>
  );
}

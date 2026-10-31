"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";

import { RequirePermission } from "@/components/RequirePermission";
import { Badge, Button, Card, Empty, ErrorText, Field, Input, Notice, PageHeader, Select } from "@/components/ui";
import { ZoneEditor, type ZoneShape } from "@/components/ZoneEditor";
import { api, ensureOk, rawFetch, unwrap, type Schemas } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useBranches } from "@/lib/branches";
import { selfIntersects, validShape, LINE_TYPES, type Point } from "@/lib/geometry";
import { useI18n } from "@/lib/i18n";
import { ZONE_TYPES, key } from "@/lib/labels";
import { can, canAny } from "@/lib/permissions";
import { useCameras, useZones } from "@/lib/queries";

/** A current frame from the camera, through the edge box (audited, not stored). */
function useFrame(cameraId: string, allowed: boolean) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    if (!cameraId || !allowed) return;
    setLoading(true);
    setError(null);
    try {
      const response = await ensureOk(await rawFetch(`/v1/cameras/${cameraId}/frame`));
      const blob = await response.blob();
      setUrl((old) => {
        if (old) URL.revokeObjectURL(old);
        return URL.createObjectURL(blob);
      });
    } catch (e) {
      setUrl(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [cameraId, allowed]);

  useEffect(() => {
    // Loading the frame for the chosen camera is the effect's purpose.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);
  return { url, error, loading, reload: load };
}

function Zones() {
  const { t } = useI18n();
  const { me, tenantId } = useAuth();
  const { branchId } = useBranches();
  const queryClient = useQueryClient();
  const cameras = useCameras(branchId);
  const [cameraId, setCameraId] = useState("");
  const zones = useZones(cameraId || null);
  const canEdit = can(me, "devices_manage");
  const frame = useFrame(cameraId, canAny(me, "live_view", "devices_manage"));
  const [draft, setDraft] = useState<ZoneShape | null>(null);
  const [dirty, setDirty] = useState(false);

  const shapes: ZoneShape[] = (zones.data ?? []).map((z) => ({
    id: z.id,
    name: z.name,
    type: z.type,
    points: z.points as Point[],
  }));

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["zones", tenantId, cameraId] });

  const save = useMutation({
    mutationFn: async (zone: ZoneShape) => {
      const body = { name: zone.name, type: zone.type as Schemas["ZoneType"], points: zone.points };
      return zone.id
        ? unwrap(api.PUT("/v1/zones/{zone_id}", { params: { path: { zone_id: zone.id } }, body }))
        : unwrap(api.POST("/v1/cameras/{camera_id}/zones", { params: { path: { camera_id: cameraId } }, body }));
    },
    onSuccess: (saved) => {
      setDraft({ id: saved.id, name: saved.name, type: saved.type, points: saved.points as Point[] });
      setDirty(false);
      void refresh();
    },
  });

  const remove = useMutation({
    mutationFn: (zoneId: string) => unwrap(api.DELETE("/v1/zones/{zone_id}", { params: { path: { zone_id: zoneId } } })),
    onSuccess: () => {
      setDraft(null);
      setDirty(false);
      void refresh();
    },
  });

  const update = (change: Partial<ZoneShape>) => {
    setDraft((d) => (d ? { ...d, ...change } : d));
    setDirty(true);
  };

  const valid = draft ? validShape(draft.type, draft.points) && draft.name.trim() !== "" : false;
  const crossing = draft && !LINE_TYPES.has(draft.type) && selfIntersects(draft.points);

  return (
    <>
      <PageHeader title={t("zones.title")}>
        <Select
          aria-label={t("common.camera")}
          className="w-64"
          value={cameraId}
          onChange={(e) => {
            setCameraId(e.target.value);
            setDraft(null);
            setDirty(false);
          }}
        >
          <option value="">{t("zones.chooseCamera")}</option>
          {(cameras.data ?? []).map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </Select>
        {cameraId && (
          <Button onClick={() => void frame.reload()} disabled={frame.loading}>
            {t("zones.reloadFrame")}
          </Button>
        )}
      </PageHeader>

      {!cameraId ? (
        <Notice>{t("zones.chooseCamera")}</Notice>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[1fr_20rem]">
          <div className="flex flex-col gap-2">
            {frame.error && <Notice tone="amber">{t("zones.frameFailed", { error: frame.error })}</Notice>}
            <ZoneEditor
              imageUrl={frame.url}
              zones={shapes}
              selected={draft}
              readOnly={!canEdit}
              onChange={(points) => update({ points })}
            />
            {canEdit && draft && (
              <p className="text-xs text-slate-500">
                {LINE_TYPES.has(draft.type) ? t("zones.lineHelp") : t("zones.help")}
              </p>
            )}
            {!canEdit && <p className="text-xs text-slate-500">{t("zones.readOnly")}</p>}
          </div>

          <div className="flex flex-col gap-3">
            <Card className="p-3">
              <ul className="flex flex-col gap-1">
                {shapes.length === 0 && <Empty>{t("zones.noZones")}</Empty>}
                {shapes.map((zone) => (
                  <li key={zone.id}>
                    <button
                      type="button"
                      onClick={() => {
                        setDraft(zone);
                        setDirty(false);
                      }}
                      className={`flex w-full items-center justify-between gap-2 rounded px-2 py-1.5 text-start text-sm hover:bg-slate-50 ${draft?.id === zone.id ? "bg-brand-50" : ""}`}
                    >
                      <span className="truncate">{zone.name}</span>
                      <Badge tone={zone.type === "ignore" ? "slate" : "amber"}>{t(key.zoneType(zone.type))}</Badge>
                    </button>
                  </li>
                ))}
              </ul>
              {canEdit && (
                <Button
                  className="mt-2 w-full"
                  onClick={() => {
                    setDraft({ name: "", type: "kitchen", points: [] });
                    setDirty(true);
                  }}
                >
                  + {t("zones.new")}
                </Button>
              )}
            </Card>

            {draft && canEdit && (
              <Card className="flex flex-col gap-3 p-3">
                <Field label={t("common.name")}>
                  {(id) => <Input id={id} maxLength={100} value={draft.name} onChange={(e) => update({ name: e.target.value })} />}
                </Field>
                <Field label={t("zones.type")}>
                  {(id) => (
                    <Select
                      id={id}
                      value={draft.type}
                      onChange={(e) => {
                        const type = e.target.value;
                        // Switching between line and polygon starts the shape again.
                        const reset = LINE_TYPES.has(type) !== LINE_TYPES.has(draft.type);
                        update({ type, points: reset ? [] : draft.points });
                      }}
                    >
                      {ZONE_TYPES.map((type) => (
                        <option key={type} value={type}>
                          {t(key.zoneType(type))}
                        </option>
                      ))}
                    </Select>
                  )}
                </Field>
                {!validShape(draft.type, draft.points) && <p className="text-xs text-amber-700">{t("zones.invalid")}</p>}
                {crossing && <p className="text-xs text-amber-700">{t("zones.selfIntersecting")}</p>}
                {dirty && <p className="text-xs text-slate-500">{t("zones.unsaved")}</p>}
                <ErrorText error={save.error ?? remove.error} />
                <div className="flex flex-wrap gap-2">
                  <Button variant="primary" disabled={!valid || save.isPending} onClick={() => save.mutate(draft)}>
                    {t("common.save")}
                  </Button>
                  <Button onClick={() => update({ points: [] })}>{t("common.clear")}</Button>
                  {draft.id && (
                    <Button
                      variant="danger"
                      disabled={remove.isPending}
                      onClick={() => {
                        if (window.confirm(t("common.confirmDelete", { name: draft.name }))) remove.mutate(draft.id!);
                      }}
                    >
                      {t("common.delete")}
                    </Button>
                  )}
                </div>
              </Card>
            )}
          </div>
        </div>
      )}
    </>
  );
}

export default function ZonesPage() {
  return (
    <RequirePermission anyOf={["devices_manage", "settings_manage"]}>
      <Zones />
    </RequirePermission>
  );
}

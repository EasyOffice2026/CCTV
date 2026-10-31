"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { RequirePermission } from "@/components/RequirePermission";
import { Badge, Button, Card, ErrorText, Modal, Notice, PageHeader, Select, Spinner, Table, Textarea } from "@/components/ui";
import { api, unwrap, type Schemas } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useBranches } from "@/lib/branches";
import { useI18n } from "@/lib/i18n";
import { key } from "@/lib/labels";
import { can } from "@/lib/permissions";
import { useCameras } from "@/lib/queries";

type Setting = Schemas["FeatureSettingOut"];
type Target = { scope: "tenant" } | { scope: "branch"; branch_id: string } | { scope: "camera"; camera_id: string };
type Choice = "inherit" | "on" | "off";

function matches(setting: Setting, target: Target): boolean {
  if (setting.scope !== target.scope) return false;
  if (target.scope === "branch") return setting.branch_id === target.branch_id;
  if (target.scope === "camera") return setting.camera_id === target.camera_id;
  return true;
}

function FeatureSettings() {
  const { t } = useI18n();
  const { me, tenantId } = useAuth();
  const { branchId } = useBranches();
  const queryClient = useQueryClient();
  const catalogue = useQuery({
    queryKey: ["features"],
    queryFn: () => unwrap(api.GET("/v1/features")),
    staleTime: Infinity,
  });
  const settingsKey = ["feature-settings", tenantId];
  const settings = useQuery({ queryKey: settingsKey, queryFn: () => unwrap(api.GET("/v1/feature-settings")) });
  const cameras = useCameras(branchId);
  const [editing, setEditing] = useState<{ feature: string; target: Target; text: string } | null>(null);
  const [paramsError, setParamsError] = useState<string | null>(null);

  const change = useMutation({
    mutationFn: async ({ feature, target, choice, params }: { feature: string; target: Target; choice: Choice | "keep"; params?: Record<string, unknown> }) => {
      const existing = (settings.data ?? []).find((s) => s.feature_key === feature && matches(s, target));
      if (choice === "inherit") {
        if (existing) await unwrap(api.DELETE("/v1/feature-settings/{setting_id}", { params: { path: { setting_id: existing.id } } }));
        return;
      }
      const enabled = choice === "keep" ? (existing?.enabled ?? false) : choice === "on";
      await unwrap(
        api.PUT("/v1/feature-settings", {
          body: { ...target, feature_key: feature, enabled, params: (params ?? existing?.params ?? {}) as Schemas["FeatureSettingIn"]["params"] },
        }),
      );
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: settingsKey }),
  });

  if (!branchId) return <Notice>{t("features.chooseBranch")}</Notice>;
  if (catalogue.isLoading || settings.isLoading) return <Spinner />;
  if (catalogue.error || settings.error) return <ErrorText error={catalogue.error ?? settings.error} />;

  const all = settings.data ?? [];
  const find = (feature: string, target: Target) => all.find((s) => s.feature_key === feature && matches(s, target));
  const tenantTarget: Target = { scope: "tenant" };
  const branchTarget: Target = { scope: "branch", branch_id: branchId };
  const canTenant = can(me, "tenant_admin");

  /** The value a level inherits from the levels above it (camera > branch > tenant > default). */
  const inherited = (feature: Schemas["FeatureOut"], level: "branch" | "camera"): boolean => {
    const branch = level === "camera" ? find(feature.key, branchTarget) : undefined;
    return branch?.enabled ?? find(feature.key, tenantTarget)?.enabled ?? feature.default_enabled ?? false;
  };

  const choiceSelect = (feature: Schemas["FeatureOut"], target: Target, fallback: boolean | null, disabled = false) => {
    const setting = find(feature.key, target);
    const value: Choice = setting ? (setting.enabled ? "on" : "off") : "inherit";
    return (
      <Select
        aria-label={`${t(key.feature(feature.key))}`}
        className="w-36"
        value={value}
        disabled={disabled || change.isPending}
        onChange={(e) => change.mutate({ feature: feature.key, target, choice: e.target.value as Choice })}
      >
        <option value="inherit">
          {t("features.inherit")}
          {fallback !== null ? ` (${fallback ? t("features.on") : t("features.off")})` : ""}
        </option>
        <option value="on">{t("features.on")}</option>
        <option value="off">{t("features.off")}</option>
      </Select>
    );
  };

  const features = catalogue.data ?? [];
  return (
    <>
      <Card className="mb-4">
        <Table>
          <thead>
            <tr>
              <th>{t("search.feature")}</th>
              <th>{t("features.wholeTenant")}</th>
              <th>{t("features.thisBranch")}</th>
              <th>{t("features.params")}</th>
            </tr>
          </thead>
          <tbody>
            {features.map((feature) => {
              const branchSetting = find(feature.key, branchTarget);
              return (
                <tr key={feature.key}>
                  <td>
                    <span className="font-medium">{t(key.feature(feature.key))}</span>{" "}
                    {feature.availability === "later" && <Badge>{t("features.later")}</Badge>}
                  </td>
                  <td>{choiceSelect(feature, tenantTarget, feature.default_enabled ?? false, !canTenant)}</td>
                  <td>{choiceSelect(feature, branchTarget, inherited(feature, "branch"))}</td>
                  <td>
                    <Button
                      size="sm"
                      onClick={() => {
                        setParamsError(null);
                        setEditing({
                          feature: feature.key,
                          target: branchTarget,
                          text: JSON.stringify(branchSetting?.params ?? {}, null, 2),
                        });
                      }}
                    >
                      {t("common.edit")}
                    </Button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      </Card>

      <h2 className="mb-2 font-semibold">{t("features.perCamera")}</h2>
      <Card>
        <Table>
          <thead>
            <tr>
              <th>{t("common.camera")}</th>
              {features.map((f) => (
                <th key={f.key}>{t(key.feature(f.key))}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {(cameras.data ?? []).map((camera) => (
              <tr key={camera.id}>
                <td className="font-medium whitespace-nowrap">{camera.name}</td>
                {features.map((feature) => (
                  <td key={feature.key}>
                    {choiceSelect(feature, { scope: "camera", camera_id: camera.id }, inherited(feature, "camera"))}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </Table>
      </Card>
      <ErrorText error={change.error} />

      <Modal open={!!editing} onClose={() => setEditing(null)} title={`${t("features.params")} · ${editing ? t(key.feature(editing.feature)) : ""}`}>
        {editing && (
          <form
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              let params: unknown;
              try {
                params = JSON.parse(editing.text);
              } catch {
                params = null;
              }
              if (!params || typeof params !== "object" || Array.isArray(params)) {
                setParamsError(t("features.paramsInvalid"));
                return;
              }
              change.mutate(
                { feature: editing.feature, target: editing.target, choice: "keep", params: params as Record<string, unknown> },
                { onSuccess: () => setEditing(null) },
              );
            }}
          >
            <p className="text-xs text-slate-500">{t("features.paramsHelp")}</p>
            <Textarea
              dir="ltr"
              rows={8}
              className="font-mono"
              value={editing.text}
              onChange={(e) => setEditing({ ...editing, text: e.target.value })}
            />
            {paramsError && <p className="text-sm text-red-700">{paramsError}</p>}
            <ErrorText error={change.error} />
            <div className="flex gap-2">
              <Button type="submit" variant="primary" disabled={change.isPending}>
                {t("common.save")}
              </Button>
              <Button onClick={() => setEditing(null)}>{t("common.cancel")}</Button>
            </div>
          </form>
        )}
      </Modal>
    </>
  );
}

export default function FeaturesPage() {
  const { t } = useI18n();
  return (
    <RequirePermission anyOf={["settings_manage"]}>
      <PageHeader title={t("features.title")} />
      <FeatureSettings />
    </RequirePermission>
  );
}

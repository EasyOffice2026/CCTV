"use client";

import { RequirePermission } from "@/components/RequirePermission";
import { Badge, Empty, ErrorText, Notice, PageHeader, Spinner } from "@/components/ui";
import { VideoTile } from "@/components/VideoTile";
import { api, unwrap } from "@/lib/api";
import { useBranches } from "@/lib/branches";
import { useI18n } from "@/lib/i18n";
import { HEALTH_TONE, key } from "@/lib/labels";
import { useCameras } from "@/lib/queries";

function liveExchange(cameraId: string) {
  return async (offer: string) =>
    (
      await unwrap(
        api.POST("/v1/cameras/{camera_id}/live", {
          params: { path: { camera_id: cameraId } },
          body: { sdp_offer: offer },
        }),
      )
    ).sdp_answer;
}

function LiveGrid() {
  const { t } = useI18n();
  const { branchId } = useBranches();
  const cameras = useCameras(branchId);

  if (!branchId) return <Notice>{t("live.chooseBranch")}</Notice>;
  if (cameras.isLoading) return <Spinner />;
  if (cameras.error) return <ErrorText error={cameras.error} />;
  const enabled = (cameras.data ?? []).filter((c) => c.enabled !== false);
  if (!enabled.length) return <Empty>{t("live.noCameras")}</Empty>;
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
      {enabled.map((camera) => (
        <VideoTile
          key={camera.id}
          title={camera.name}
          exchange={liveExchange(camera.id)}
          footer={<Badge tone={HEALTH_TONE[camera.health]}>{t(key.health(camera.health))}</Badge>}
        />
      ))}
    </div>
  );
}

export default function LivePage() {
  const { t } = useI18n();
  return (
    <RequirePermission anyOf={["live_view"]}>
      <PageHeader title={t("live.title")} />
      <p className="mb-4 text-xs text-slate-500">{t("live.auditNote")}</p>
      <LiveGrid />
    </RequirePermission>
  );
}

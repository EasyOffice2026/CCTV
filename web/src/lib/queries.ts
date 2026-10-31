"use client";

import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "@/lib/api";
import { useAuth } from "@/lib/auth";

/** Cameras of a branch (or all visible ones). */
export function useCameras(branchId?: string | null) {
  const { tenantId } = useAuth();
  return useQuery({
    queryKey: ["cameras", tenantId, branchId ?? null],
    queryFn: () =>
      unwrap(api.GET("/v1/cameras", { params: { query: branchId ? { branch_id: branchId } : {} } })),
    enabled: !!tenantId,
  });
}

export function useZones(cameraId?: string | null) {
  const { tenantId } = useAuth();
  return useQuery({
    queryKey: ["zones", tenantId, cameraId],
    queryFn: () => unwrap(api.GET("/v1/cameras/{camera_id}/zones", { params: { path: { camera_id: cameraId! } } })),
    enabled: !!tenantId && !!cameraId,
  });
}

export function useStaff(branchId?: string | null, enabled = true) {
  const { tenantId } = useAuth();
  return useQuery({
    queryKey: ["staff", tenantId, branchId ?? null],
    queryFn: () => unwrap(api.GET("/v1/staff", { params: { query: branchId ? { branch_id: branchId } : {} } })),
    enabled: !!tenantId && enabled,
  });
}

export function useShifts(branchId?: string | null, enabled = true) {
  const { tenantId } = useAuth();
  return useQuery({
    queryKey: ["shifts", tenantId, branchId ?? null],
    queryFn: () => unwrap(api.GET("/v1/shifts", { params: { query: branchId ? { branch_id: branchId } : {} } })),
    enabled: !!tenantId && enabled,
  });
}

/** Map id -> name for showing camera names on events. */
export function useCameraNames(): (id: string) => string {
  const { data } = useCameras(null);
  const names = new Map((data ?? []).map((c) => [c.id, c.name]));
  return (id) => names.get(id) ?? "";
}

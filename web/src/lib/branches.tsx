"use client";

import { useQuery } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useMemo } from "react";

import { api, unwrap, type Schemas } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useStoredValue, writeStored } from "@/lib/storage";

export const DEFAULT_TIMEZONE = "Asia/Kuwait";

type Branches = {
  branches: Schemas["BranchOut"][];
  /** null = all branches the user can see. */
  branchId: string | null;
  branch: Schemas["BranchOut"] | null;
  setBranchId: (id: string | null) => void;
  timezoneOf: (branchId: string | null | undefined) => string;
  nameOf: (branchId: string | null | undefined) => string;
};

const BranchContext = createContext<Branches | null>(null);

export function useBranchList() {
  const { status, tenantId } = useAuth();
  return useQuery({
    queryKey: ["branches", tenantId],
    queryFn: () => unwrap(api.GET("/v1/branches")),
    enabled: status === "signed-in" && !!tenantId,
  });
}

export function BranchProvider({ children }: { children: React.ReactNode }) {
  const { tenantId } = useAuth();
  const { data } = useBranchList();
  const branches = useMemo(() => data ?? [], [data]);
  const storageKey = `cctv.branch.${tenantId}`;
  const branchId = useStoredValue(storageKey);

  // A remembered branch that no longer exists (or isn't visible) falls back to "all".
  const validBranchId = branchId && branches.some((b) => b.id === branchId) ? branchId : null;

  const setBranchId = useCallback((id: string | null) => writeStored(storageKey, id), [storageKey]);

  const value = useMemo<Branches>(() => {
    const byId = new Map(branches.map((b) => [b.id, b]));
    return {
      branches,
      branchId: validBranchId,
      branch: validBranchId ? (byId.get(validBranchId) ?? null) : null,
      setBranchId,
      timezoneOf: (id) => (id && byId.get(id)?.timezone) || DEFAULT_TIMEZONE,
      nameOf: (id) => (id && byId.get(id)?.name) || "",
    };
  }, [branches, validBranchId, setBranchId]);
  return <BranchContext.Provider value={value}>{children}</BranchContext.Provider>;
}

export function useBranches(): Branches {
  const value = useContext(BranchContext);
  if (!value) throw new Error("useBranches outside BranchProvider");
  return value;
}

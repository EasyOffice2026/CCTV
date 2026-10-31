"use client";

import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import {
  api,
  noteActivity,
  onTokenChange,
  refreshAccessToken,
  setAccessToken,
  setTenantId,
  unwrap,
  type Schemas,
} from "@/lib/api";
import type { Me } from "@/lib/permissions";
import { readStored, writeStored } from "@/lib/storage";

type Status = "loading" | "anonymous" | "signed-in";

type Auth = {
  status: Status;
  me: Me | null;
  /** The tenant acted on: the user's own, or the one a super-admin picked. */
  tenantId: string | null;
  setTenant: (tenantId: string | null) => void;
  signedIn: (tokens: Schemas["TokenPair"]) => Promise<void>;
  signOut: () => Promise<void>;
};

const AuthContext = createContext<Auth | null>(null);
const TENANT_KEY = "cctv.tenant";


export function AuthProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<Status>("loading");
  const [me, setMe] = useState<Me | null>(null);
  const [pickedTenant, setPickedTenant] = useState<string | null>(null);

  const loadMe = useCallback(async () => {
    const current = await unwrap(api.GET("/v1/auth/me"));
    if (current.role === "super_admin") {
      const tenant = readStored(TENANT_KEY);
      setTenantId(tenant);
      setPickedTenant(tenant);
    } else {
      setTenantId(null);
    }
    setMe(current);
    setStatus("signed-in");
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const ok = await refreshAccessToken();
      if (cancelled) return;
      if (!ok) {
        setStatus("anonymous");
        return;
      }
      try {
        await loadMe();
      } catch {
        if (!cancelled) setStatus("anonymous");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [loadMe]);

  // The session ended (refresh refused): back to the login page.
  useEffect(
    () =>
      onTokenChange((token) => {
        if (token === null) {
          setMe(null);
          setStatus("anonymous");
        }
      }),
    [],
  );

  useEffect(() => {
    const events = ["pointerdown", "keydown", "wheel", "touchstart"] as const;
    events.forEach((name) => window.addEventListener(name, noteActivity, { passive: true }));
    return () => events.forEach((name) => window.removeEventListener(name, noteActivity));
  }, []);

  const signedIn = useCallback(
    async (tokens: Schemas["TokenPair"]) => {
      setAccessToken(tokens.access_token, tokens.expires_in);
      queryClient.clear();
      await loadMe();
    },
    [loadMe, queryClient],
  );

  const signOut = useCallback(async () => {
    try {
      await api.POST("/v1/auth/logout");
    } finally {
      setAccessToken(null);
      queryClient.clear();
    }
  }, [queryClient]);

  const setTenant = useCallback(
    (tenant: string | null) => {
      writeStored(TENANT_KEY, tenant);
      setTenantId(tenant);
      setPickedTenant(tenant);
      queryClient.clear();
    },
    [queryClient],
  );

  const tenantId = me?.role === "super_admin" ? pickedTenant : (me?.tenant_id ?? null);
  const value = useMemo<Auth>(
    () => ({ status, me, tenantId, setTenant, signedIn, signOut }),
    [status, me, tenantId, setTenant, signedIn, signOut],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): Auth {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth outside AuthProvider");
  return value;
}

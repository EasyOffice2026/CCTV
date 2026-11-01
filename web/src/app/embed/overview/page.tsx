"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { OwnerOverview } from "@/components/OwnerOverview";
import { ErrorText, Notice, Select, Spinner } from "@/components/ui";
import { api, unwrap } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useBranchList } from "@/lib/branches";
import { takeHandoff, tellParent } from "@/lib/handoff";
import { useI18n } from "@/lib/i18n";
import { can } from "@/lib/permissions";

type State = "starting" | "signing-in" | "ready" | "need-token" | "failed";

/**
 * The owner overview for the operational system's own pages (plan 8.2), in an iframe:
 *
 *   <iframe src="https://cctv.example/embed/overview/#token=<sign-in token>&lang=ar">
 *
 * The page may also be given a token later with
 * `iframe.contentWindow.postMessage({ type: "cctv:token", token }, "https://cctv.example")`,
 * e.g. after it reports `cctv:need-token` or `cctv:session-ended`. It reports its height
 * (`cctv:height`) so the frame can fit it. Only sites allowed in the dashboard's
 * `frame-ancestors` can show it (docs/integration/ops-system.md).
 */
export default function EmbeddedOverview() {
  const { t, setLocale } = useI18n();
  const { status, me, signedIn } = useAuth();
  const branches = useBranchList();
  const [state, setState] = useState<State>("starting");
  const [error, setError] = useState<unknown>(null);
  const [branchId, setBranchId] = useState<string | null>(null);
  const started = useRef(false);

  const exchange = useCallback(
    async (token: string) => {
      setState("signing-in");
      setError(null);
      try {
        await signedIn(await unwrap(api.POST("/v1/auth/sso", { body: { token, embedded: true } })), true);
        setState("ready");
        tellParent({ type: "cctv:signed-in" });
      } catch (e) {
        setError(e);
        setState("failed");
        tellParent({ type: "cctv:sign-in-failed" });
      }
    },
    [signedIn],
  );

  // Start once the cookie check is done (a refresh failing later would sign us out).
  // The fragment is read here, not during render: the page is prerendered without it.
  useEffect(() => {
    if (status === "loading" || started.current) return;
    started.current = true;
    const params = takeHandoff();
    const lang = params.get("lang");
    if (lang === "en" || lang === "ar") setLocale(lang);
    setBranchId(params.get("branch"));
    const token = params.get("token");
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (token) void exchange(token);
    else if (status === "signed-in") setState("ready");
    else {
      setState("need-token");
      tellParent({ type: "cctv:need-token" });
    }
  }, [status, exchange, setLocale]);

  // A token from the page around us.
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      const data = event.data as { type?: unknown; token?: unknown } | null;
      if (event.source !== window.parent || data?.type !== "cctv:token" || typeof data.token !== "string") return;
      void exchange(data.token);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [exchange]);

  // The session ended (idle, signed out elsewhere, user deactivated).
  const ended = state === "ready" && status === "anonymous";
  useEffect(() => {
    if (ended) tellParent({ type: "cctv:session-ended" });
  }, [ended]);

  // Let the frame fit the page.
  useEffect(() => {
    const observer = new ResizeObserver(() => tellParent({ type: "cctv:height", height: document.documentElement.scrollHeight }));
    observer.observe(document.body);
    return () => observer.disconnect();
  }, []);

  let content: React.ReactNode;
  if (state === "ready" && status === "signed-in") {
    content = can(me, "kpis_view") ? (
      <OwnerOverview branchId={branchId} embedded />
    ) : (
      <Notice tone="amber">{t("common.forbidden")}</Notice>
    );
  } else if (state === "failed") {
    content = (
      <div className="flex flex-col gap-2">
        <ErrorText error={error} />
        <Notice tone="amber">{t("embed.failed")}</Notice>
      </div>
    );
  } else if (state === "need-token" || ended) {
    content = <Notice tone="amber">{t(ended ? "embed.ended" : "embed.needToken")}</Notice>;
  } else {
    content = <Spinner label={t("sso.signingIn")} />;
  }

  return (
    <main className="mx-auto flex max-w-6xl flex-col gap-3 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-semibold text-slate-700">
          {t("app.name")} · {t("overview.title")}
        </p>
        {state === "ready" && (branches.data?.length ?? 0) > 1 && (
          <Select
            aria-label={t("common.branch")}
            value={branchId ?? ""}
            onChange={(e) => setBranchId(e.target.value || null)}
            className="w-auto"
          >
            <option value="">{t("shell.allBranches")}</option>
            {branches.data?.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
              </option>
            ))}
          </Select>
        )}
      </div>
      {content}
    </main>
  );
}

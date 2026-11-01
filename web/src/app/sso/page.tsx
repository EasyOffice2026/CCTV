"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { AuthCard, safeNext } from "@/components/AuthCard";
import { ErrorText, Spinner } from "@/components/ui";
import { api, unwrap } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { takeHandoff } from "@/lib/handoff";
import { useI18n } from "@/lib/i18n";

/** Opened from the operational system with `#token=…` (plan 8.1): signs in without a
 * password and goes on to `next` (or the user's home page). */
export default function SsoPage() {
  const { t, setLocale } = useI18n();
  const { status, signedIn } = useAuth();
  const router = useRouter();
  const [error, setError] = useState<unknown>(null);
  const started = useRef(false);

  // The fragment is read here, not during render: the page is prerendered without it.
  useEffect(() => {
    if (status === "loading" || started.current) return;
    started.current = true;
    const params = takeHandoff();
    const lang = params.get("lang");
    if (lang === "en" || lang === "ar") setLocale(lang);
    const token = params.get("token");
    const next = safeNext(params.get("next")) ?? "/";
    if (!token) {
      if (status === "signed-in") router.replace(next);
      // eslint-disable-next-line react-hooks/set-state-in-effect
      else setError(new Error(t("sso.noToken")));
      return;
    }
    void (async () => {
      try {
        await signedIn(await unwrap(api.POST("/v1/auth/sso", { body: { token, embedded: false } })));
        router.replace(next);
      } catch (e) {
        setError(e);
      }
    })();
  }, [status, signedIn, router, setLocale, t]);

  return (
    <AuthCard title={t("sso.title")}>
      {error ? (
        <div className="flex flex-col gap-3 text-sm">
          <ErrorText error={error} />
          <p className="text-slate-600">{t("sso.failed")}</p>
          <Link className="text-brand-700 hover:underline" href="/login/">
            {t("sso.passwordInstead")}
          </Link>
        </div>
      ) : (
        <Spinner label={t("sso.signingIn")} />
      )}
    </AuthCard>
  );
}

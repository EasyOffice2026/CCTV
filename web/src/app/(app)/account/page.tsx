"use client";

import { useState } from "react";

import { Button, Card, ErrorText, Field, Input, Notice, PageHeader } from "@/components/ui";
import { api, unwrap } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useI18n } from "@/lib/i18n";
import { installApp, useInstallState } from "@/lib/install";
import { key } from "@/lib/labels";

/** Install the dashboard as an app (home-screen icon, own window, alert notifications). */
function PhoneApp() {
  const { t } = useI18n();
  const state = useInstallState();
  return (
    <Card className="flex flex-col gap-2 p-4 text-sm md:col-span-2">
      <h2 className="font-semibold">{t("app.title")}</h2>
      <p className="text-slate-600">{t("app.help")}</p>
      {state === "installed" && <Notice tone="green">{t("app.installed")}</Notice>}
      {state === "available" && (
        <div>
          <Button variant="primary" onClick={() => void installApp()}>
            {t("app.install")}
          </Button>
        </div>
      )}
      {state === "ios" && <Notice>{t("app.ios")}</Notice>}
      {state === "unavailable" && <Notice>{t("app.other")}</Notice>}
      <p className="text-xs text-slate-500">{t("app.alerts")}</p>
    </Card>
  );
}

export default function AccountPage() {
  const { t } = useI18n();
  const { me } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState(false);

  return (
    <>
      <PageHeader title={t("account.title")} />
      <div className="grid max-w-3xl gap-4 md:grid-cols-2">
        <Card className="flex flex-col gap-2 p-4 text-sm">
          <p className="font-medium">{me?.full_name}</p>
          <p dir="ltr" className="text-start text-slate-600">
            {me?.email}
          </p>
          <p className="text-slate-600">{me && t(key.role(me.role))}</p>
          <Notice tone={me?.mfa_enabled ? "green" : "amber"}>{me?.mfa_enabled ? t("account.mfaOn") : t("account.mfaOff")}</Notice>
        </Card>
        <Card className="p-4">
          <h2 className="mb-3 font-semibold">{t("account.changePassword")}</h2>
          <form
            className="flex flex-col gap-3"
            onSubmit={async (e) => {
              e.preventDefault();
              setError(null);
              setDone(false);
              if (next !== repeat) {
                setError(new Error(t("auth.passwordMismatch")));
                return;
              }
              setBusy(true);
              try {
                await unwrap(api.POST("/v1/auth/password/change", { body: { current_password: current, new_password: next } }));
                setDone(true);
                setCurrent("");
                setNext("");
                setRepeat("");
              } catch (err) {
                setError(err);
              } finally {
                setBusy(false);
              }
            }}
          >
            <Field label={t("account.currentPassword")}>
              {(id) => <Input id={id} type="password" autoComplete="current-password" required value={current} onChange={(e) => setCurrent(e.target.value)} />}
            </Field>
            <Field label={t("auth.newPassword")} hint={t("auth.passwordRules")}>
              {(id) => (
                <Input id={id} type="password" autoComplete="new-password" minLength={10} required value={next} onChange={(e) => setNext(e.target.value)} />
              )}
            </Field>
            <Field label={t("auth.confirmPassword")}>
              {(id) => (
                <Input id={id} type="password" autoComplete="new-password" minLength={10} required value={repeat} onChange={(e) => setRepeat(e.target.value)} />
              )}
            </Field>
            <ErrorText error={error} />
            {done && <Notice tone="green">{t("account.passwordChanged")}</Notice>}
            <Button type="submit" variant="primary" disabled={busy}>
              {t("common.save")}
            </Button>
          </form>
        </Card>
        <PhoneApp />
      </div>
    </>
  );
}

"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { AuthCard } from "@/components/AuthCard";
import { Button, ErrorText, Field, Input, Notice, Spinner } from "@/components/ui";
import { api, unwrap } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

/** Target of the password-reset and invitation emails: /set-password?token=... */
function SetPasswordForm() {
  const { t } = useI18n();
  const token = useSearchParams().get("token");
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  if (!token) return <Notice tone="amber">{t("auth.missingToken")}</Notice>;
  if (done) return <Notice tone="green">{t("auth.passwordSet")}</Notice>;

  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={async (e) => {
        e.preventDefault();
        if (password !== repeat) {
          setError(new Error(t("auth.passwordMismatch")));
          return;
        }
        setBusy(true);
        setError(null);
        try {
          await unwrap(api.POST("/v1/auth/password/reset", { body: { token, new_password: password } }));
          setDone(true);
        } catch (err) {
          setError(err);
        } finally {
          setBusy(false);
        }
      }}
    >
      <Field label={t("auth.newPassword")} hint={t("auth.passwordRules")}>
        {(id) => (
          <Input
            id={id}
            type="password"
            autoComplete="new-password"
            minLength={10}
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        )}
      </Field>
      <Field label={t("auth.confirmPassword")}>
        {(id) => (
          <Input
            id={id}
            type="password"
            autoComplete="new-password"
            minLength={10}
            required
            value={repeat}
            onChange={(e) => setRepeat(e.target.value)}
          />
        )}
      </Field>
      <ErrorText error={error} />
      <Button type="submit" variant="primary" disabled={busy}>
        {t("common.save")}
      </Button>
    </form>
  );
}

export default function SetPasswordPage() {
  const { t } = useI18n();
  return (
    <AuthCard title={t("auth.setPasswordTitle")}>
      <Suspense fallback={<Spinner />}>
        <SetPasswordForm />
      </Suspense>
      <Link href="/login/" className="mt-4 block text-center text-sm text-brand-600 hover:underline">
        {t("auth.backToLogin")}
      </Link>
    </AuthCard>
  );
}

"use client";

import Link from "next/link";
import { useState } from "react";

import { AuthCard } from "@/components/AuthCard";
import { Button, ErrorText, Field, Input, Notice } from "@/components/ui";
import { api, unwrap } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

export default function ForgotPasswordPage() {
  const { t } = useI18n();
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  return (
    <AuthCard title={t("auth.forgotTitle")}>
      {sent ? (
        <Notice tone="green">{t("auth.forgotSent")}</Notice>
      ) : (
        <form
          className="flex flex-col gap-3"
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError(null);
            try {
              await unwrap(api.POST("/v1/auth/password/forgot", { body: { email } }));
              setSent(true);
            } catch (err) {
              setError(err);
            } finally {
              setBusy(false);
            }
          }}
        >
          <p className="text-sm text-slate-600">{t("auth.forgotHelp")}</p>
          <Field label={t("auth.email")}>
            {(id) => <Input id={id} type="email" required value={email} onChange={(e) => setEmail(e.target.value)} />}
          </Field>
          <ErrorText error={error} />
          <Button type="submit" variant="primary" disabled={busy}>
            {t("auth.forgotSend")}
          </Button>
        </form>
      )}
      <Link href="/login/" className="mt-4 block text-center text-sm text-brand-600 hover:underline">
        {t("auth.backToLogin")}
      </Link>
    </AuthCard>
  );
}

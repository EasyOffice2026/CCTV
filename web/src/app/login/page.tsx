"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import QRCode from "qrcode";
import { Suspense, useEffect, useState } from "react";

import { homePath } from "@/components/AppShell";
import { AuthCard, safeNext } from "@/components/AuthCard";
import { Button, ErrorText, Field, Input, Spinner } from "@/components/ui";
import { api, unwrap, type Schemas } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useI18n } from "@/lib/i18n";

type Step =
  | { kind: "password" }
  | { kind: "verify"; token: string }
  | { kind: "setup"; setup: Schemas["MfaSetup"]; qr: string };

function LoginForm() {
  const { t } = useI18n();
  const { status, me, signedIn } = useAuth();
  const router = useRouter();
  const next = safeNext(useSearchParams().get("next"));
  const [step, setStep] = useState<Step>({ kind: "password" });
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    if (status === "signed-in") router.replace(next ?? homePath(me));
  }, [status, me, next, router]);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  const submitPassword = () =>
    run(async () => {
      const result = await unwrap(api.POST("/v1/auth/login", { body: { email, password } }));
      if ("access_token" in result) return signedIn(result);
      if (result.mfa_required === "verify") {
        setStep({ kind: "verify", token: result.mfa_token });
        return;
      }
      const setup = await unwrap(api.POST("/v1/auth/mfa/setup", { body: { token: result.mfa_token } }));
      const qr = await QRCode.toDataURL(setup.otpauth_uri, { margin: 1, width: 200 });
      setStep({ kind: "setup", setup, qr });
    });

  const submitCode = () =>
    run(async () => {
      if (step.kind === "verify") {
        await signedIn(await unwrap(api.POST("/v1/auth/mfa/verify", { body: { token: step.token, code } })));
      } else if (step.kind === "setup") {
        await signedIn(
          await unwrap(api.POST("/v1/auth/mfa/enable", { body: { token: step.setup.setup_token, code } })),
        );
      }
    });

  if (status === "loading") return <Spinner />;

  if (step.kind === "password") {
    return (
      <AuthCard title={t("auth.signIn")}>
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            void submitPassword();
          }}
        >
          <Field label={t("auth.email")}>
            {(id) => (
              <Input id={id} type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
            )}
          </Field>
          <Field label={t("auth.password")}>
            {(id) => (
              <Input
                id={id}
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            )}
          </Field>
          <ErrorText error={error} />
          <Button type="submit" variant="primary" disabled={busy}>
            {t("auth.signIn")}
          </Button>
          <Link href="/forgot-password/" className="text-center text-sm text-brand-600 hover:underline">
            {t("auth.forgotLink")}
          </Link>
        </form>
      </AuthCard>
    );
  }

  return (
    <AuthCard title={step.kind === "setup" ? t("auth.mfaSetupTitle") : t("auth.mfaTitle")}>
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          void submitCode();
        }}
      >
        {step.kind === "setup" ? (
          <>
            <p className="text-sm text-slate-600">{t("auth.mfaSetupHelp")}</p>
            {/* eslint-disable-next-line @next/next/no-img-element -- generated data URL */}
            <img src={step.qr} alt="QR code" width={200} height={200} className="mx-auto" />
            <p className="text-xs text-slate-500">{t("auth.mfaSecret")}</p>
            <code dir="ltr" className="rounded bg-slate-100 p-2 text-center text-sm break-all select-all">
              {step.setup.secret}
            </code>
          </>
        ) : (
          <p className="text-sm text-slate-600">{t("auth.mfaHelp")}</p>
        )}
        <Field label={t("auth.mfaCode")}>
          {(id) => (
            <Input
              id={id}
              dir="ltr"
              inputMode="numeric"
              autoComplete="one-time-code"
              pattern="[0-9 ]{6,8}"
              required
              autoFocus
              value={code}
              onChange={(e) => setCode(e.target.value)}
            />
          )}
        </Field>
        <ErrorText error={error} />
        <Button type="submit" variant="primary" disabled={busy}>
          {step.kind === "setup" ? t("auth.enable") : t("auth.verify")}
        </Button>
        <Button
          variant="ghost"
          onClick={() => {
            setStep({ kind: "password" });
            setCode("");
          }}
        >
          {t("auth.backToLogin")}
        </Button>
      </form>
    </AuthCard>
  );
}

export default function LoginPage() {
  return (
    <Suspense fallback={<Spinner />}>
      <LoginForm />
    </Suspense>
  );
}

"use client";

import { Card } from "@/components/ui";
import { useI18n } from "@/lib/i18n";

/** Frame for the signed-out pages (login, password reset). */
export function AuthCard({ title, children }: { title: string; children: React.ReactNode }) {
  const { t, locale, setLocale } = useI18n();
  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex items-center justify-between">
          <div>
            <p className="text-lg font-semibold text-slate-900">{t("app.name")}</p>
            <p className="text-xs text-slate-500">{t("app.tagline")}</p>
          </div>
          <button
            className="rounded-md px-2 py-1 text-sm text-slate-600 hover:bg-slate-100"
            onClick={() => setLocale(locale === "en" ? "ar" : "en")}
          >
            {t("shell.language")}
          </button>
        </div>
        <Card className="p-6">
          <h1 className="mb-4 text-lg font-semibold">{title}</h1>
          {children}
        </Card>
      </div>
    </div>
  );
}

/** Only same-site paths are followed after login. */
export function safeNext(next: string | null): string | null {
  // Same-origin paths only ("//host" and "/\host" are other sites to a browser).
  return next && next.startsWith("/") && !/^\/[/\\]/.test(next) && !next.startsWith("/login") ? next : null;
}

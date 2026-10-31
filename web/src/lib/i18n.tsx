"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useSyncExternalStore } from "react";

import { ar } from "@/lib/messages/ar";
import { en, type MessageKey } from "@/lib/messages/en";
import { useStoredValue, writeStored } from "@/lib/storage";

export type Locale = "en" | "ar";

const MESSAGES: Record<Locale, Record<MessageKey, string>> = { en, ar };
const STORAGE_KEY = "cctv.locale";

/** Replace {name} placeholders. */
export function translate(
  locale: Locale,
  key: MessageKey,
  vars?: Record<string, string | number>,
): string {
  const text = MESSAGES[locale][key] ?? en[key] ?? key;
  return vars ? text.replace(/\{(\w+)\}/g, (_, name: string) => String(vars[name] ?? `{${name}}`)) : text;
}

/** Intl locale: Kuwait Arabic with Latin digits, which matches the NVRs and receipts. */
export function intlLocale(locale: Locale): string {
  return locale === "ar" ? "ar-KW-u-nu-latn" : "en-GB";
}

type I18n = {
  locale: Locale;
  dir: "ltr" | "rtl";
  setLocale: (locale: Locale) => void;
  t: (key: MessageKey, vars?: Record<string, string | number>) => string;
};

const I18nContext = createContext<I18n | null>(null);

const browserLocale = (): Locale => (navigator.language.toLowerCase().startsWith("ar") ? "ar" : "en");
const noSubscription = () => () => {};

export function I18nProvider({ children }: { children: React.ReactNode }) {
  const stored = useStoredValue(STORAGE_KEY);
  // Hydration-safe: "en" in the prerendered HTML, the browser's language after.
  const fallback = useSyncExternalStore(noSubscription, browserLocale, (): Locale => "en");
  const locale: Locale = stored === "en" || stored === "ar" ? stored : fallback;

  useEffect(() => {
    document.documentElement.lang = locale;
    document.documentElement.dir = locale === "ar" ? "rtl" : "ltr";
  }, [locale]);

  const setLocale = useCallback((next: Locale) => writeStored(STORAGE_KEY, next), []);

  const value = useMemo<I18n>(
    () => ({
      locale,
      dir: locale === "ar" ? "rtl" : "ltr",
      setLocale,
      t: (key, vars) => translate(locale, key, vars),
    }),
    [locale, setLocale],
  );
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18n {
  const value = useContext(I18nContext);
  if (!value) throw new Error("useI18n outside I18nProvider");
  return value;
}

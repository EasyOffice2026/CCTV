"use client";

import { useSyncExternalStore } from "react";

/**
 * Small per-browser preferences (language, chosen branch) in localStorage, with an
 * in-memory fallback when storage is blocked, so the choice still works for the session.
 */
const memory = new Map<string, string | null>();
const listeners = new Set<() => void>();

export function readStored(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return memory.get(key) ?? null;
  }
}

export function writeStored(key: string, value: string | null): void {
  memory.set(key, value);
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    // blocked: the in-memory value is used
  }
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  window.addEventListener("storage", listener); // other tabs
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", listener);
  };
}

/** The stored value; null while prerendering and when unset. */
export function useStoredValue(key: string): string | null {
  return useSyncExternalStore(
    subscribe,
    () => readStored(key),
    () => null,
  );
}

"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError } from "@/lib/api";
import { AuthProvider } from "@/lib/auth";
import { BranchProvider } from "@/lib/branches";
import { I18nProvider } from "@/lib/i18n";

export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            // Retry server/network trouble, not "not allowed" or "not found".
            retry: (count, error) => count < 2 && !(error instanceof ApiError && error.status < 500),
          },
        },
      }),
  );
  return (
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <AuthProvider>
          <BranchProvider>{children}</BranchProvider>
        </AuthProvider>
      </I18nProvider>
    </QueryClientProvider>
  );
}

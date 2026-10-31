import type { Metadata } from "next";

import { Providers } from "@/components/Providers";

import "./globals.css";

export const metadata: Metadata = {
  title: "AI CCTV",
  description: "Restaurant monitoring dashboard",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    // lang/dir follow the chosen language on the client (see lib/i18n.tsx).
    <html lang="en" dir="ltr" suppressHydrationWarning>
      <body className="min-h-screen antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}

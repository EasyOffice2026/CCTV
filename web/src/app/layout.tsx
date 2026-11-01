import type { Metadata, Viewport } from "next";

import { Providers } from "@/components/Providers";

import "./globals.css";

export const metadata: Metadata = {
  title: "AI CCTV",
  description: "Restaurant monitoring dashboard",
  robots: { index: false, follow: false },
  // Installable as an app on phones and computers (lib/install.ts, public/manifest.json).
  manifest: "/manifest.json",
  icons: { icon: "/icons/favicon-32.png", apple: "/icons/apple-touch-icon.png" },
  appleWebApp: { capable: true, title: "AI CCTV", statusBarStyle: "default" },
};

export const viewport: Viewport = {
  themeColor: "#2259c2",
  width: "device-width",
  initialScale: 1,
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

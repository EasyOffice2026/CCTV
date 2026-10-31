import type { NextConfig } from "next";

// A static single-page app: `next build` writes plain files to out/, served by nginx in
// production (see Dockerfile), which also proxies /v1 to the API on the same origin.
const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  images: { unoptimized: true },
  reactStrictMode: true,
  poweredByHeader: false,
};

export default nextConfig;

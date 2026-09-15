import type { NextConfig } from "next";

const apiBase = process.env.ASSARIUM_API_URL ?? "http://127.0.0.1:8000";

const config: NextConfig = {
  reactStrictMode: true,
  // Proxy the API in development so the browser sees one origin and no CORS preflight.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiBase}/api/:path*` }];
  },
};

export default config;

import type { NextConfig } from "next";

const config: NextConfig = {
  poweredByHeader: false,
  async rewrites() {
    const origin = process.env.API_ORIGIN || "http://127.0.0.1:8000";
    return [
      { source: "/api/:path*", destination: `${origin}/api/:path*` },
      { source: "/mcp", destination: `${origin}/mcp` },
      { source: "/oauth/:path*", destination: `${origin}/oauth/:path*` },
      { source: "/.well-known/:path*", destination: `${origin}/.well-known/:path*` },
    ];
  },
  async headers() {
    return [{ source: "/:path*", headers: [
      { key: "Referrer-Policy", value: "no-referrer" },
      { key: "X-Content-Type-Options", value: "nosniff" },
      { key: "X-Frame-Options", value: "DENY" },
    ] }];
  },
};
export default config;

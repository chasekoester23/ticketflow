/**
 * Security headers for every page the browser loads from the frontend.
 * (API responses get their own headers from the FastAPI app.)
 *
 * - frame-ancestors / X-Frame-Options: no other site can embed TicketFlow
 *   in a frame to trick someone into clicking "Accept" (clickjacking).
 * - object-src / base-uri / form-action: close plugin, <base> and form-post
 *   injection paths. Script and connect sources are deliberately not
 *   restricted yet: Next.js inline bootstrapping needs nonces, which is a
 *   separate change to make together with the hosted deployment.
 *
 * @type {import('next').NextConfig}
 */
const securityHeaders = [
  {
    key: "Content-Security-Policy",
    value: "frame-ancestors 'none'; object-src 'none'; base-uri 'self'; form-action 'self'",
  },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=()" },
];

export default {
  poweredByHeader: false,
  // A self-contained server in .next/standalone, for the production image only
  // (frontend/Dockerfile sets NEXT_OUTPUT). `next dev` and `next start` are unchanged.
  ...(process.env.NEXT_OUTPUT === "standalone" ? { output: "standalone" } : {}),
  async headers() {
    // /api/... is the same-origin proxy (app/api/[...path]/route.ts). Those responses carry
    // the backend's own headers, so the page headers here aren't added on top of them.
    return [{ source: "/:path((?!api/).*)", headers: securityHeaders }];
  },
};

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
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

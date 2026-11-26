/**
 * Marketing-only deployments (NEXT_PUBLIC_SITE_MODE=marketing, set at build time) serve the
 * public website and nothing else: the homepage at "/", the demo request page and its form
 * handler, and static files. Every application page redirects home, and every other /api/
 * path is refused, so the private application is unreachable even though its code ships
 * in the same build. Without the setting this middleware does nothing.
 */
import { NextResponse, type NextRequest } from "next/server";

const MARKETING = process.env.NEXT_PUBLIC_SITE_MODE === "marketing";
const PAGES = new Set(["/welcome", "/demo"]);
const STATIC = /^\/(_next\/|brand\/|site\/|favicon\.ico$|icon\.png$|apple-icon\.png$|robots\.txt$)/;

export function middleware(request: NextRequest) {
  if (!MARKETING) return NextResponse.next();
  const { pathname } = request.nextUrl;
  if (pathname === "/") return NextResponse.rewrite(new URL("/welcome", request.url));
  if (PAGES.has(pathname) || STATIC.test(pathname) || pathname === "/api/demo-request") return NextResponse.next();
  if (pathname.startsWith("/api/")) return NextResponse.json({ detail: "Not found" }, { status: 404 });
  return NextResponse.redirect(new URL("/", request.url));
}

export const config = { matcher: "/:path*" };

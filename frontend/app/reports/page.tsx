"use client";

import { useEffect } from "react";

/** Reports became Analytics; old links and bookmarks land there. */
export default function ReportsMoved() {
  useEffect(() => {
    const tab = new URLSearchParams(window.location.search).get("tab");
    const map: Record<string, string> = { material: "materials", trucking: "trucking", costs: "costs" };
    window.location.replace(tab && map[tab] ? `/analytics?tab=${map[tab]}` : "/analytics");
  }, []);
  return <p className="tf-empty" role="status">Reports moved to Analytics…</p>;
}

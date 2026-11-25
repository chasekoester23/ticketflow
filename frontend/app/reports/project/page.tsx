"use client";

import { useEffect } from "react";
import { useProject } from "../../project-context";

/** The project report lives in each project's workspace now; old links land there. */
export default function ProjectReportMoved() {
  const { projectId } = useProject();
  useEffect(() => {
    const wanted = new URLSearchParams(window.location.search).get("project_id") || projectId;
    window.location.replace(wanted && /^\d+$/.test(wanted) ? `/projects/${wanted}` : "/projects");
  }, [projectId]);
  return <p className="tf-empty" role="status">Opening the project report…</p>;
}

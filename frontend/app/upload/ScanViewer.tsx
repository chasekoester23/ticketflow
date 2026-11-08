"use client";

import { useEffect, useRef, useState, type PointerEvent } from "react";

const MIN_ZOOM = 1;
const MAX_ZOOM = 4;
export const zoomIn = (zoom: number) => Math.min(MAX_ZOOM, Math.round((zoom + 0.25) * 100) / 100);
export const zoomOut = (zoom: number) => Math.max(MIN_ZOOM, Math.round((zoom - 0.25) * 100) / 100);

/**
 * The original scan. Zoom, rotation and scroll position stay put from one ticket
 * to the next, because a stack from one supplier has the tons in the same place.
 * Click to zoom in on a spot, drag to pan. Rotation here is view-only; it never
 * touches the stored scan.
 */
export function ScanViewer({ src, alt, zoom, rotation, onZoom }: {
  src: string;
  alt: string;
  zoom: number;
  rotation: number;
  onZoom: (zoom: number) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; y: number; left: number; top: number; moved: boolean } | null>(null);
  const [boxWidth, setBoxWidth] = useState(0);
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => { setFailed(false); }, [src]);
  useEffect(() => {
    const element = box.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => setBoxWidth(element.clientWidth));
    observer.observe(element);
    setBoxWidth(element.clientWidth);
    return () => observer.disconnect();
  }, []);

  // Sideways scans would otherwise overflow their box, because CSS rotation
  // doesn't change an element's layout size.
  const sideways = rotation % 180 !== 0;
  const available = Math.max(0, boxWidth - 24) * zoom;
  const ratio = natural ? natural.h / natural.w : 1;
  const visual = natural && boxWidth
    ? { width: available, height: sideways ? available / ratio : available * ratio }
    : null;

  function onPointerDown(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0 || !box.current) return;
    drag.current = { x: event.clientX, y: event.clientY, left: box.current.scrollLeft, top: box.current.scrollTop, moved: false };
    box.current.setPointerCapture?.(event.pointerId);
  }
  function onPointerMove(event: PointerEvent<HTMLDivElement>) {
    const state = drag.current;
    if (!state || !box.current) return;
    const dx = event.clientX - state.x;
    const dy = event.clientY - state.y;
    if (Math.abs(dx) + Math.abs(dy) > 4) state.moved = true;
    if (state.moved) {
      box.current.scrollLeft = state.left - dx;
      box.current.scrollTop = state.top - dy;
    }
  }
  function onPointerUp(event: PointerEvent<HTMLDivElement>) {
    const state = drag.current;
    const element = box.current;
    drag.current = null;
    if (!state || state.moved || !element) return;
    if (zoom > 1) {
      onZoom(1);
      return;
    }
    // Zoom to 2.5x with the clicked spot staying where it was on screen.
    const rect = element.getBoundingClientRect();
    const pointX = event.clientX - rect.left;
    const pointY = event.clientY - rect.top;
    const contentX = element.scrollLeft + pointX;
    const contentY = element.scrollTop + pointY;
    onZoom(2.5);
    requestAnimationFrame(() => {
      element.scrollLeft = contentX * 2.5 - pointX;
      element.scrollTop = contentY * 2.5 - pointY;
    });
  }

  // A tab stop, so arrow keys can scroll a zoomed scan without a mouse.
  return <div ref={box} data-testid="scan-box" tabIndex={0} role="region" aria-label="Scanned ticket. When zoomed, arrow keys scroll it."
    onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp}
    className={`min-h-[320px] flex-1 touch-none select-none overflow-auto bg-[#e6e9eb] p-3 ${zoom > 1 ? "cursor-grab active:cursor-grabbing" : "cursor-zoom-in"}`}>
    {failed
      ? <p role="alert" className="m-4 rounded border border-red-200 bg-red-50 p-3 text-sm text-red-900">
        This scan could not be loaded. The stored image may be missing, or your session may have ended. Don&apos;t approve this ticket without seeing the original.
      </p>
      : <div className="relative mx-auto" style={visual ?? { width: "100%" }}>
        {/* eslint-disable-next-line @next/next/no-img-element -- authenticated API image */}
        <img src={src} alt={alt} draggable={false}
          onLoad={(event) => setNatural({ w: event.currentTarget.naturalWidth || 1, h: event.currentTarget.naturalHeight || 1 })}
          onError={() => setFailed(true)}
          className="max-w-none border border-slate-300 bg-white"
          style={visual
            ? {
              position: "absolute", left: "50%", top: "50%",
              width: sideways ? visual.height : visual.width,
              height: sideways ? visual.width : visual.height,
              transform: `translate(-50%, -50%) rotate(${rotation}deg)`,
            }
            : { width: "100%" }} />
      </div>}
  </div>;
}

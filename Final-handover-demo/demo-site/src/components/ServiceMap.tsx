"use client";

import { SERVICES, EDGES, W, H, LANG_COLOR, HEALTH_COLOR, HEALTH_GLOW } from "@/data/topology";
import type { Health, Latency } from "@/types";

type Props = {
  health: Health;
  latency: Latency;
  onContextMenu: (e: React.MouseEvent, serviceId: string) => void;
};

function cx(s: { x: number }) { return s.x + W / 2; }
function cy(s: { y: number }) { return s.y + H / 2; }

const svcMap = Object.fromEntries(SERVICES.map((s) => [s.id, s]));

export default function ServiceMap({ health, latency, onContextMenu }: Props) {
  return (
    <div
      style={{
        flex: "0 0 62%",
        display: "flex",
        flexDirection: "column",
        borderRight: "1px solid #1c2a3a",
        overflow: "hidden",
      }}
    >
      {/* Panel header */}
      <div
        style={{
          padding: "10px 16px",
          borderBottom: "1px solid #111a24",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          flexShrink: 0,
        }}
      >
        <span style={{ fontSize: 10, letterSpacing: 2, color: "#4a6070", textTransform: "uppercase" }}>
          Service Topology — OpenTelemetry Astronomy Shop
        </span>
        <span style={{ fontSize: 10, color: "#2a3a4a" }}>right-click any service to inject fault</span>
      </div>

      {/* Map area */}
      <div style={{ flex: 1, position: "relative", overflow: "hidden", padding: 8 }}>
        {/* Connection lines */}
        <svg
          style={{
            position: "absolute",
            inset: 0,
            width: "100%",
            height: "100%",
            pointerEvents: "none",
          }}
        >
          {EDGES.map(([a, b]) => {
            const sa = svcMap[a];
            const sb = svcMap[b];
            if (!sa || !sb) return null;
            const isCritical =
              (a === "checkout" || b === "checkout") &&
              (a === "payment" || b === "payment" || a === "productcatalog" || b === "productcatalog");
            const degraded =
              health[a] === "critical" || health[b] === "critical" ||
              health[a] === "warning"  || health[b] === "warning";
            const evicted = health[a] === "evicted" || health[b] === "evicted";
            return (
              <line
                key={`${a}-${b}`}
                x1={cx(sa) + 8} y1={cy(sa) + 8}
                x2={cx(sb) + 8} y2={cy(sb) + 8}
                stroke={evicted ? "#1a2030" : degraded ? "#ff4466" : isCritical ? "#00aa55" : "#1e3048"}
                strokeWidth={isCritical ? 1.5 : 1}
                strokeDasharray={evicted ? "4,4" : "none"}
                opacity={evicted ? 0.3 : 0.7}
              />
            );
          })}
        </svg>

        {/* Service cards */}
        {SERVICES.map((svc) => {
          const h = health[svc.id] || "healthy";
          const lat = latency[svc.id] || 0;
          const evicted = h === "evicted";
          return (
            <div
              key={svc.id}
              onContextMenu={(e) => onContextMenu(e, svc.id)}
              style={{
                position: "absolute",
                left: svc.x + 8,
                top: svc.y + 8,
                width: W,
                height: H,
                background: evicted ? "#0a0e14" : "#0d1622",
                border: `1px solid ${evicted ? "#1a2030" : HEALTH_COLOR[h]}`,
                borderRadius: 6,
                padding: "7px 9px",
                cursor: evicted ? "default" : "context-menu",
                opacity: evicted ? 0.35 : 1,
                boxShadow: evicted ? "none" : `0 0 ${h === "healthy" ? 6 : 12}px ${HEALTH_GLOW[h]}`,
                transition: "all .4s",
                userSelect: "none",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 5, marginBottom: 3 }}>
                <div
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: "50%",
                    flexShrink: 0,
                    background: HEALTH_COLOR[h],
                    animation:
                      h === "critical" ? "pulse-red 1s infinite" :
                      h === "warning"  ? "pulse-amber 1.5s infinite" : "none",
                  }}
                />
                <span
                  style={{
                    fontSize: 10,
                    fontWeight: 600,
                    color: evicted ? "#2a3a4a" : h === "healthy" ? "#a0c0d4" : "#e0e8f0",
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    maxWidth: 86,
                  }}
                >
                  {svc.name}
                </span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <span style={{ fontSize: 9, color: LANG_COLOR[svc.lang] || "#5a7a8a" }}>
                  {svc.lang}
                </span>
                <span
                  style={{
                    fontSize: 9,
                    color: evicted ? "#2a3a4a" : lat > 500 ? "#ff4466" : lat > 200 ? "#ffaa00" : "#4a7a5a",
                  }}
                >
                  {evicted ? "evicted" : lat > 999 ? "—" : `${lat}ms`}
                </span>
              </div>
              {svc.critical && !evicted && (
                <div
                  style={{
                    position: "absolute",
                    top: 3,
                    right: 5,
                    fontSize: 8,
                    color: h === "healthy" ? "#00aa55" : "#ff6688",
                    letterSpacing: 0.5,
                  }}
                >
                  CRITICAL
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Legend */}
      <div
        style={{
          padding: "8px 16px",
          borderTop: "1px solid #111a24",
          display: "flex",
          gap: 16,
          alignItems: "center",
          flexShrink: 0,
        }}
      >
        {Object.entries(HEALTH_COLOR).map(([k, v]) => (
          <div key={k} style={{ display: "flex", alignItems: "center", gap: 5 }}>
            <div style={{ width: 6, height: 6, borderRadius: "50%", background: v }} />
            <span style={{ fontSize: 9, color: "#4a6070", textTransform: "capitalize" }}>{k}</span>
          </div>
        ))}
        <span style={{ marginLeft: "auto", fontSize: 9, color: "#2a3a4a" }}>
          OTel Astronomy Shop — {SERVICES.length} services
        </span>
      </div>
    </div>
  );
}

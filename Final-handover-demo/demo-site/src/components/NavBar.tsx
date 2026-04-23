"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { Phase, Incident } from "@/types";

type Props = {
  healthCount: number;
  total: number;
  incident: Incident | null;
  phase: Phase;
  onSolve: () => void;
  onReset: () => void;
};

const NAV_LINKS = [
  { href: "/",        label: "SERVICE MAP" },
  { href: "/cluster", label: "CLUSTER"     },
  { href: "/agent",   label: "AGENT LOGS"  },
];

export default function NavBar({ healthCount, total, incident, phase, onSolve, onReset }: Props) {
  const pathname = usePathname();

  return (
    <div
      style={{
        background: "#0b0f1a",
        borderBottom: "1px solid #1c2a3a",
        padding: "0 20px",
        height: 52,
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        flexShrink: 0,
      }}
    >
      {/* Left: logo + nav */}
      <div style={{ display: "flex", alignItems: "center", gap: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginRight: 24 }}>
          <div
            style={{
              width: 8,
              height: 8,
              borderRadius: "50%",
              background: "#00e87a",
              boxShadow: "0 0 8px #00e87a",
            }}
          />
          <span style={{ color: "#7ab8d4", fontSize: 11, letterSpacing: 2, textTransform: "uppercase" }}>
            otel-demo-prod
          </span>
          <span style={{ color: "#2a3a4a", margin: "0 4px" }}>|</span>
          <span style={{ color: "#4a6070", fontSize: 11 }}>EKS ap-southeast-2</span>
        </div>

        {NAV_LINKS.map(({ href, label }) => {
          const active = pathname === href;
          return (
            <Link
              key={href}
              href={href}
              style={{
                padding: "0 14px",
                height: 52,
                display: "flex",
                alignItems: "center",
                fontSize: 10,
                letterSpacing: 1.5,
                color: active ? "#a0c8e0" : "#4a6070",
                borderBottom: active ? "2px solid #00aaff" : "2px solid transparent",
                transition: "color .15s",
              }}
            >
              {label}
            </Link>
          );
        })}
      </div>

      {/* Right: status + actions */}
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        {total > 0 && (
          <span style={{ color: "#4a6070", fontSize: 11, letterSpacing: 1 }}>
            {healthCount}/{total} healthy
          </span>
        )}

        {incident && (
          <button
            onClick={onSolve}
            disabled={phase !== "idle"}
            style={{
              background: phase !== "idle" ? "#0f1a24" : "#003388",
              border: "1px solid",
              borderColor: phase !== "idle" ? "#1c2a3a" : "#0055cc",
              color: phase !== "idle" ? "#334" : "#88bbff",
              padding: "6px 16px",
              borderRadius: 4,
              cursor: phase !== "idle" ? "not-allowed" : "pointer",
              fontSize: 12,
              letterSpacing: 1,
              boxShadow: phase === "idle" ? "0 0 12px #0044aa44" : "none",
              transition: "all .2s",
              fontFamily: "inherit",
            }}
          >
            {phase === "idle"          ? "⚡ SOLVE WITH AI"       :
             phase === "investigating" ? "⏳ INVESTIGATING..."    :
             phase === "awaiting"      ? "⏸ AWAITING APPROVAL"   :
             phase === "resolving"     ? "⚙ RESOLVING..."        : "✓ RESOLVED"}
          </button>
        )}

        <button
          onClick={onReset}
          style={{
            background: "transparent",
            border: "1px solid #1c2a3a",
            color: "#4a6070",
            padding: "6px 12px",
            borderRadius: 4,
            cursor: "pointer",
            fontSize: 11,
            letterSpacing: 1,
            fontFamily: "inherit",
          }}
        >
          RESET
        </button>
      </div>
    </div>
  );
}

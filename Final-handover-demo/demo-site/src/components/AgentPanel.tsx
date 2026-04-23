"use client";

import { useEffect, useRef } from "react";
import LogLine from "./LogLine";
import ApprovalCard from "./ApprovalCard";
import type { LogEntry, Phase } from "@/types";

type Props = {
  phase: Phase;
  logs: LogEntry[];
  showApproval: boolean;
  onApprove: () => void;
  onDeny: () => void;
};

export default function AgentPanel({ phase, logs, showApproval, onApprove, onDeny }: Props) {
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [logs, showApproval]);

  const statusColor =
    phase === "idle"     ? "#2a3a4a" :
    phase === "resolved" ? "#00e87a" : "#00aaff";

  return (
    <div
      style={{
        flex: 1,
        display: "flex",
        flexDirection: "column",
        background: "#060810",
        overflow: "hidden",
      }}
    >
      {/* Panel header */}
      <div
        style={{
          padding: "10px 16px",
          borderBottom: "1px solid #111a24",
          display: "flex",
          alignItems: "center",
          gap: 8,
          flexShrink: 0,
        }}
      >
        <div
          style={{
            width: 6,
            height: 6,
            borderRadius: "50%",
            background: statusColor,
            boxShadow: phase !== "idle" ? `0 0 8px ${statusColor}` : "none",
            animation: phase === "investigating" || phase === "resolving" ? "pulse-blue 2s infinite" : "none",
          }}
        />
        <span
          style={{ fontSize: 10, letterSpacing: 2, color: "#4a6070", textTransform: "uppercase" }}
        >
          Agent Activity
        </span>
        {phase !== "idle" && (
          <span
            style={{
              marginLeft: "auto",
              fontSize: 9,
              color:
                phase === "resolved" ? "#00aa55" :
                phase === "awaiting" ? "#ffaa00" : "#00aaff",
              letterSpacing: 1,
            }}
          >
            {phase.toUpperCase()}
          </span>
        )}
      </div>

      {/* Log stream */}
      <div
        ref={logRef}
        style={{
          flex: 1,
          overflowY: "auto",
          padding: "12px 14px",
          display: "flex",
          flexDirection: "column",
          gap: 4,
        }}
      >
        {phase === "idle" && logs.length === 0 && (
          <div
            style={{
              margin: "auto",
              textAlign: "center",
              color: "#1e2e3e",
            }}
          >
            <div style={{ fontSize: 32, marginBottom: 12 }}>⬡</div>
            <div style={{ fontSize: 11, letterSpacing: 1 }}>NO ACTIVE INVESTIGATION</div>
            <div style={{ fontSize: 10, marginTop: 6, color: "#1a2830" }}>
              Right-click a service to inject a fault
            </div>
          </div>
        )}

        {logs.map((log) => (
          <LogLine key={log.id} log={log} />
        ))}

        {showApproval && <ApprovalCard onApprove={onApprove} onDeny={onDeny} />}
      </div>
    </div>
  );
}

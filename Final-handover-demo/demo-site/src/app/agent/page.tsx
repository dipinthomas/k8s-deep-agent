"use client";

import { useState, useEffect, useRef } from "react";
import NavBar from "@/components/NavBar";
import LogLine from "@/components/LogLine";
import type { LogEntry, Phase } from "@/types";

export default function AgentPage() {
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [phase, setPhase] = useState<Phase>("idle");
  const [connected, setConnected] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);
  const esRef = useRef<EventSource | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const connect = () => {
    if (esRef.current) esRef.current.close();

    const es = new EventSource("/api/agent-stream");
    esRef.current = es;

    es.onopen = () => setConnected(true);

    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data) as Omit<LogEntry, "id">;

        if (data.type === "approval") {
          setPhase("awaiting");
          setLogs((p) => [
            ...p,
            { id: Math.random(), type: "approval", text: "⏸ AWAITING HUMAN APPROVAL" },
          ]);
        } else if (data.type === "resolved") {
          setPhase("resolved");
          setLogs((p) => [...p, { ...data, id: Math.random() }]);
        } else if (data.type === "phase") {
          setPhase((data as { type: string; value: Phase }).value as Phase);
        } else {
          setLogs((p) => [...p, { ...data, id: Math.random() }]);
          if (data.type === "agent" || data.type === "spawn") setPhase("investigating");
        }
      } catch {/* ignore */}
    };

    es.onerror = () => {
      setConnected(false);
      es.close();
      reconnectTimer.current = setTimeout(connect, 3000);
    };
  };

  useEffect(() => {
    connect();
    return () => {
      esRef.current?.close();
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-scroll
  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [logs]);

  const phaseColor: Record<Phase, string> = {
    idle: "#2a3a4a",
    investigating: "#00aaff",
    awaiting: "#ffaa00",
    resolving: "#ff8833",
    resolved: "#00e87a",
  };

  return (
    <div
      style={{
        height: "100vh",
        display: "flex",
        flexDirection: "column",
        background: "#04060d",
        color: "#c8d8e8",
        fontFamily: "'SF Mono','Fira Code',monospace",
      }}
    >
      <NavBar healthCount={0} total={0} incident={null} phase="idle" onSolve={() => {}} onReset={() => {}} />

      {/* Sub-header */}
      <div
        style={{
          padding: "10px 20px",
          borderBottom: "1px solid #111a24",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          background: "#060810",
          flexShrink: 0,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span
            style={{
              width: 6,
              height: 6,
              borderRadius: "50%",
              background: connected ? "#00e87a" : "#ff2244",
              boxShadow: connected ? "0 0 8px #00e87a" : "0 0 8px #ff2244",
            }}
          />
          <span
            style={{ fontSize: 10, letterSpacing: 2, color: "#4a6070", textTransform: "uppercase" }}
          >
            Agent Activity Stream
          </span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span
            style={{
              fontSize: 9,
              color: phaseColor[phase],
              letterSpacing: 1,
              textTransform: "uppercase",
            }}
          >
            {phase}
          </span>
          <button
            onClick={() => { setLogs([]); setPhase("idle"); }}
            style={{
              background: "transparent",
              border: "1px solid #1c2a3a",
              color: "#4a6070",
              padding: "4px 10px",
              borderRadius: 4,
              fontSize: 10,
              letterSpacing: 1,
            }}
          >
            CLEAR
          </button>
        </div>
      </div>

      {/* Terminal */}
      <div
        ref={logRef}
        style={{
          flex: 1,
          overflowY: "auto",
          padding: "16px 20px",
          display: "flex",
          flexDirection: "column",
          gap: 3,
        }}
      >
        {/* Static header */}
        <div style={{ color: "#1e3048", fontSize: 10, marginBottom: 8, lineHeight: 1.6 }}>
          <div>otel-demo-prod / EKS ap-southeast-2</div>
          <div>k8s-ai-agent — Deep Agents + LangGraph + Claude claude-sonnet-4-6</div>
          <div style={{ marginTop: 4, borderBottom: "1px solid #0d1622", paddingBottom: 8 }}>
            ─────────────────────────────────────────────────────
          </div>
        </div>

        {phase === "idle" && logs.length === 0 && (
          <div
            style={{
              margin: "auto",
              textAlign: "center",
              color: "#1e2e3e",
              paddingBottom: 60,
            }}
          >
            <div style={{ fontSize: 28, marginBottom: 12 }}>⬡</div>
            <div style={{ fontSize: 11, letterSpacing: 1 }}>WAITING FOR INCIDENT</div>
            <div style={{ fontSize: 10, marginTop: 6, color: "#1a2830" }}>
              inject a fault on the main page to begin
            </div>
          </div>
        )}

        {logs.map((log) => (
          <LogLine key={log.id} log={log} />
        ))}

        {/* Blinking cursor */}
        {phase === "investigating" || phase === "resolving" ? (
          <div
            style={{
              display: "inline-block",
              width: 7,
              height: 13,
              background: "#00aaff",
              marginTop: 2,
              animation: "blink 1s infinite",
            }}
          />
        ) : null}
      </div>

      {/* Phone QR hint */}
      <div
        style={{
          padding: "8px 20px",
          borderTop: "1px solid #0d1622",
          fontSize: 9,
          color: "#1a2830",
          display: "flex",
          justifyContent: "space-between",
        }}
      >
        <span>demo.1conceptonline.com/agent</span>
        <span>NZ Tech Rally 2026 · Wellington · 15 May</span>
      </div>
    </div>
  );
}

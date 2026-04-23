"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import NavBar from "@/components/NavBar";
import ServiceMap from "@/components/ServiceMap";
import AgentPanel from "@/components/AgentPanel";
import ContextMenu from "@/components/ContextMenu";
import IncidentBanner from "@/components/IncidentBanner";
import { SERVICES, FAULTS } from "@/data/topology";
import { DISK_SCRIPT, RESOLUTION_SCRIPT } from "@/data/scripts";
import type { Health, Latency, LogEntry, Phase, CtxMenuState, Incident } from "@/types";

function initHealth(): { h: Health; l: Latency } {
  const h: Health = {};
  const l: Latency = {};
  SERVICES.forEach((s) => {
    h[s.id] = "healthy";
    l[s.id] = s.critical
      ? 80 + Math.floor(Math.random() * 50)
      : 12 + Math.floor(Math.random() * 30);
  });
  return { h, l };
}

export default function HomePage() {
  const [{ h: health, l: latency }, setHl] = useState(initHealth);
  const [ctxMenu, setCtxMenu] = useState<CtxMenuState | null>(null);
  const [incident, setIncident] = useState<Incident | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [showApproval, setShowApproval] = useState(false);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const eventSource = useRef<EventSource | null>(null);
  const useRealApi = process.env.NEXT_PUBLIC_USE_REAL_API === "true";

  const clearTimers = useCallback(() => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
  }, []);

  const addLog = useCallback((entry: Omit<LogEntry, "id">) => {
    setLogs((p) => [...p, { ...entry, id: Math.random() }]);
  }, []);

  // Close context menu on outside click
  useEffect(() => {
    const close = () => setCtxMenu(null);
    window.addEventListener("click", close);
    return () => window.removeEventListener("click", close);
  }, []);

  const injectFault = useCallback(
    async (faultId: string) => {
      const svcId = ctxMenu!.serviceId;
      setCtxMenu(null);
      setIncident({ svcId, faultId });
      setHl(({ h, l }) => ({
        h: { ...h, [svcId]: "critical" },
        l: { ...l, [svcId]: faultId === "high_latency" ? 2400 : 9999 },
      }));

      if (faultId === "disk_pressure") {
        timers.current.push(
          setTimeout(() => {
            setHl(({ h, l }) => ({
              h: { ...h, checkout: "warning", payment: "warning" },
              l: { ...l, checkout: 890, payment: 720 },
            }));
          }, 2200)
        );
      }

      if (useRealApi) {
        try {
          await fetch("/api/inject-fault", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ service: svcId, type: faultId }),
          });
        } catch {
          // fall through — UI still shows simulated state
        }
      }
    },
    [ctxMenu, useRealApi]
  );

  const connectSSE = useCallback(() => {
    if (eventSource.current) eventSource.current.close();
    const es = new EventSource("/api/agent-stream");
    eventSource.current = es;

    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data) as Omit<LogEntry, "id">;
        if (data.type === "approval") {
          setShowApproval(true);
          setPhase("awaiting");
        } else if (data.type === "resolved") {
          setPhase("resolved");
          setHl(({ h, l }) => ({
            h: { ...h, checkout: "healthy", payment: "healthy" },
            l: { ...l, checkout: 118, payment: 95 },
          }));
          addLog(data);
        } else if (data.type === "evict") {
          const svc = (data as { type: string; service: string }).service;
          setHl(({ h, l }) => ({ h: { ...h, [svc]: "evicted" }, l }));
        } else {
          addLog(data);
        }
      } catch {/* ignore malformed events */}
    };

    es.onerror = () => es.close();
  }, [addLog]);

  const handleSolve = useCallback(() => {
    if (!incident || phase !== "idle") return;
    setPhase("investigating");
    setLogs([]);
    setShowApproval(false);

    if (useRealApi) {
      connectSSE();
      return;
    }

    // Simulated mode
    const script =
      incident.faultId === "disk_pressure" ? DISK_SCRIPT : DISK_SCRIPT;
    script.forEach(({ ms, ...entry }) => {
      const t = setTimeout(() => {
        if (entry.type === "approval") {
          setShowApproval(true);
          setPhase("awaiting");
        } else {
          addLog(entry as Omit<LogEntry, "id">);
        }
      }, ms);
      timers.current.push(t);
    });
  }, [incident, phase, useRealApi, connectSSE, addLog]);

  const handleApprove = useCallback(async () => {
    setShowApproval(false);
    setPhase("resolving");
    setHl(({ h, l }) => ({
      h: {
        ...h,
        loadgenerator: "evicted",
        imageprovider: "evicted",
        adservice: "evicted",
      },
      l,
    }));

    if (useRealApi) {
      try {
        await fetch("/api/approve", { method: "POST" });
      } catch {/* ignore */}
      return;
    }

    RESOLUTION_SCRIPT.forEach(({ ms, ...entry }) => {
      const t = setTimeout(() => {
        if (entry.type === "resolved") {
          setPhase("resolved");
          setHl(({ h, l }) => ({
            h: { ...h, checkout: "healthy", payment: "healthy" },
            l: { ...l, checkout: 118, payment: 95 },
          }));
        }
        addLog(entry as Omit<LogEntry, "id">);
      }, ms);
      timers.current.push(t);
    });
  }, [useRealApi, addLog]);

  const handleDeny = useCallback(() => {
    setShowApproval(false);
    setPhase("idle");
    addLog({ type: "agent", text: "Eviction denied by operator. Standing by." });
  }, [addLog]);

  const handleReset = useCallback(async () => {
    clearTimers();
    eventSource.current?.close();
    const { h, l } = initHealth();
    setHl({ h, l });
    setIncident(null);
    setPhase("idle");
    setLogs([]);
    setShowApproval(false);

    if (useRealApi) {
      try { await fetch("/api/reset", { method: "POST" }); } catch {/* ignore */}
    }
  }, [clearTimers, useRealApi]);

  const healthCount = SERVICES.filter((s) => health[s.id] === "healthy").length;

  return (
    <div
      style={{
        background: "#07090f",
        color: "#c8d8e8",
        height: "100vh",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
      }}
    >
      <NavBar
        healthCount={healthCount}
        total={SERVICES.length}
        incident={incident}
        phase={phase}
        onSolve={handleSolve}
        onReset={handleReset}
      />

      {incident && health[incident.svcId] === "critical" && phase === "idle" && (
        <IncidentBanner />
      )}

      <div style={{ display: "flex", flex: 1, overflow: "hidden" }}>
        <ServiceMap
          health={health}
          latency={latency}
          onContextMenu={(e, serviceId) => {
            e.preventDefault();
            if (health[serviceId] !== "evicted") {
              setCtxMenu({ x: e.clientX, y: e.clientY, serviceId });
            }
          }}
        />

        <AgentPanel
          phase={phase}
          logs={logs}
          showApproval={showApproval}
          onApprove={handleApprove}
          onDeny={handleDeny}
        />
      </div>

      {ctxMenu && (
        <ContextMenu
          x={ctxMenu.x}
          y={ctxMenu.y}
          serviceId={ctxMenu.serviceId}
          faults={FAULTS}
          onSelect={(faultId) => injectFault(faultId)}
        />
      )}
    </div>
  );
}

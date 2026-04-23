"use client";

import { useState, useEffect } from "react";
import NavBar from "@/components/NavBar";
import type { Phase } from "@/types";

type PodRow = {
  name: string;
  status: string;
  restarts: number;
  age: string;
  node: string;
  priority: string;
  critical: boolean;
};

const STATUS_COLOR: Record<string, string> = {
  Running: "#00e87a",
  Pending: "#ffaa00",
  Terminating: "#ff6644",
  CrashLoopBackOff: "#ff2244",
  Evicted: "#334455",
  Unknown: "#4a6070",
};

export default function ClusterPage() {
  const [pods, setPods] = useState<PodRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastRefresh, setLastRefresh] = useState<string>("");
  const headlampUrl = process.env.NEXT_PUBLIC_HEADLAMP_URL;

  const fetchPods = async () => {
    try {
      const res = await fetch("/api/cluster/pods");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setPods(data.pods || []);
      setLastRefresh(new Date().toLocaleTimeString());
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchPods();
    const id = setInterval(fetchPods, 3000);
    return () => clearInterval(id);
  }, []);

  const phase: Phase = "idle";

  // If Headlamp URL is configured, embed it
  if (headlampUrl) {
    return (
      <div style={{ height: "100vh", display: "flex", flexDirection: "column" }}>
        <NavBar healthCount={0} total={0} incident={null} phase={phase} onSolve={() => {}} onReset={() => {}} />
        <iframe
          src={headlampUrl}
          style={{ flex: 1, border: "none", background: "#07090f" }}
          title="Headlamp — Live Cluster View"
        />
      </div>
    );
  }

  // Fallback: custom pod table
  return (
    <div
      style={{
        height: "100vh",
        display: "flex",
        flexDirection: "column",
        background: "#07090f",
        color: "#c8d8e8",
        fontFamily: "'SF Mono','Fira Code',monospace",
      }}
    >
      <NavBar healthCount={0} total={0} incident={null} phase={phase} onSolve={() => {}} onReset={() => {}} />

      <div style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column" }}>
        {/* Sub-header */}
        <div
          style={{
            padding: "10px 20px",
            borderBottom: "1px solid #1c2a3a",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            background: "#0b0f1a",
            flexShrink: 0,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ fontSize: 10, letterSpacing: 2, color: "#4a6070", textTransform: "uppercase" }}>
              Live Cluster — namespace: otel-demo
            </span>
            {!error && (
              <span
                style={{
                  width: 6,
                  height: 6,
                  borderRadius: "50%",
                  background: "#00e87a",
                  boxShadow: "0 0 8px #00e87a",
                  display: "inline-block",
                  animation: "pulse-blue 2s infinite",
                }}
              />
            )}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            {lastRefresh && (
              <span style={{ fontSize: 10, color: "#2a3a4a" }}>refreshed {lastRefresh}</span>
            )}
            <button
              onClick={fetchPods}
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
              REFRESH
            </button>
          </div>
        </div>

        {/* Pod table */}
        <div style={{ flex: 1, overflowY: "auto", padding: "0 20px 20px" }}>
          {loading && (
            <div style={{ padding: 40, textAlign: "center", color: "#2a3a4a", fontSize: 12 }}>
              Loading cluster state...
            </div>
          )}

          {error && (
            <div
              style={{
                margin: 20,
                padding: 16,
                background: "#1a0810",
                border: "1px solid #ff224444",
                borderRadius: 6,
                fontSize: 11,
                color: "#ff6688",
              }}
            >
              <div style={{ marginBottom: 6, letterSpacing: 1 }}>CLUSTER API UNAVAILABLE</div>
              <div style={{ color: "#4a6070" }}>{error}</div>
              <div style={{ marginTop: 8, color: "#2a3a4a", fontSize: 10 }}>
                Configure API_URL in your environment and ensure the agent backend is running.
              </div>
            </div>
          )}

          {!loading && !error && (
            <table
              style={{
                width: "100%",
                borderCollapse: "collapse",
                fontSize: 11,
                marginTop: 16,
              }}
            >
              <thead>
                <tr
                  style={{
                    borderBottom: "1px solid #1c2a3a",
                    color: "#4a6070",
                    letterSpacing: 1,
                    fontSize: 9,
                    textTransform: "uppercase",
                  }}
                >
                  {["Pod Name", "Status", "Node", "Priority", "Restarts", "Age"].map((h) => (
                    <th key={h} style={{ padding: "8px 12px", textAlign: "left", fontWeight: 400 }}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {pods.map((pod) => {
                  const color = STATUS_COLOR[pod.status] || STATUS_COLOR.Unknown;
                  const terminating = pod.status === "Terminating" || pod.status === "Evicted";
                  return (
                    <tr
                      key={pod.name}
                      style={{
                        borderBottom: "1px solid #0d1622",
                        opacity: terminating ? 0.5 : 1,
                        transition: "opacity .3s",
                        background: pod.critical ? "#0d1a0f" : "transparent",
                      }}
                    >
                      <td style={{ padding: "8px 12px" }}>
                        <span style={{ color: terminating ? "#334" : "#a0c8e0" }}>{pod.name}</span>
                        {pod.critical && (
                          <span
                            style={{
                              marginLeft: 8,
                              fontSize: 8,
                              color: "#00aa55",
                              letterSpacing: 0.5,
                            }}
                          >
                            CRITICAL
                          </span>
                        )}
                      </td>
                      <td style={{ padding: "8px 12px" }}>
                        <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
                          <span
                            style={{
                              width: 6,
                              height: 6,
                              borderRadius: "50%",
                              background: color,
                              flexShrink: 0,
                            }}
                          />
                          <span style={{ color }}>{pod.status}</span>
                        </span>
                      </td>
                      <td style={{ padding: "8px 12px", color: "#4a6070", fontSize: 10 }}>
                        {pod.node}
                      </td>
                      <td style={{ padding: "8px 12px", color: "#4a6070", fontSize: 10 }}>
                        {pod.priority}
                      </td>
                      <td
                        style={{
                          padding: "8px 12px",
                          color: pod.restarts > 0 ? "#ffaa00" : "#4a6070",
                        }}
                      >
                        {pod.restarts}
                      </td>
                      <td style={{ padding: "8px 12px", color: "#2a3a4a", fontSize: 10 }}>
                        {pod.age}
                      </td>
                    </tr>
                  );
                })}
                {pods.length === 0 && (
                  <tr>
                    <td colSpan={6} style={{ padding: 40, textAlign: "center", color: "#2a3a4a" }}>
                      No pods found in otel-demo namespace
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}

export default function IncidentBanner() {
  return (
    <div
      style={{
        background: "#1a0810",
        borderBottom: "1px solid #ff224444",
        padding: "8px 20px",
        display: "flex",
        alignItems: "center",
        gap: 12,
        flexShrink: 0,
      }}
    >
      <span style={{ color: "#ff2244", fontSize: 11, letterSpacing: 2 }}>⚠ INCIDENT</span>
      <span style={{ color: "#ff6688", fontSize: 12 }}>
        Node disk pressure detected — checkout latency degrading — click SOLVE to begin investigation
      </span>
    </div>
  );
}

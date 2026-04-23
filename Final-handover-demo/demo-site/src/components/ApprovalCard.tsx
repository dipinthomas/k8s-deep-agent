type Props = { onApprove: () => void; onDeny: () => void };

export default function ApprovalCard({ onApprove, onDeny }: Props) {
  return (
    <div
      style={{
        margin: "8px 0",
        background: "#0c1520",
        border: "1px solid #ffaa0066",
        borderRadius: 6,
        padding: 14,
        animation: "fadeIn .2s ease-out",
      }}
    >
      <div style={{ color: "#ffaa00", fontSize: 11, letterSpacing: 1, marginBottom: 8 }}>
        ⏸  AWAITING APPROVAL
      </div>

      <div style={{ fontSize: 10, color: "#7a9ab0", marginBottom: 10, lineHeight: 1.7 }}>
        <div>
          <span style={{ color: "#4a6070" }}>Root cause: </span>
          <span style={{ color: "#c0d8e8" }}>imageprovider NGINX_LOG_LEVEL=debug</span>
        </div>
        <div>
          <span style={{ color: "#4a6070" }}>Node disk:   </span>
          <span style={{ color: "#ff6688" }}>87%</span>
        </div>
        <div>
          <span style={{ color: "#4a6070" }}>Checkout p99:</span>
          <span style={{ color: "#ff6688" }}> 890ms</span>
        </div>

        <div style={{ marginTop: 8, paddingTop: 8, borderTop: "1px solid #1c2a3a" }}>
          <div style={{ color: "#4a6070", marginBottom: 4 }}>Evict (lowest priority first):</div>
          {["loadgenerator", "imageprovider", "adservice"].map((id) => (
            <div key={id} style={{ paddingLeft: 8, color: "#ff8888" }}>
              — {id}
            </div>
          ))}
          <div style={{ marginTop: 6, color: "#4a6070" }}>Protected:</div>
          {["checkoutservice", "paymentservice", "cartservice"].map((id) => (
            <div key={id} style={{ paddingLeft: 8, color: "#00aa55" }}>
              ✓ {id}
            </div>
          ))}
        </div>

        <div style={{ marginTop: 8, color: "#7a7a3a", fontSize: 9 }}>
          ⚠ Product browsing unavailable. Payments continue normally.
        </div>
      </div>

      <div style={{ display: "flex", gap: 8 }}>
        <button
          onClick={onApprove}
          style={{
            flex: 1,
            background: "#003322",
            border: "1px solid #00aa55",
            color: "#00e87a",
            padding: "8px 0",
            borderRadius: 4,
            cursor: "pointer",
            fontSize: 11,
            letterSpacing: 1,
            fontFamily: "inherit",
          }}
        >
          ✓ APPROVE
        </button>
        <button
          onClick={onDeny}
          style={{
            flex: 1,
            background: "#1a0810",
            border: "1px solid #ff224466",
            color: "#ff6688",
            padding: "8px 0",
            borderRadius: 4,
            cursor: "pointer",
            fontSize: 11,
            letterSpacing: 1,
            fontFamily: "inherit",
          }}
        >
          ✗ DENY
        </button>
      </div>
    </div>
  );
}

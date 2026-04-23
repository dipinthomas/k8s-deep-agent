import type { Fault } from "@/types";

type Props = {
  x: number;
  y: number;
  serviceId: string;
  faults: Fault[];
  onSelect: (faultId: string) => void;
};

export default function ContextMenu({ x, y, serviceId, faults, onSelect }: Props) {
  return (
    <div
      style={{
        position: "fixed",
        left: x,
        top: y,
        zIndex: 1000,
        background: "#0d1622",
        border: "1px solid #1c2a3a",
        borderRadius: 6,
        overflow: "hidden",
        boxShadow: "0 8px 32px #00000088",
        minWidth: 200,
        fontFamily: "'SF Mono','Fira Code',monospace",
      }}
    >
      <div
        style={{
          padding: "8px 12px",
          borderBottom: "1px solid #1c2a3a",
          fontSize: 9,
          color: "#4a6070",
          letterSpacing: 1,
          textTransform: "uppercase",
        }}
      >
        Inject Fault — {serviceId}
      </div>
      {faults.map((f) => (
        <div
          key={f.id}
          onClick={(e) => { e.stopPropagation(); onSelect(f.id); }}
          style={{
            padding: "9px 12px",
            cursor: "pointer",
            fontSize: 11,
            display: "flex",
            flexDirection: "column",
            gap: 2,
            borderBottom: "1px solid #0d1622",
          }}
          onMouseEnter={(e) => (e.currentTarget.style.background = "#1a2a3a")}
          onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
        >
          <span style={{ color: "#c0d8e8" }}>{f.label}</span>
          <span style={{ fontSize: 9, color: "#4a6070" }}>{f.desc}</span>
        </div>
      ))}
    </div>
  );
}

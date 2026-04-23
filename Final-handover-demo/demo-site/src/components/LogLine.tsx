import type { LogEntry } from "@/types";

const TOOL_COLOR: Record<string, string> = {
  kubectl:    "#00d4ff",
  cloudwatch: "#ff9933",
  otel:       "#aa88ff",
  memory:     "#00aa55",
  default:    "#7ab8d4",
};

const TYPE_STYLE: Record<string, React.CSSProperties> = {
  agent:   { color: "#7ab8d4" },
  spawn:   { color: "#5a8a9a" },
  hypo:    { color: "#aaaa44", fontStyle: "italic" },
  reject:  { color: "#ff6644" },
  confirm: { color: "#00cc66" },
  resolved:{ color: "#00e87a", fontWeight: 600 },
  approval:{ color: "#ffaa00" },
};

const PREFIX: Record<string, string> = {
  agent:   "  ◆ ",
  spawn:   "    ",
  hypo:    "  ? ",
  reject:  "  ✗ ",
  confirm: "  ✓ ",
  resolved:"  ✓ ",
  approval:"  ⏸ ",
};

const base: React.CSSProperties = {
  fontSize: 10,
  lineHeight: 1.6,
  fontFamily: "'SF Mono','Fira Code',monospace",
  animation: "fadeIn .15s ease-out",
};

export default function LogLine({ log }: { log: LogEntry }) {
  if (log.type === "tool") {
    const color = TOOL_COLOR[log.tool || ""] || TOOL_COLOR.default;
    return (
      <div style={base}>
        <div>
          <span style={{ color: "#2a4a5a" }}>$ </span>
          <span style={{ color }}>[{log.tool}]</span>
          <span style={{ color: "#7a9ab0" }}> {log.cmd}</span>
        </div>
        {log.result &&
          String(log.result)
            .split("\n")
            .map((line, i) => (
              <div key={i} style={{ paddingLeft: 14, color: "#4a6a7a" }}>
                → {line}
              </div>
            ))}
      </div>
    );
  }

  return (
    <div style={{ ...base, ...(TYPE_STYLE[log.type] || {}) }}>
      <span style={{ color: "#2a3a4a" }}>{PREFIX[log.type] || "  "}</span>
      {log.text}
    </div>
  );
}

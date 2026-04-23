export type Phase = "idle" | "investigating" | "awaiting" | "resolving" | "resolved";

export type HealthStatus = "healthy" | "warning" | "critical" | "evicted";

export type Health = Record<string, HealthStatus>;
export type Latency = Record<string, number>;

export type LogEntry = {
  id: number;
  type: string;
  text?: string;
  tool?: string;
  cmd?: string;
  result?: string;
};

export type CtxMenuState = { x: number; y: number; serviceId: string };

export type Incident = { svcId: string; faultId: string };

export type Service = {
  id: string;
  name: string;
  lang: string;
  x: number;
  y: number;
  critical: boolean;
};

export type Fault = {
  id: string;
  label: string;
  desc: string;
};

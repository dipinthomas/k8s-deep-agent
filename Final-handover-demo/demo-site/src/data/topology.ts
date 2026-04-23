import type { Service, Fault } from "@/types";

export const W = 112, H = 54;

export const SERVICES: Service[] = [
  { id: "loadgenerator",  name: "Load Generator",  lang: "Python",     x: 266, y: 12,  critical: false },
  { id: "frontendproxy",  name: "Frontend Proxy",  lang: "Envoy",      x: 266, y: 94,  critical: false },
  { id: "checkout",       name: "Checkout",        lang: "Go",         x: 22,  y: 196, critical: true  },
  { id: "cart",           name: "Cart",            lang: ".NET",       x: 152, y: 196, critical: true  },
  { id: "imageprovider",  name: "Image Provider",  lang: "nginx",      x: 266, y: 196, critical: false },
  { id: "adservice",      name: "Ad Service",      lang: "Java",       x: 392, y: 196, critical: false },
  { id: "recommendation", name: "Recommend",       lang: "Python",     x: 508, y: 196, critical: false },
  { id: "payment",        name: "Payment",         lang: "JavaScript", x: 22,  y: 308, critical: true  },
  { id: "currency",       name: "Currency",        lang: "C++",        x: 152, y: 308, critical: false },
  { id: "productcatalog", name: "Product Catalog", lang: "Go",         x: 266, y: 308, critical: true  },
  { id: "shipping",       name: "Shipping",        lang: "Rust",       x: 392, y: 308, critical: false },
  { id: "email",          name: "Email",           lang: "Ruby",       x: 508, y: 308, critical: false },
];

export const EDGES: [string, string][] = [
  ["loadgenerator",  "frontendproxy"],
  ["frontendproxy",  "checkout"],
  ["frontendproxy",  "cart"],
  ["frontendproxy",  "imageprovider"],
  ["frontendproxy",  "adservice"],
  ["frontendproxy",  "recommendation"],
  ["frontendproxy",  "productcatalog"],
  ["checkout",       "payment"],
  ["checkout",       "currency"],
  ["checkout",       "productcatalog"],
  ["checkout",       "shipping"],
  ["checkout",       "email"],
  ["cart",           "payment"],
];

export const FAULTS: Fault[] = [
  { id: "disk_pressure", label: "Disk Pressure",  desc: "imageprovider fills node disk" },
  { id: "cpu_spike",     label: "CPU Spike",       desc: "Pod CPU throttling" },
  { id: "pod_crash",     label: "Crash Loop",      desc: "CrashLoopBackOff" },
  { id: "high_latency",  label: "High Latency",    desc: "Network latency injection" },
];

export const LANG_COLOR: Record<string, string> = {
  "Go": "#00acd7",
  "Python": "#3776ab",
  ".NET": "#512bd4",
  "Java": "#f89820",
  "JavaScript": "#f0db4f",
  "nginx": "#009639",
  "Envoy": "#ac57f0",
  "C++": "#00599c",
  "Rust": "#ce4a1a",
  "Ruby": "#cc342d",
};

export const HEALTH_COLOR: Record<string, string> = {
  healthy:  "#00e87a",
  warning:  "#ffaa00",
  critical: "#ff2244",
  evicted:  "#334455",
};

export const HEALTH_GLOW: Record<string, string> = {
  healthy:  "#00e87a33",
  warning:  "#ffaa0033",
  critical: "#ff224466",
  evicted:  "transparent",
};

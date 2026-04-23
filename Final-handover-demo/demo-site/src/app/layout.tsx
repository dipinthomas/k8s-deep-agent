import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "K8s AI Agent Demo — NZ Tech Rally 2026",
  description: "Live Kubernetes incident response with an AI agent",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

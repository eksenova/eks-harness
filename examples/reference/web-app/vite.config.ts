import react from "@vitejs/plugin-react";
import type { Connect } from "vite";
import { defineConfig } from "vite";

type Invoice = { id: number; customer: string; amount: number; status: "draft" | "sent" };

const invoices: Invoice[] = [
  { id: 1, customer: "Harman Kuyumculuk", amount: 2450.1, status: "sent" },
  { id: 2, customer: "Beyaz Altın Toptan", amount: 980, status: "draft" },
  { id: 3, customer: "Kadıköy Şube", amount: 125.5, status: "draft" },
];

const api: Connect.NextHandleFunction = (req, res, next) => {
  if (!req.url?.startsWith("/api/")) return next();
  res.setHeader("Content-Type", "application/json");
  if (req.url.startsWith("/api/invoices") && req.method === "GET") {
    res.end(JSON.stringify({ items: invoices, total: invoices.length }));
    return;
  }
  if (req.url.startsWith("/api/invoices") && req.method === "POST") {
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", () => {
      const draft = JSON.parse(body || "{}");
      const created = { id: invoices.length + 1, customer: String(draft.customer), amount: Number(draft.amount), status: "draft" as const };
      invoices.push(created);
      res.statusCode = 201;
      res.end(JSON.stringify(created));
    });
    return;
  }
  if (req.url.startsWith("/api/rates")) {
    res.end(JSON.stringify({ gold: 2450.1 }));
    return;
  }
  res.statusCode = 404;
  res.end(JSON.stringify({ error: "not found" }));
};

export default defineConfig({
  plugins: [react(), { name: "reference-api", configureServer: (server) => void server.middlewares.use(api) }],
  server: { strictPort: true },
});

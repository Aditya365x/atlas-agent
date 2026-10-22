import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Atlas: the agent that does the work",
  description:
    "Atlas plans, searches the web, runs Python in a sandbox, analyses your files, checks its own answer and shows every step. Try it without signing up.",
  openGraph: {
    title: "Atlas: the agent that does the work",
    description: "An autonomous AI agent with tools, RAG, MCP and a visible reasoning route.",
    type: "website",
  },
};

export const viewport: Viewport = { width: "device-width", initialScale: 1, themeColor: "#000000" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500&display=swap" />
        <link
          rel="icon"
          href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 28 28'%3E%3Crect width='28' height='28' rx='8' fill='black'/%3E%3Cpath d='M7 21 L14 6 L21 21' fill='none' stroke='white' stroke-width='2.4' stroke-linejoin='round' stroke-linecap='round'/%3E%3Cpath d='M10 15.5 H18' stroke='white' stroke-width='2.4' stroke-linecap='round'/%3E%3C/svg%3E"
        />
      </head>
      <body>{children}</body>
    </html>
  );
}

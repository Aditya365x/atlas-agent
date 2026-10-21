"use client";

import { useState } from "react";
import { api, ApiError, auth } from "@/lib/api";
import Logo from "./Logo";
import Route from "./Route";
import type { AgentEvent } from "@/lib/api";

const DEMO: AgentEvent[] = [
  { type: "plan", needs_tools: true, steps: ["Find this quarter's prices for the three GPUs", "Compute price per GB of VRAM", "Chart the comparison"] },
  { type: "tool.call", id: "a", tool: "web_search", args: { query: "H100 L40S A100 rental price per hour" } },
  { type: "tool.result", id: "a", tool: "web_search", ok: true, ms: 1400, preview: "" },
  { type: "tool.call", id: "b", tool: "python_exec", args: { code: "" } },
  { type: "tool.result", id: "b", tool: "python_exec", ok: true, ms: 2100, preview: "" },
  { type: "reflection", score: 9, feedback: "", revise: false },
];

export default function AuthScreen({ onAuthed }: { onAuthed: () => void }) {
  const [mode, setMode] = useState<"login" | "register">("register");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await api<{ access_token: string; email: string }>(`/v1/auth/${mode}`, {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      auth.save(res.access_token, res.email);
      onAuthed();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong. Try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth">
      <section className="auth-story">
        <div className="brand" style={{ padding: 0 }}>
          <Logo size={30} /> Atlas
        </div>
        <h1>Give it a task. Watch the route it takes.</h1>
        <p>
          Atlas plans the work, searches the web, runs Python in a locked-down sandbox, reads the files you upload, checks its own
          answer, and shows you every step on the way.
        </p>
        <div className="demo-route" aria-label="Example of a task route">
          <Route events={DEMO} live={false} defaultOpen />
        </div>
      </section>
      <section className="auth-form-wrap">
        <form className="auth-form" onSubmit={submit}>
          <h2>{mode === "register" ? "Create your account" : "Sign in"}</h2>
          <p>{mode === "register" ? "Free to try. Your chats and files stay private to you." : "Pick up where you left off."}</p>
          <label className="field">
            <span>Email</span>
            <input type="email" required autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} />
          </label>
          <label className="field">
            <span>Password</span>
            <input
              type="password"
              required
              minLength={8}
              autoComplete={mode === "register" ? "new-password" : "current-password"}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          {error && (
            <div className="error-box" role="alert" style={{ marginBottom: 14 }}>
              {error}
            </div>
          )}
          <button className="btn btn-primary" style={{ width: "100%", padding: "10px 14px" }} disabled={busy}>
            {busy ? "One moment" : mode === "register" ? "Create account" : "Sign in"}
          </button>
          <div className="switch">
            {mode === "register" ? "Already have an account? " : "New here? "}
            <button type="button" onClick={() => { setMode(mode === "register" ? "login" : "register"); setError(null); }}>
              {mode === "register" ? "Sign in" : "Create an account"}
            </button>
          </div>
        </form>
      </section>
    </main>
  );
}

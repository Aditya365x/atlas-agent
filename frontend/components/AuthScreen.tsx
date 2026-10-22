"use client";

import { useState } from "react";
import { api, ApiError, auth } from "@/lib/api";
import BlurText from "./bits/BlurText";
import ShinyText from "./bits/ShinyText";
import Threads from "./bits/Threads";
import Logo from "./Logo";

type Token = { access_token: string; email: string };

export default function AuthScreen({ onAuthed }: { onAuthed: () => void }) {
  const [mode, setMode] = useState<"guest" | "login" | "register">("guest");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function go(path: string, body?: object) {
    setBusy(true);
    setError(null);
    try {
      const res = await api<Token>(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });
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
      <div className="auth-bg" aria-hidden>
        <Threads color="#ffffff" amplitude={1.1} distance={0.2} lineCount={26} speed={0.35} enableMouseInteraction opacity={0.55} />
      </div>

      <section className="auth-card">
        <div className="logo-big"><Logo size={52} /></div>
        <div className="auth-title" role="heading" aria-level={1}>
          <BlurText text="Atlas" animateBy="letters" delay={60} direction="top" />
        </div>
        <div className="tagline">
          <ShinyText text="The agent that does the work, and shows its route." color="#8a8a8a" shineColor="#ffffff" speed={3} />
        </div>

        {mode === "guest" ? (
          <>
            <button className="btn btn-primary" disabled={busy} onClick={() => go("/v1/auth/guest")}>
              {busy ? "Starting…" : "Try Atlas now, no sign-up"}
            </button>
            <div className="divider">or</div>
            <button className="btn" onClick={() => setMode("register")}>Create an account</button>
            <div className="auth-switch">
              Have an account? <button type="button" onClick={() => setMode("login")}>Sign in</button>
            </div>
          </>
        ) : (
          <form
            className="auth-form"
            onSubmit={(e) => {
              e.preventDefault();
              go(`/v1/auth/${mode}`, { email, password });
            }}
          >
            <label className="sr-only" htmlFor="email">Email</label>
            <input id="email" className="input" type="email" required autoComplete="email" placeholder="Email address" value={email} onChange={(e) => setEmail(e.target.value)} />
            <label className="sr-only" htmlFor="password">Password</label>
            <input
              id="password"
              className="input"
              type="password"
              required
              minLength={8}
              placeholder="Password (8+ characters)"
              autoComplete={mode === "register" ? "new-password" : "current-password"}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            <button className="btn btn-primary" disabled={busy}>
              {busy ? "One moment…" : mode === "register" ? "Create account" : "Sign in"}
            </button>
            <div className="auth-switch" style={{ textAlign: "center" }}>
              {mode === "register" ? "Already have an account? " : "New here? "}
              <button type="button" onClick={() => { setMode(mode === "register" ? "login" : "register"); setError(null); }}>
                {mode === "register" ? "Sign in" : "Create one"}
              </button>
              {" · "}
              <button type="button" onClick={() => { setMode("guest"); setError(null); }}>Continue as guest</button>
            </div>
          </form>
        )}

        {error && <div className="error-box" role="alert" style={{ marginTop: 14 }}>{error}</div>}

        <div className="features">
          <span>Web research</span>
          <span>Python sandbox</span>
          <span>RAG over your files</span>
          <span>MCP server</span>
          <span>Self-review</span>
        </div>
      </section>
      <div className="auth-foot">Guest sessions have a small daily budget. Your chats and files are private to your account.</div>
    </main>
  );
}

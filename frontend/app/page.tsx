"use client";

import { useEffect, useState } from "react";
import AuthScreen from "@/components/AuthScreen";
import ChatApp from "@/components/ChatApp";
import { auth } from "@/lib/api";

export default function Home() {
  const [authed, setAuthed] = useState<boolean | null>(null);

  useEffect(() => {
    setAuthed(!!auth.token);
    const onLogout = () => setAuthed(false);
    window.addEventListener("atlas:logout", onLogout);
    return () => window.removeEventListener("atlas:logout", onLogout);
  }, []);

  if (authed === null) return null;
  return authed ? <ChatApp onLogout={() => setAuthed(false)} /> : <AuthScreen onAuthed={() => setAuthed(true)} />;
}

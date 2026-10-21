// Load test: concurrent users each register, start a run and stream it to completion.
//   k6 run -e API=http://localhost:8000 loadtest/k6.js
// Run against LLM_MODE=fake to measure platform overhead (API, queue, DB, SSE) without model latency,
// then against a live model for end-to-end numbers. Raise RATE_LIMIT_PER_MINUTE on the API first,
// otherwise the per-user limiter (working as intended) turns most requests into 429s.
import http from "k6/http";
import { check } from "k6";
import { Trend, Rate } from "k6/metrics";

const API = __ENV.API || "http://localhost:8000";
const runLatency = new Trend("run_e2e_ms", true);
const runOk = new Rate("run_success");

export const options = {
  scenarios: {
    ramp: { executor: "ramping-vus", stages: [{ duration: "30s", target: 25 }, { duration: "1m", target: 50 }, { duration: "20s", target: 0 }] },
  },
  thresholds: { run_success: ["rate>0.98"], http_req_failed: ["rate<0.02"], "http_req_duration{name:create_run}": ["p(95)<300"] },
};

const PROMPTS = ["Calculate 48291 * 7734", "hi! who are you?", "Plot a chart of y = x^2 for x from 1 to 10"];

export function setup() {
  const tokens = [];
  for (let i = 0; i < 50; i++) {
    const r = http.post(`${API}/v1/auth/register`, JSON.stringify({ email: `load-${Date.now()}-${i}@atlas.dev`, password: "load-test-pass" }), {
      headers: { "Content-Type": "application/json" },
    });
    tokens.push(r.json("access_token"));
  }
  return { tokens };
}

export default function (data) {
  const token = data.tokens[(__VU - 1) % data.tokens.length];
  const headers = { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
  const t0 = Date.now();
  const res = http.post(`${API}/v1/runs`, JSON.stringify({ message: PROMPTS[__ITER % PROMPTS.length] }), { headers, tags: { name: "create_run" } });
  if (!check(res, { "run accepted": (r) => r.status === 202 })) {
    runOk.add(false);
    return;
  }
  // k6 has no native SSE client; the events endpoint returns once the run is terminal, so a plain GET streams to the end.
  const ev = http.get(`${API}${res.json("events_url")}`, { headers: { Authorization: `Bearer ${token}` }, timeout: "180s", tags: { name: "stream" } });
  const ok = ev.status === 200 && ev.body.includes("run.completed");
  runOk.add(ok);
  if (ok) runLatency.add(Date.now() - t0);
}

import assert from "node:assert/strict";
import test from "node:test";
import { createApp } from "./app.js";

test("bounded job queue rejects early when full", async (t) => {
  const app = await createApp({
    queueCapacity: 1,
    workers: 1,
    downstreamConcurrency: 1,
    processDelayMs: 100,
    logger: false,
  });
  t.after(() => app.close());

  const first = await app.inject({ method: "POST", url: "/jobs" });
  const second = await app.inject({ method: "POST", url: "/jobs" });
  const third = await app.inject({ method: "POST", url: "/jobs" });

  assert.equal(first.statusCode, 202);
  assert.equal(second.statusCode, 202);
  assert.equal(third.statusCode, 429);
});

test("sync route rejects when all downstream slots are occupied", async (t) => {
  const app = await createApp({
    downstreamConcurrency: 1,
    processDelayMs: 100,
    logger: false,
  });
  t.after(() => app.close());

  const firstRequest = app.inject({ method: "GET", url: "/sync" });
  await new Promise((resolve) => setTimeout(resolve, 10));
  const secondResponse = await app.inject({ method: "GET", url: "/sync" });
  await firstRequest;

  assert.equal(secondResponse.statusCode, 503);
});

test("rate limit allows its quota and rejects the next request", async (t) => {
  const app = await createApp({ ratePerSecond: 1, logger: false });
  t.after(() => app.close());

  const first = await app.inject({ method: "GET", url: "/limited" });
  const second = await app.inject({ method: "GET", url: "/limited" });

  assert.equal(first.statusCode, 200);
  assert.equal(second.statusCode, 429);
});

test("batch endpoint validates its item bound", async (t) => {
  const app = await createApp({ logger: false });
  t.after(() => app.close());

  const response = await app.inject({ method: "GET", url: "/batch?items=2001" });
  assert.equal(response.statusCode, 400);
});

test("tenant bulkhead keeps another tenant admissible", async (t) => {
  const app = await createApp({
    tenantOutstandingLimit: 1,
    processDelayMs: 50,
    logger: false,
  });
  t.after(() => app.close());

  const first = await app.inject({ method: "POST", url: "/jobs?tenant=alpha" });
  const blocked = await app.inject({ method: "POST", url: "/jobs?tenant=alpha" });
  const otherTenant = await app.inject({ method: "POST", url: "/jobs?tenant=beta" });

  assert.equal(first.statusCode, 202);
  assert.equal(blocked.statusCode, 429);
  assert.equal(otherTenant.statusCode, 202);
});

test("stream endpoint rejects an oversized stream", async (t) => {
  const app = await createApp({ logger: false });
  t.after(() => app.close());

  const response = await app.inject({ method: "GET", url: "/stream?items=1001" });
  assert.equal(response.statusCode, 400);
});

test("job retry policy retries transient failures and then completes", async (t) => {
  const app = await createApp({
    processDelayMs: 1,
    retryBaseMs: 1,
    maxRetries: 2,
    jobTimeoutMs: 1000,
    logger: false,
  });
  t.after(() => app.close());

  const response = await app.inject({ method: "POST", url: "/jobs?failures=2" });
  assert.equal(response.statusCode, 202);

  for (let attempt = 0; attempt < 50; attempt++) {
    const metrics = await app.inject({ method: "GET", url: "/metrics" });
    if (metrics.json().jobs_processed_total === 1) break;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }

  const metrics = (await app.inject({ method: "GET", url: "/metrics" })).json();
  assert.equal(metrics.jobs_retries_total, 2);
  assert.equal(metrics.jobs_processed_total, 1);
});

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

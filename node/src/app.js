import Fastify from "fastify";
import rateLimit from "@fastify/rate-limit";
import pLimit from "p-limit";
import PQueue from "p-queue";
import { performance } from "node:perf_hooks";
import { once } from "node:events";
import { setTimeout as sleep } from "node:timers/promises";

const integer = (value, name, min, max) => {
  const number = Number(value);
  if (!Number.isInteger(number) || number < min || number > max) {
    throw new Error(`${name} must be between ${min} and ${max}`);
  }
  return number;
};

export async function createApp(overrides = {}) {
  const config = {
    queueCapacity: 32,
    workers: 4,
    downstreamConcurrency: 2,
    processDelayMs: 750,
    jobTimeoutMs: 5000,
    ratePerSecond: 10,
    tenantOutstandingLimit: 0,
    maxRetries: 3,
    retryBaseMs: 25,
    ...overrides,
  };
  config.queueCapacity = integer(config.queueCapacity, "queueCapacity", 1, 10000);
  config.workers = integer(config.workers, "workers", 1, 256);
  config.downstreamConcurrency = integer(
    config.downstreamConcurrency,
    "downstreamConcurrency",
    1,
    256,
  );
  config.processDelayMs = integer(config.processDelayMs, "processDelayMs", 1, 60000);
  config.jobTimeoutMs = integer(config.jobTimeoutMs, "jobTimeoutMs", 1, 120000);
  config.ratePerSecond = integer(config.ratePerSecond, "ratePerSecond", 1, 100000);
  config.tenantOutstandingLimit = integer(
    config.tenantOutstandingLimit,
    "tenantOutstandingLimit",
    0,
    10000,
  );
  config.maxRetries = integer(config.maxRetries, "maxRetries", 0, 20);
  config.retryBaseMs = integer(config.retryBaseMs, "retryBaseMs", 0, 5000);

  const app = Fastify({ logger: overrides.logger ?? false });
  const jobs = new PQueue({ concurrency: config.workers });
  const downstream = new PQueue({ concurrency: config.downstreamConcurrency });
  const metrics = {
    enqueued: 0,
    rejected: 0,
    processed: 0,
    failed: 0,
    retries: 0,
    tenantRejected: 0,
    syncInUse: 0,
  };
  const tenantOutstanding = new Map();
  let nextJobId = 1;

  function reserveTenant(tenant) {
    if (config.tenantOutstandingLimit === 0) return true;
    const count = tenantOutstanding.get(tenant) ?? 0;
    if (count >= config.tenantOutstandingLimit) return false;
    tenantOutstanding.set(tenant, count + 1);
    return true;
  }

  function releaseTenant(tenant) {
    if (config.tenantOutstandingLimit === 0) return;
    const count = (tenantOutstanding.get(tenant) ?? 1) - 1;
    if (count <= 0) tenantOutstanding.delete(tenant);
    else tenantOutstanding.set(tenant, count);
  }

  await app.register(rateLimit, {
    global: false,
    errorResponseBuilder: (_request, context) => ({
      statusCode: 429,
      error_code: "RATE_LIMITED",
      message: "rate limit exceeded",
      retry_after_seconds: context.after,
    }),
  });

  app.addHook("onClose", async () => {
    await jobs.onIdle();
    await downstream.onIdle();
  });

  app.get("/healthz", async () => ({ status: "ok" }));

  app.post("/jobs", async (request, reply) => {
    if (jobs.size >= config.queueCapacity) {
      metrics.rejected++;
      return reply.header("retry-after", "1").code(429).send({
        error_code: "QUEUE_FULL",
        message: "server is busy; retry later",
      });
    }

    let failures;
    const tenant = request.query.tenant ?? "default";
    try {
      failures = integer(request.query.failures ?? 0, "failures", 0, 10);
    } catch (error) {
      return reply.code(400).send({ error: error.message });
    }
    if (!["alpha", "beta", "default"].includes(tenant)) {
      return reply.code(400).send({ error: "tenant must be alpha, beta, or default" });
    }
    if (!reserveTenant(tenant)) {
      metrics.rejected++;
      metrics.tenantRejected++;
      return reply.code(429).send({ error_code: "TENANT_BUSY" });
    }

    const job = { id: nextJobId++, tenant, failures, createdAt: new Date().toISOString() };
    metrics.enqueued++;
    const timeoutSignal = AbortSignal.timeout(config.jobTimeoutMs);
    void jobs
      .add(async () => {
        try {
          await processJob(job, timeoutSignal);
          metrics.processed++;
        } catch (error) {
          metrics.failed++;
          app.log.warn({ jobId: job.id, err: error }, "job failed or timed out");
        } finally {
          releaseTenant(job.tenant);
        }
      }, { signal: timeoutSignal })
      .catch((error) => {
        metrics.failed++;
        releaseTenant(job.tenant);
        app.log.warn({ jobId: job.id, err: error }, "job was cancelled before processing");
      });

    return reply.code(202).send({ status: "queued", job_id: job.id });
  });

  async function processJob(job, signal) {
    await downstream.add(async () => {
      for (let attempt = 1; ; attempt++) {
        await sleep(config.processDelayMs, undefined, { signal });
        if (attempt > job.failures) return;
        if (attempt > config.maxRetries) throw new Error("retry limit reached");

        metrics.retries++;
        const exponential = Math.min(config.retryBaseMs * (2 ** (attempt - 1)), 1000);
        const jitter = Math.floor(Math.random() * (Math.floor(exponential / 4) + 1));
        await sleep(exponential + jitter, undefined, { signal });
      }
    }, { signal });
  }

  app.get("/sync", async (_request, reply) => {
    if (downstream.size + downstream.pending >= config.downstreamConcurrency) {
      metrics.rejected++;
      return reply.code(503).send({
        error_code: "DOWNSTREAM_SATURATED",
        message: "downstream concurrency limit reached",
      });
    }

    metrics.syncInUse++;
    try {
      const signal = AbortSignal.timeout(config.jobTimeoutMs);
      await downstream.add(
        () => sleep(config.processDelayMs, undefined, { signal }),
        { signal },
      );
      return { status: "processed" };
    } catch {
      return reply.code(504).send({ error_code: "TIMEOUT" });
    } finally {
      metrics.syncInUse--;
    }
  });

  app.get("/cpu", async (request, reply) => {
    let ms;
    try {
      ms = integer(request.query.ms ?? 50, "ms", 1, 1000);
    } catch (error) {
      return reply.code(400).send({ error: error.message });
    }

    // Intentionally synchronous: this demonstrates event-loop blocking.
    const deadline = performance.now() + ms;
    let checksum = 0x9e3779b9;
    while (performance.now() < deadline) {
      checksum = Math.imul(checksum ^ (checksum >>> 13), 0x5bd1e995);
    }
    return { status: "computed", cpu_ms: ms, checksum: checksum >>> 0 };
  });

  app.get("/stream", async (request, reply) => {
    let items;
    let delayMs;
    try {
      items = integer(request.query.items ?? 20, "items", 1, 1000);
      delayMs = integer(request.query.delay_ms ?? 10, "delay_ms", 0, 1000);
    } catch (error) {
      return reply.code(400).send({ error: error.message });
    }

    reply.hijack();
    reply.raw.writeHead(200, { "content-type": "text/plain; charset=utf-8" });
    try {
      for (let item = 1; item <= items; item++) {
        if (delayMs > 0) await sleep(delayMs);
        if (!reply.raw.write(`${item}\n`)) {
          await Promise.race([once(reply.raw, "drain"), once(reply.raw, "close")]);
          if (reply.raw.destroyed) return reply;
        }
      }
      if (!reply.raw.destroyed) reply.raw.end();
    } catch (error) {
      reply.raw.destroy(error);
    }
    return reply;
  });

  app.get("/limited", {
    config: {
      rateLimit: {
        max: config.ratePerSecond,
        timeWindow: "1 second",
      },
    },
  }, async () => ({ status: "allowed" }));

  app.get("/batch", async (request, reply) => {
    let items;
    let concurrency;
    try {
      items = integer(request.query.items ?? 100, "items", 1, 2000);
      concurrency = integer(request.query.concurrency ?? 8, "concurrency", 1, 64);
    } catch (error) {
      return reply.code(400).send({ error: error.message });
    }

    const limit = pLimit(concurrency);
    const timeoutSignal = AbortSignal.timeout(config.jobTimeoutMs);
    try {
      await Promise.all(
        Array.from({ length: items }, () =>
          limit(() => sleep(config.processDelayMs, undefined, { signal: timeoutSignal })),
        ),
      );
      return { items, completed: items, concurrency };
    } catch {
      return reply.code(504).send({ error_code: "BATCH_TIMEOUT" });
    }
  });

  app.get("/metrics", async () => ({
    queue_depth: jobs.size,
    queue_capacity: config.queueCapacity,
    workers: config.workers,
    jobs_in_flight: jobs.pending,
    downstream_in_use: downstream.pending,
    downstream_queue_depth: downstream.size,
    downstream_concurrency: config.downstreamConcurrency,
    sync_requests_in_flight: metrics.syncInUse,
    jobs_enqueued_total: metrics.enqueued,
    requests_rejected_total: metrics.rejected,
    jobs_processed_total: metrics.processed,
    jobs_failed_total: metrics.failed,
    jobs_retries_total: metrics.retries,
    tenant_rejected_total: metrics.tenantRejected,
    tenant_outstanding_limit: config.tenantOutstandingLimit,
  }));

  return app;
}

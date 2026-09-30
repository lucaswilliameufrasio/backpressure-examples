import Fastify from "fastify";
import rateLimit from "@fastify/rate-limit";
import pLimit from "p-limit";
import PQueue from "p-queue";
import { performance } from "node:perf_hooks";
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

  const app = Fastify({ logger: overrides.logger ?? false });
  const jobs = new PQueue({ concurrency: config.workers });
  const downstream = new PQueue({ concurrency: config.downstreamConcurrency });
  const metrics = {
    enqueued: 0,
    rejected: 0,
    processed: 0,
    failed: 0,
    syncInUse: 0,
  };
  let nextJobId = 1;

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

  app.post("/jobs", async (_request, reply) => {
    if (jobs.size >= config.queueCapacity) {
      metrics.rejected++;
      return reply.header("retry-after", "1").code(429).send({
        error_code: "QUEUE_FULL",
        message: "server is busy; retry later",
      });
    }

    const job = { id: nextJobId++, createdAt: new Date().toISOString() };
    metrics.enqueued++;
    const timeoutSignal = AbortSignal.timeout(config.jobTimeoutMs);
    void jobs
      .add(async () => {
        try {
          await downstream.add(
            () => sleep(config.processDelayMs, undefined, { signal: timeoutSignal }),
            { signal: timeoutSignal },
          );
          metrics.processed++;
        } catch (error) {
          metrics.failed++;
          app.log.warn({ jobId: job.id, err: error }, "job failed or timed out");
        }
      }, { signal: timeoutSignal })
      .catch((error) => {
        metrics.failed++;
        app.log.warn({ jobId: job.id, err: error }, "job was cancelled before processing");
      });

    return reply.code(202).send({ status: "queued", job_id: job.id });
  });

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
  }));

  return app;
}

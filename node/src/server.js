import { createApp } from "./app.js";

function envInt(name, fallback) {
  const value = Number(process.env[name]);
  return Number.isInteger(value) && value > 0 ? value : fallback;
}

function envIntAllowZero(name, fallback) {
  const value = Number(process.env[name]);
  return Number.isInteger(value) && value >= 0 ? value : fallback;
}

const app = await createApp({
  logger: true,
  queueCapacity: envInt("QUEUE_CAPACITY", 32),
  workers: envInt("WORKERS", 4),
  downstreamConcurrency: envInt("DOWNSTREAM_CONCURRENCY", 2),
  processDelayMs: envInt("PROCESS_DELAY_MS", 750),
  jobTimeoutMs: envInt("JOB_TIMEOUT_MS", 5000),
  ratePerSecond: envInt("RATE_PER_SECOND", 10),
  tenantOutstandingLimit: Number.isInteger(Number(process.env.TENANT_OUTSTANDING_LIMIT))
    ? Number(process.env.TENANT_OUTSTANDING_LIMIT)
    : 0,
  maxRetries: envIntAllowZero("MAX_RETRIES", 3),
  retryBaseMs: envInt("RETRY_BASE_MS", 25),
});

const port = envInt("PORT", 3000);
const host = process.env.HOST ?? "127.0.0.1";
await app.listen({ host, port });

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, async () => {
    app.log.info({ signal }, "graceful shutdown started");
    try {
      await app.close();
      process.exitCode = 0;
    } catch (error) {
      app.log.error({ err: error }, "graceful shutdown failed");
      process.exitCode = 1;
    }
  });
}

import { createApp } from "./app.js";

function envInt(name, fallback) {
  const value = Number(process.env[name]);
  return Number.isInteger(value) && value > 0 ? value : fallback;
}

const app = await createApp({
  logger: true,
  queueCapacity: envInt("QUEUE_CAPACITY", 32),
  workers: envInt("WORKERS", 4),
  downstreamConcurrency: envInt("DOWNSTREAM_CONCURRENCY", 2),
  processDelayMs: envInt("PROCESS_DELAY_MS", 750),
  jobTimeoutMs: envInt("JOB_TIMEOUT_MS", 5000),
  ratePerSecond: envInt("RATE_PER_SECOND", 10),
});

const port = envInt("PORT", 3000);
await app.listen({ host: "0.0.0.0", port });

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

# Benchmark: node / rate-limit

- Run: `5807e19deaa0`
- Started: 2026-09-29T16:15:11Z
- Commit: `aad3b68c04a6`
- Source tree: `dirty`
- Environment: linux cachyos ; kernel 7.2.3; x86_64
- Hardware: AMD Ryzen 9 7900 12-Core Processor; 24 logical cores; 62.0 GiB RAM (rounded)

## Configuration

```json
{
  "downstream_concurrency": 1,
  "job_timeout_ms": 10000,
  "max_retries": 3,
  "process_delay_ms": 50,
  "queue_capacity": 8,
  "repeats": 1,
  "retry_base_ms": 10,
  "scenario_parameters": {
    "burst_rate": 200,
    "concurrency": 20,
    "cpu_ms": 100,
    "duration_seconds": 5,
    "failures_injected": 5,
    "rate_per_second": 10,
    "requests": 50,
    "stream_delay_ms": 5,
    "stream_items": 100,
    "tenant_probe_tenant": null
  },
  "tenant_outstanding_limit": 0,
  "workers": 2
}
```

## Repetitions

| Run | RPS | p50 ms | p95 ms | p99 ms | CPU cores | Peak RSS MiB | HTTP statuses |
|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | 4224.328 | 0.933 | 10.291 | 10.754 | 0.0 | 80.91 | `{"200": 10, "429": 40}` |

## Aggregate medians

- Requests/sec: 4224.328
- p50/p95/p99 latency (ms): 0.933 / 10.291 / 10.754

This is a local observation, not a universal language benchmark. The environment and exact workload are part of the result.

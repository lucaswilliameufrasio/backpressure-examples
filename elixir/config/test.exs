import Config

config :backpressure_elixir,
  serve_http: false,
  queue_capacity: 1,
  workers: 1,
  downstream_concurrency: 1,
  tenant_outstanding_limit: 0,
  process_delay_ms: 400,
  job_timeout_ms: 2_000,
  rate_per_second: 1,
  rate_window_ms: 60_000,
  max_retries: 2,
  retry_base_ms: 5

use backpressure_examples_rust::{AppState, Config, router, start_workers};
use std::{env, time::Duration};
use tokio::signal;

fn env_usize(name: &str, fallback: usize) -> usize {
    env::var(name)
        .ok()
        .and_then(|value| value.parse().ok())
        .filter(|value| *value > 0)
        .unwrap_or(fallback)
}

fn env_usize_allow_zero(name: &str, fallback: usize) -> usize {
    env::var(name)
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(fallback)
}

fn env_millis(name: &str, fallback: u64) -> Duration {
    Duration::from_millis(env_usize(name, fallback as usize) as u64)
}

fn env_string(name: &str, fallback: &str) -> String {
    env::var(name).unwrap_or_else(|_| fallback.to_string())
}

#[tokio::main]
async fn main() {
    let defaults = Config::default();
    let config = Config {
        queue_capacity: env_usize("QUEUE_CAPACITY", defaults.queue_capacity),
        workers: env_usize("WORKERS", defaults.workers),
        downstream_concurrency: env_usize(
            "DOWNSTREAM_CONCURRENCY",
            defaults.downstream_concurrency,
        ),
        process_delay: env_millis("PROCESS_DELAY_MS", 750),
        job_timeout: env_millis("JOB_TIMEOUT_MS", 5000),
        rate_per_second: env_usize("RATE_PER_SECOND", 10) as f64,
        rate_burst: env_usize("RATE_BURST", 20),
        tenant_outstanding_limit: env_usize_allow_zero("TENANT_OUTSTANDING_LIMIT", 0),
        max_retries: env_usize_allow_zero("MAX_RETRIES", defaults.max_retries),
        retry_base: env_millis("RETRY_BASE_MS", 25),
    };
    let (state, rx) = AppState::new(config);
    let worker_handle = start_workers(rx, state.clone());
    let port = env_usize("PORT", 3000);
    let host = env_string("HOST", "127.0.0.1");
    let listener = tokio::net::TcpListener::bind(format!("{host}:{port}"))
        .await
        .expect("bind HTTP listener");
    println!("Rust example listening on {host}:{port}");

    axum::serve(listener, router(state.clone()))
        .with_graceful_shutdown(shutdown_signal())
        .await
        .expect("HTTP server failed");

    drop(state);
    worker_handle.await.expect("worker tasks failed");
}

async fn shutdown_signal() {
    let ctrl_c = async {
        signal::ctrl_c().await.expect("install Ctrl-C handler");
    };

    #[cfg(unix)]
    let terminate = async {
        use tokio::signal::unix::{SignalKind, signal};
        signal(SignalKind::terminate())
            .expect("install SIGTERM handler")
            .recv()
            .await;
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
    println!("shutdown signal received; draining queued jobs");
}

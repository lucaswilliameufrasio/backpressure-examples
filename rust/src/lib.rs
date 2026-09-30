use axum::{
    Json, Router,
    extract::{Query, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
};
use futures_util::stream::{self, StreamExt};
use serde::{Deserialize, Serialize};
use std::{
    sync::{
        Arc, Mutex,
        atomic::{AtomicI64, AtomicU64, AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::{
    sync::{Mutex as AsyncMutex, Semaphore, mpsc},
    task::JoinHandle,
    time::{sleep, timeout},
};

#[derive(Clone, Debug)]
pub struct Config {
    pub queue_capacity: usize,
    pub workers: usize,
    pub downstream_concurrency: usize,
    pub process_delay: Duration,
    pub job_timeout: Duration,
    pub rate_per_second: f64,
    pub rate_burst: usize,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            queue_capacity: 32,
            workers: 4,
            downstream_concurrency: 2,
            process_delay: Duration::from_millis(750),
            job_timeout: Duration::from_secs(5),
            rate_per_second: 10.0,
            rate_burst: 20,
        }
    }
}

#[derive(Clone, Debug)]
pub struct Job {
    id: u64,
}

#[derive(Default)]
struct Metrics {
    enqueued: AtomicU64,
    rejected: AtomicU64,
    processed: AtomicU64,
    failed: AtomicU64,
    in_flight: AtomicI64,
    queue_depth: AtomicUsize,
    sync_in_use: AtomicUsize,
    next_id: AtomicU64,
}

#[derive(Clone)]
pub struct AppState {
    tx: mpsc::Sender<Job>,
    pub config: Config,
    metrics: Arc<Metrics>,
    downstream: Arc<Semaphore>,
    limiter: Arc<Mutex<TokenBucket>>,
}

impl AppState {
    pub fn new(config: Config) -> (Self, mpsc::Receiver<Job>) {
        let (tx, rx) = mpsc::channel(config.queue_capacity);
        let state = Self {
            downstream: Arc::new(Semaphore::new(config.downstream_concurrency)),
            limiter: Arc::new(Mutex::new(TokenBucket::new(
                config.rate_per_second,
                config.rate_burst,
            ))),
            metrics: Arc::new(Metrics::default()),
            tx,
            config,
        };
        (state, rx)
    }
}

struct TokenBucket {
    tokens: f64,
    capacity: f64,
    refill_per_second: f64,
    updated_at: Instant,
}

impl TokenBucket {
    fn new(refill_per_second: f64, capacity: usize) -> Self {
        Self {
            tokens: capacity as f64,
            capacity: capacity as f64,
            refill_per_second,
            updated_at: Instant::now(),
        }
    }

    fn allow(&mut self) -> bool {
        let now = Instant::now();
        let elapsed = now.duration_since(self.updated_at).as_secs_f64();
        self.tokens = (self.tokens + elapsed * self.refill_per_second).min(self.capacity);
        self.updated_at = now;
        if self.tokens < 1.0 {
            return false;
        }
        self.tokens -= 1.0;
        true
    }
}

#[derive(Serialize)]
struct ErrorBody {
    error_code: &'static str,
    message: &'static str,
}

#[derive(Deserialize)]
struct BatchQuery {
    items: Option<usize>,
    concurrency: Option<usize>,
}

#[derive(Deserialize)]
struct CpuQuery {
    ms: Option<u64>,
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(health))
        .route("/jobs", post(enqueue_job))
        .route("/sync", get(sync_work))
        .route("/cpu", get(cpu_work))
        .route("/limited", get(rate_limited))
        .route("/batch", get(batch))
        .route("/metrics", get(metrics))
        .with_state(state)
}

pub fn start_workers(rx: mpsc::Receiver<Job>, state: AppState) -> JoinHandle<()> {
    let rx = Arc::new(AsyncMutex::new(rx));
    let config = state.config.clone();
    let metrics = state.metrics.clone();
    let downstream = state.downstream.clone();
    let mut handles = Vec::with_capacity(state.config.workers);

    for worker_id in 0..state.config.workers {
        let rx = rx.clone();
        let config = config.clone();
        let metrics = metrics.clone();
        let downstream = downstream.clone();
        handles.push(tokio::spawn(async move {
            loop {
                let job = {
                    let mut receiver = rx.lock().await;
                    receiver.recv().await
                };
                let Some(job) = job else { break };

                metrics.queue_depth.fetch_sub(1, Ordering::Relaxed);
                metrics.in_flight.fetch_add(1, Ordering::Relaxed);
                let result = timeout(config.job_timeout, async {
                    let _permit = downstream
                        .acquire()
                        .await
                        .expect("downstream semaphore remains open");
                    sleep(config.process_delay).await;
                })
                .await;

                if result.is_ok() {
                    metrics.processed.fetch_add(1, Ordering::Relaxed);
                    println!("worker={worker_id} processed job={}", job.id);
                } else {
                    metrics.failed.fetch_add(1, Ordering::Relaxed);
                    eprintln!("worker={worker_id} job={} timed out", job.id);
                }
                metrics.in_flight.fetch_sub(1, Ordering::Relaxed);
            }
        }));
    }

    tokio::spawn(async move {
        for handle in handles {
            let _ = handle.await;
        }
    })
}

async fn health() -> impl IntoResponse {
    (StatusCode::OK, Json(serde_json::json!({ "status": "ok" })))
}

async fn enqueue_job(State(state): State<AppState>) -> impl IntoResponse {
    let job = Job {
        id: state.metrics.next_id.fetch_add(1, Ordering::Relaxed) + 1,
    };
    state.metrics.queue_depth.fetch_add(1, Ordering::Relaxed);
    match state.tx.try_send(job.clone()) {
        Ok(()) => {
            state.metrics.enqueued.fetch_add(1, Ordering::Relaxed);
            (
                StatusCode::ACCEPTED,
                Json(serde_json::json!({ "status": "queued", "job_id": job.id })),
            )
        }
        Err(mpsc::error::TrySendError::Full(_)) => {
            state.metrics.queue_depth.fetch_sub(1, Ordering::Relaxed);
            state.metrics.rejected.fetch_add(1, Ordering::Relaxed);
            (
                StatusCode::TOO_MANY_REQUESTS,
                Json(
                    serde_json::to_value(ErrorBody {
                        error_code: "QUEUE_FULL",
                        message: "server is busy; retry later",
                    })
                    .unwrap(),
                ),
            )
        }
        Err(mpsc::error::TrySendError::Closed(_)) => {
            state.metrics.queue_depth.fetch_sub(1, Ordering::Relaxed);
            (
                StatusCode::SERVICE_UNAVAILABLE,
                Json(
                    serde_json::to_value(ErrorBody {
                        error_code: "SHUTTING_DOWN",
                        message: "worker queue is closed",
                    })
                    .unwrap(),
                ),
            )
        }
    }
}

async fn sync_work(State(state): State<AppState>) -> impl IntoResponse {
    let permit = match state.downstream.clone().try_acquire_owned() {
        Ok(permit) => permit,
        Err(_) => {
            state.metrics.rejected.fetch_add(1, Ordering::Relaxed);
            return (
                StatusCode::SERVICE_UNAVAILABLE,
                Json(
                    serde_json::to_value(ErrorBody {
                        error_code: "DOWNSTREAM_SATURATED",
                        message: "downstream concurrency limit reached",
                    })
                    .unwrap(),
                ),
            );
        }
    };
    state.metrics.sync_in_use.fetch_add(1, Ordering::Relaxed);
    let result = timeout(state.config.job_timeout, sleep(state.config.process_delay)).await;
    drop(permit);
    state.metrics.sync_in_use.fetch_sub(1, Ordering::Relaxed);
    match result {
        Ok(()) => (
            StatusCode::OK,
            Json(serde_json::json!({ "status": "processed" })),
        ),
        Err(_) => (
            StatusCode::GATEWAY_TIMEOUT,
            Json(serde_json::json!({ "error_code": "TIMEOUT" })),
        ),
    }
}

async fn rate_limited(State(state): State<AppState>) -> impl IntoResponse {
    let allowed = state
        .limiter
        .lock()
        .expect("token bucket mutex poisoned")
        .allow();
    if !allowed {
        state.metrics.rejected.fetch_add(1, Ordering::Relaxed);
        return (
            StatusCode::TOO_MANY_REQUESTS,
            Json(serde_json::json!({ "error_code": "RATE_LIMITED" })),
        );
    }
    (
        StatusCode::OK,
        Json(serde_json::json!({ "status": "allowed" })),
    )
}

async fn cpu_work(
    State(state): State<AppState>,
    Query(query): Query<CpuQuery>,
) -> impl IntoResponse {
    let millis = query.ms.unwrap_or(50);
    if !(1..=1000).contains(&millis) {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({ "error": "ms must be 1..=1000" })),
        );
    }
    let permit = match state.downstream.clone().try_acquire_owned() {
        Ok(permit) => permit,
        Err(_) => {
            state.metrics.rejected.fetch_add(1, Ordering::Relaxed);
            return (
                StatusCode::SERVICE_UNAVAILABLE,
                Json(serde_json::json!({ "error_code": "DOWNSTREAM_SATURATED" })),
            );
        }
    };
    state.metrics.sync_in_use.fetch_add(1, Ordering::Relaxed);
    let result = timeout(
        state.config.job_timeout,
        burn_cpu(Duration::from_millis(millis)),
    )
    .await;
    drop(permit);
    state.metrics.sync_in_use.fetch_sub(1, Ordering::Relaxed);
    match result {
        Ok(checksum) => (
            StatusCode::OK,
            Json(serde_json::json!({
                "status": "computed",
                "cpu_ms": millis,
                "checksum": checksum
            })),
        ),
        Err(_) => (
            StatusCode::GATEWAY_TIMEOUT,
            Json(serde_json::json!({ "error_code": "TIMEOUT" })),
        ),
    }
}

async fn burn_cpu(duration: Duration) -> u64 {
    let started = Instant::now();
    let mut checksum = 0x9e3779b97f4a7c15_u64;
    let mut iterations = 0_u64;
    while started.elapsed() < duration {
        checksum ^= checksum << 7;
        checksum ^= checksum >> 9;
        checksum = checksum.wrapping_add(iterations.wrapping_add(0x517cc1b727220a95));
        iterations = iterations.wrapping_add(1);
        if iterations.is_multiple_of(4096) {
            tokio::task::yield_now().await;
        }
    }
    std::hint::black_box(checksum)
}

async fn batch(
    State(state): State<AppState>,
    Query(query): Query<BatchQuery>,
) -> impl IntoResponse {
    let items = query.items.unwrap_or(100);
    let concurrency = query.concurrency.unwrap_or(8);
    if !(1..=2000).contains(&items) || !(1..=64).contains(&concurrency) {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({
                "error": "items must be 1..=2000 and concurrency must be 1..=64"
            })),
        );
    }

    let work = stream::iter(0..items)
        .map(|_| async {
            sleep(state.config.process_delay).await;
        })
        .buffer_unordered(concurrency)
        .collect::<Vec<()>>();
    if timeout(state.config.job_timeout, work).await.is_err() {
        return (
            StatusCode::GATEWAY_TIMEOUT,
            Json(serde_json::json!({ "error_code": "BATCH_TIMEOUT" })),
        );
    }
    (
        StatusCode::OK,
        Json(serde_json::json!({
            "items": items,
            "completed": items,
            "concurrency": concurrency
        })),
    )
}

async fn metrics(State(state): State<AppState>) -> impl IntoResponse {
    let downstream_in_use =
        state.config.downstream_concurrency - state.downstream.available_permits();
    Json(serde_json::json!({
        "queue_depth": state.metrics.queue_depth.load(Ordering::Relaxed),
        "queue_capacity": state.config.queue_capacity,
        "workers": state.config.workers,
        "jobs_in_flight": state.metrics.in_flight.load(Ordering::Relaxed),
        "downstream_in_use": downstream_in_use,
        "downstream_concurrency": state.config.downstream_concurrency,
        "sync_requests_in_flight": state.metrics.sync_in_use.load(Ordering::Relaxed),
        "jobs_enqueued_total": state.metrics.enqueued.load(Ordering::Relaxed),
        "requests_rejected_total": state.metrics.rejected.load(Ordering::Relaxed),
        "jobs_processed_total": state.metrics.processed.load(Ordering::Relaxed),
        "jobs_failed_total": state.metrics.failed.load(Ordering::Relaxed),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{body::Body, http::Request};
    use tower::ServiceExt;

    fn test_config() -> Config {
        Config {
            process_delay: Duration::from_millis(1),
            job_timeout: Duration::from_secs(1),
            downstream_concurrency: 1,
            rate_per_second: 0.001,
            rate_burst: 1,
            ..Config::default()
        }
    }

    #[tokio::test]
    async fn bounded_queue_rejects_when_full() {
        let mut config = test_config();
        config.queue_capacity = 1;
        let (state, _rx) = AppState::new(config);
        state.tx.try_send(Job { id: 0 }).unwrap();
        state.metrics.queue_depth.store(1, Ordering::Relaxed);

        let response = router(state)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/jobs")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::TOO_MANY_REQUESTS);
    }

    #[tokio::test]
    async fn rate_limiter_rejects_after_burst() {
        let (state, _rx) = AppState::new(test_config());
        let app = router(state);
        for expected in [StatusCode::OK, StatusCode::TOO_MANY_REQUESTS] {
            let response = app
                .clone()
                .oneshot(
                    Request::builder()
                        .uri("/limited")
                        .body(Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(response.status(), expected);
        }
    }

    #[tokio::test]
    async fn sync_route_rejects_when_downstream_is_saturated() {
        let (state, _rx) = AppState::new(test_config());
        let _permit = state.downstream.clone().try_acquire_owned().unwrap();
        let response = router(state)
            .oneshot(Request::builder().uri("/sync").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn batch_rejects_unbounded_input() {
        let (state, _rx) = AppState::new(test_config());
        let response = router(state)
            .oneshot(
                Request::builder()
                    .uri("/batch?items=2001")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    }
}

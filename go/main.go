package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"math/rand"
	"net/http"
	_ "net/http/pprof"
	"os"
	"os/signal"
	"strconv"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"golang.org/x/time/rate"
)

type Config struct {
	Addr                   string
	QueueCapacity          int
	Workers                int
	DownstreamConcurrency  int
	ProcessDelay           time.Duration
	JobTimeout             time.Duration
	RatePerSecond          rate.Limit
	RateBurst              int
	TenantOutstandingLimit int
	MaxRetries             int
	RetryBase              time.Duration
}

type Job struct {
	ID       int64  `json:"id"`
	Tenant   string `json:"tenant"`
	Failures int    `json:"failures"`
}

type Metrics struct {
	Enqueued       atomic.Int64
	Rejected       atomic.Int64
	Processed      atomic.Int64
	Failed         atomic.Int64
	Retries        atomic.Int64
	TenantRejected atomic.Int64
	InFlight       atomic.Int64
	SyncInUse      atomic.Int64
}

type App struct {
	config            Config
	queue             chan Job
	down              chan struct{}
	limiter           *rate.Limiter
	metrics           Metrics
	ids               atomic.Int64
	wg                sync.WaitGroup
	tenantMu          sync.Mutex
	tenantOutstanding map[string]int
}

func NewApp(config Config) *App {
	if config.Addr == "" {
		config.Addr = "127.0.0.1:8080"
	}
	if config.QueueCapacity < 1 {
		config.QueueCapacity = 32
	}
	if config.Workers < 1 {
		config.Workers = 4
	}
	if config.DownstreamConcurrency < 1 {
		config.DownstreamConcurrency = 2
	}
	if config.ProcessDelay <= 0 {
		config.ProcessDelay = 750 * time.Millisecond
	}
	if config.JobTimeout <= 0 {
		config.JobTimeout = 5 * time.Second
	}
	if config.RatePerSecond <= 0 {
		config.RatePerSecond = 10
	}
	if config.RateBurst < 1 {
		config.RateBurst = 20
	}
	if config.MaxRetries < 0 {
		config.MaxRetries = 3
	}
	if config.RetryBase <= 0 {
		config.RetryBase = 25 * time.Millisecond
	}

	return &App{
		config:            config,
		queue:             make(chan Job, config.QueueCapacity),
		down:              make(chan struct{}, config.DownstreamConcurrency),
		limiter:           rate.NewLimiter(config.RatePerSecond, config.RateBurst),
		tenantOutstanding: make(map[string]int),
	}
}

func (a *App) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("POST /jobs", a.handleJob)
	mux.HandleFunc("GET /sync", a.handleSync)
	mux.HandleFunc("GET /cpu", a.handleCPU)
	mux.HandleFunc("GET /limited", a.handleLimited)
	mux.HandleFunc("GET /batch", a.handleBatch)
	mux.HandleFunc("GET /stream", a.handleStream)
	mux.HandleFunc("GET /metrics", a.handleMetrics)
	return mux
}

func (a *App) StartWorkers() {
	for workerID := 0; workerID < a.config.Workers; workerID++ {
		a.wg.Add(1)
		go a.worker(workerID)
	}
}

func (a *App) StopWorkers() {
	close(a.queue)
	a.wg.Wait()
}

func (a *App) handleJob(w http.ResponseWriter, r *http.Request) {
	tenant := r.URL.Query().Get("tenant")
	if tenant == "" {
		tenant = "default"
	}
	if tenant != "alpha" && tenant != "beta" && tenant != "default" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "tenant must be alpha, beta, or default"})
		return
	}
	failures, err := queryInt(r, "failures", 0, 0, 10)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	job := Job{ID: a.ids.Add(1), Tenant: tenant, Failures: failures}
	if !a.reserveTenant(tenant) {
		a.metrics.Rejected.Add(1)
		a.metrics.TenantRejected.Add(1)
		writeJSON(w, http.StatusTooManyRequests, map[string]string{"error_code": "TENANT_BUSY"})
		return
	}
	select {
	case a.queue <- job:
		a.metrics.Enqueued.Add(1)
		writeJSON(w, http.StatusAccepted, map[string]any{"status": "queued", "job_id": job.ID})
	default:
		a.releaseTenant(tenant)
		a.metrics.Rejected.Add(1)
		w.Header().Set("Retry-After", "1")
		writeJSON(w, http.StatusTooManyRequests, map[string]string{
			"error_code": "QUEUE_FULL",
			"message":    "server is busy; retry later",
		})
	}
}

func (a *App) handleSync(w http.ResponseWriter, r *http.Request) {
	select {
	case a.down <- struct{}{}:
		defer func() { <-a.down }()
		a.metrics.SyncInUse.Add(1)
		defer a.metrics.SyncInUse.Add(-1)
	case <-r.Context().Done():
		return
	default:
		a.metrics.Rejected.Add(1)
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{
			"error_code": "DOWNSTREAM_SATURATED",
			"message":    "downstream concurrency limit reached",
		})
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), a.config.JobTimeout)
	defer cancel()
	if err := sleepContext(ctx, a.config.ProcessDelay); err != nil {
		writeJSON(w, http.StatusGatewayTimeout, map[string]string{"error_code": "TIMEOUT"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "processed"})
}

func (a *App) handleLimited(w http.ResponseWriter, _ *http.Request) {
	if !a.limiter.Allow() {
		a.metrics.Rejected.Add(1)
		w.Header().Set("Retry-After", "1")
		writeJSON(w, http.StatusTooManyRequests, map[string]string{"error_code": "RATE_LIMITED"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "allowed"})
}

func (a *App) handleCPU(w http.ResponseWriter, r *http.Request) {
	millis, err := queryInt(r, "ms", 50, 1, 1000)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	select {
	case a.down <- struct{}{}:
		defer func() { <-a.down }()
		a.metrics.SyncInUse.Add(1)
		defer a.metrics.SyncInUse.Add(-1)
	default:
		a.metrics.Rejected.Add(1)
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error_code": "DOWNSTREAM_SATURATED"})
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), a.config.JobTimeout)
	defer cancel()
	checksum, err := burnCPU(ctx, time.Duration(millis)*time.Millisecond)
	if err != nil {
		writeJSON(w, http.StatusGatewayTimeout, map[string]string{"error_code": "TIMEOUT"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "computed", "cpu_ms": millis, "checksum": checksum})
}

func burnCPU(ctx context.Context, duration time.Duration) (uint64, error) {
	started := time.Now()
	checksum := uint64(0x9e3779b97f4a7c15)
	for time.Since(started) < duration {
		for i := uint64(0); i < 4096; i++ {
			checksum ^= checksum << 7
			checksum ^= checksum >> 9
			checksum += i + 0x517cc1b727220a95
		}
		select {
		case <-ctx.Done():
			return checksum, ctx.Err()
		default:
		}
	}
	return checksum, nil
}

func (a *App) handleBatch(w http.ResponseWriter, r *http.Request) {
	items, err := queryInt(r, "items", 100, 1, 2000)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	concurrency, err := queryInt(r, "concurrency", 8, 1, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), a.config.JobTimeout)
	defer cancel()
	sem := make(chan struct{}, concurrency)
	var wg sync.WaitGroup
	var completed atomic.Int64
	for i := 0; i < items; i++ {
		select {
		case sem <- struct{}{}:
		case <-ctx.Done():
			writeJSON(w, http.StatusGatewayTimeout, map[string]string{"error_code": "BATCH_TIMEOUT"})
			return
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer func() { <-sem }()
			if sleepContext(ctx, a.config.ProcessDelay) == nil {
				completed.Add(1)
			}
		}()
	}
	wg.Wait()
	if err := ctx.Err(); err != nil {
		writeJSON(w, http.StatusGatewayTimeout, map[string]string{"error_code": "BATCH_TIMEOUT"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]int64{"items": int64(items), "completed": completed.Load(), "concurrency": int64(concurrency)})
}

func (a *App) handleStream(w http.ResponseWriter, r *http.Request) {
	items, err := queryInt(r, "items", 20, 1, 1000)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	delayMS, err := queryInt(r, "delay_ms", 10, 0, 1000)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming unsupported", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	ctx := r.Context()
	for item := 1; item <= items; item++ {
		if _, err := fmt.Fprintf(w, "%d\n", item); err != nil {
			return
		}
		flusher.Flush()
		if err := sleepContext(ctx, time.Duration(delayMS)*time.Millisecond); err != nil {
			return
		}
	}
}

func (a *App) handleMetrics(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"queue_depth":              len(a.queue),
		"queue_capacity":           cap(a.queue),
		"workers":                  a.config.Workers,
		"jobs_in_flight":           a.metrics.InFlight.Load(),
		"downstream_in_use":        len(a.down),
		"downstream_concurrency":   cap(a.down),
		"sync_requests_in_flight":  a.metrics.SyncInUse.Load(),
		"jobs_enqueued_total":      a.metrics.Enqueued.Load(),
		"requests_rejected_total":  a.metrics.Rejected.Load(),
		"jobs_processed_total":     a.metrics.Processed.Load(),
		"jobs_failed_total":        a.metrics.Failed.Load(),
		"jobs_retries_total":       a.metrics.Retries.Load(),
		"tenant_rejected_total":    a.metrics.TenantRejected.Load(),
		"tenant_outstanding_limit": a.config.TenantOutstandingLimit,
	})
}

func (a *App) worker(workerID int) {
	defer a.wg.Done()
	for job := range a.queue {
		a.metrics.InFlight.Add(1)
		err := a.processJob(job)
		if err != nil {
			a.metrics.Failed.Add(1)
			log.Printf("worker=%d job=%d failed: %v", workerID, job.ID, err)
		} else {
			a.metrics.Processed.Add(1)
		}
		a.releaseTenant(job.Tenant)
		a.metrics.InFlight.Add(-1)
	}
}

func (a *App) processJob(job Job) error {
	ctx, cancel := context.WithTimeout(context.Background(), a.config.JobTimeout)
	defer cancel()
	select {
	case a.down <- struct{}{}:
		defer func() { <-a.down }()
	case <-ctx.Done():
		return ctx.Err()
	}

	for attempt := 0; ; attempt++ {
		if err := sleepContext(ctx, a.config.ProcessDelay); err != nil {
			return err
		}
		if attempt >= job.Failures {
			return nil
		}
		if attempt >= a.config.MaxRetries {
			return fmt.Errorf("retry limit reached after %d attempts", attempt+1)
		}
		a.metrics.Retries.Add(1)
		backoff := a.config.RetryBase * time.Duration(1<<attempt)
		jitter := time.Duration(rand.Int63n(int64(max(time.Millisecond, backoff/4))))
		if err := sleepContext(ctx, backoff+jitter); err != nil {
			return err
		}
	}
}

func (a *App) reserveTenant(tenant string) bool {
	if a.config.TenantOutstandingLimit <= 0 {
		return true
	}
	a.tenantMu.Lock()
	defer a.tenantMu.Unlock()
	if a.tenantOutstanding[tenant] >= a.config.TenantOutstandingLimit {
		return false
	}
	a.tenantOutstanding[tenant]++
	return true
}

func (a *App) releaseTenant(tenant string) {
	if a.config.TenantOutstandingLimit <= 0 {
		return
	}
	a.tenantMu.Lock()
	defer a.tenantMu.Unlock()
	if a.tenantOutstanding[tenant] <= 1 {
		delete(a.tenantOutstanding, tenant)
	} else {
		a.tenantOutstanding[tenant]--
	}
}

func (a *App) Handler() http.Handler { return a.Routes() }

func queryInt(r *http.Request, name string, fallback, min, max int) (int, error) {
	value := r.URL.Query().Get(name)
	if value == "" {
		return fallback, nil
	}
	n, err := strconv.Atoi(value)
	if err != nil || n < min || n > max {
		return 0, fmt.Errorf("%s must be between %d and %d", name, min, max)
	}
	return n, nil
}

func sleepContext(ctx context.Context, duration time.Duration) error {
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func envInt(name string, fallback int) int {
	value, err := strconv.Atoi(os.Getenv(name))
	if err != nil || value < 1 {
		return fallback
	}
	return value
}

func envIntAllowZero(name string, fallback int) int {
	value, err := strconv.Atoi(os.Getenv(name))
	if err != nil || value < 0 {
		return fallback
	}
	return value
}

func envDuration(name string, fallback time.Duration) time.Duration {
	value, err := strconv.Atoi(os.Getenv(name))
	if err != nil || value < 1 {
		return fallback
	}
	return time.Duration(value) * time.Millisecond
}

func main() {
	config := Config{
		Addr:                   envString("ADDR", "127.0.0.1:8080"),
		QueueCapacity:          envInt("QUEUE_CAPACITY", 32),
		Workers:                envInt("WORKERS", 4),
		DownstreamConcurrency:  envInt("DOWNSTREAM_CONCURRENCY", 2),
		ProcessDelay:           envDuration("PROCESS_DELAY_MS", 750*time.Millisecond),
		JobTimeout:             envDuration("JOB_TIMEOUT_MS", 5*time.Second),
		RatePerSecond:          rate.Limit(envInt("RATE_PER_SECOND", 10)),
		RateBurst:              envInt("RATE_BURST", 20),
		TenantOutstandingLimit: envInt("TENANT_OUTSTANDING_LIMIT", 0),
		MaxRetries:             envIntAllowZero("MAX_RETRIES", 3),
		RetryBase:              envDuration("RETRY_BASE_MS", 25*time.Millisecond),
	}
	app := NewApp(config)
	app.StartWorkers()
	server := &http.Server{Addr: config.Addr, Handler: app.Routes(), ReadHeaderTimeout: 3 * time.Second}

	serverErr := make(chan error, 1)
	go func() {
		log.Printf("Go example listening on %s", config.Addr)
		serverErr <- server.ListenAndServe()
	}()

	var pprofServer *http.Server
	if os.Getenv("ENABLE_PPROF") == "1" {
		pprofAddr := envString("PPROF_ADDR", "127.0.0.1:6060")
		pprofServer = &http.Server{Addr: pprofAddr, Handler: http.DefaultServeMux, ReadHeaderTimeout: 3 * time.Second}
		go func() {
			if err := pprofServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
				log.Printf("local pprof listener failed: %v", err)
			}
		}()
		log.Printf("pprof enabled on %s", pprofAddr)
	}

	signals, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	select {
	case <-signals.Done():
	case err := <-serverErr:
		if !errors.Is(err, http.ErrServerClosed) {
			log.Printf("HTTP server failed: %v", err)
		}
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := server.Shutdown(ctx); err != nil {
		log.Printf("HTTP shutdown: %v", err)
	}
	if pprofServer != nil {
		if err := pprofServer.Shutdown(ctx); err != nil {
			log.Printf("pprof shutdown: %v", err)
		}
	}
	app.StopWorkers()
}

func envString(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

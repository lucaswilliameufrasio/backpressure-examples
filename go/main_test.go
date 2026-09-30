package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"golang.org/x/time/rate"
)

func TestJobsRejectWhenBoundedQueueIsFull(t *testing.T) {
	app := NewApp(Config{QueueCapacity: 1, Workers: 1})
	app.queue <- Job{ID: 99}

	request := httptest.NewRequest(http.MethodPost, "/jobs", nil)
	response := httptest.NewRecorder()
	app.Routes().ServeHTTP(response, request)

	if response.Code != http.StatusTooManyRequests {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusTooManyRequests)
	}
	if app.metrics.Rejected.Load() != 1 {
		t.Fatalf("rejected = %d, want 1", app.metrics.Rejected.Load())
	}
}

func TestSyncRejectsWhenDownstreamIsSaturated(t *testing.T) {
	app := NewApp(Config{DownstreamConcurrency: 1, ProcessDelay: time.Millisecond})
	app.down <- struct{}{}

	request := httptest.NewRequest(http.MethodGet, "/sync", nil)
	response := httptest.NewRecorder()
	app.Routes().ServeHTTP(response, request)

	if response.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusServiceUnavailable)
	}
}

func TestLimitedRouteRejectsAfterBurst(t *testing.T) {
	app := NewApp(Config{RatePerSecond: rate.Limit(0.001), RateBurst: 1})
	handler := app.Routes()

	for i, want := range []int{http.StatusOK, http.StatusTooManyRequests} {
		request := httptest.NewRequest(http.MethodGet, "/limited", nil)
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if response.Code != want {
			t.Fatalf("request %d status = %d, want %d", i+1, response.Code, want)
		}
	}
}

func TestBatchBoundsInput(t *testing.T) {
	app := NewApp(Config{ProcessDelay: time.Millisecond, JobTimeout: time.Second})
	request := httptest.NewRequest(http.MethodGet, "/batch?items=0", nil)
	response := httptest.NewRecorder()
	app.Routes().ServeHTTP(response, request)
	if response.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want %d", response.Code, http.StatusBadRequest)
	}
}

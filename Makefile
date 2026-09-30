SHELL := /bin/sh
MISE_EXEC := $(shell command -v mise >/dev/null 2>&1 && printf 'mise exec -- ')
STACK ?= go
SCENARIO ?= queue-saturation
REPEATS ?= 3

.PHONY: help doctor setup check test check-go check-rust check-node check-elixir check-python \
	run-go run-rust run-node run-elixir run-all load-smoke benchmark profile profile-go ebpf-check ebpf-probe

help:
	@printf '%s\n' \
	  'setup        install pinned tools (mise) and language dependencies' \
	  'doctor       show required and optional local tools' \
	  'run-all      start all four servers on free loopback ports' \
	  'run-<stack>  start one of go, rust, node, elixir' \
	  'check        format, lint, test and build every stack' \
	  'load-smoke   short queue-saturation test on every stack' \
	  'benchmark    STACK=go SCENARIO=queue-saturation REPEATS=3' \
	  'profile      show runtime profiling guide' \
	  'profile-go   record a sanitized local Go pprof CPU profile' \
	  'ebpf-check   check and record optional Linux profiler readiness' \
	  'ebpf-probe   explicitly attempt a minimal local eBPF program'

doctor:
	python3 scripts/doctor.py

setup:
	@if command -v mise >/dev/null 2>&1; then mise install go rust node pnpm erlang elixir golangci-lint oha; fi
	cd go && $(MISE_EXEC)go mod download
	$(MISE_EXEC)cargo fetch --locked --manifest-path rust/Cargo.toml
	cd node && $(MISE_EXEC)pnpm install --frozen-lockfile
	cd elixir && $(MISE_EXEC)mix local.hex --force
	cd elixir && $(MISE_EXEC)mix local.rebar --force
	cd elixir && $(MISE_EXEC)mix deps.get

check-go:
	cd go && test -z "$$($(MISE_EXEC)gofmt -l .)"
	cd go && $(MISE_EXEC)go test -race -count=1 ./...
	cd go && $(MISE_EXEC)go vet ./...
	cd go && $(MISE_EXEC)golangci-lint run ./...
	cd go && $(MISE_EXEC)go build -o "$${TMPDIR:-/tmp}/backpressure-examples-go" .

check-rust:
	cd rust && $(MISE_EXEC)cargo fmt --check
	cd rust && $(MISE_EXEC)cargo test --locked
	cd rust && $(MISE_EXEC)cargo clippy --locked --all-targets -- -D warnings
	cd rust && $(MISE_EXEC)cargo build --locked --release

check-node:
	cd node && $(MISE_EXEC)pnpm install --frozen-lockfile
	cd node && $(MISE_EXEC)pnpm check
	cd node && $(MISE_EXEC)pnpm test

check-elixir:
	cd elixir && $(MISE_EXEC)mix format --check-formatted
	cd elixir && $(MISE_EXEC)mix test
	cd elixir && $(MISE_EXEC)mix compile --warnings-as-errors

check-python:
	$(MISE_EXEC)python3 -m unittest scripts.test_reporting
	$(MISE_EXEC)python3 -m py_compile scripts/*.py

test: check-go check-rust check-node check-elixir check-python

check: doctor test

run-go:
	$(MISE_EXEC)python3 scripts/dev.py --stack go

run-rust:
	$(MISE_EXEC)python3 scripts/dev.py --stack rust

run-node:
	$(MISE_EXEC)python3 scripts/dev.py --stack node

run-elixir:
	$(MISE_EXEC)python3 scripts/dev.py --stack elixir

run-all:
	$(MISE_EXEC)python3 scripts/dev.py --stack all

load-smoke:
	$(MISE_EXEC)python3 scripts/benchmark.py --stack all --scenario queue-saturation --requests 100 --concurrency 40 --repeats 1

benchmark:
	$(MISE_EXEC)python3 scripts/benchmark.py --stack $(STACK) --scenario $(SCENARIO) --repeats $(REPEATS)

profile:
	@$(MISE_EXEC)python3 -c 'from pathlib import Path; print(Path("docs/profiling.md").read_text())'

profile-go:
	$(MISE_EXEC)python3 scripts/profile_go.py

ebpf-check:
	$(MISE_EXEC)python3 scripts/ebpf_check.py

ebpf-probe:
	$(MISE_EXEC)python3 scripts/ebpf_check.py --run-probe

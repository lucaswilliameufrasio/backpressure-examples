from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.benchmark import _assert_allowlisted, summarize_oha, validate_report
from scripts.collect_environment import _source_status_entries_clean, collect_environment
from scripts.doctor import ebpf_install_hint
from scripts.ebpf_check import (
    classify_probe,
    create_report as create_ebpf_report,
    validate_report as validate_ebpf_report,
)
from scripts.profile_go import summarize_pprof, validate_profile_report


def environment_stub():
    return {
        "os": {
            "family": "linux",
            "distribution_id": None,
            "distribution_version": None,
            "kernel_version": None,
            "architecture": "test",
        },
        "hardware": {"cpu_model": None, "logical_cores": None, "memory_gib_rounded": None},
        "container_limits": {"cpu_cores_rounded": None, "memory_gib_rounded": None},
        "tools": {},
        "git_commit": None,
        "working_tree_clean": False,
        "python": "3.12.0",
    }


def valid_report():
    return {
        "schema_version": 1,
        "run_id": "0123456789ab",
        "started_at": "2026-01-01T00:00:00Z",
        "git_commit": None,
        "stack": "go",
        "scenario": "queue-saturation",
        "configuration": {
            "queue_capacity": 1,
            "workers": 1,
            "downstream_concurrency": 1,
            "process_delay_ms": 10,
            "job_timeout_ms": 1000,
            "tenant_outstanding_limit": 0,
            "max_retries": 1,
            "retry_base_ms": 5,
            "repeats": 1,
            "scenario_parameters": {"requests": 10, "concurrency": 5},
        },
        "environment": environment_stub(),
        "load_generator": {"name": "oha", "version": "oha test"},
        "repetitions": [
            {
                "index": 1,
                "parameters": {"requests": 10, "concurrency": 5},
                "http": {
                    "requests": 10,
                    "status_codes": {"202": 2, "429": 8},
                    "transport_errors": 0,
                    "http_2xx_rate": 0.2,
                    "oha_success_rate": 1.0,
                    "requests_per_second": 100.0,
                    "duration_seconds": 0.1,
                    "latency_ms": {"p50": 1.0, "p95": 2.0, "p99": 3.0},
                    "bytes_per_second": 1000.0,
                },
                "service_resources": {
                    "sampled": False,
                    "average_cpu_cores": None,
                    "peak_rss_mib": None,
                    "sample_interval_ms": 100,
                },
                "application_metrics": {"before": {}, "after_load": {}, "after_drain": {}},
                "tenant_probe": None,
                "graceful_shutdown": None,
            }
        ],
        "aggregate": {
            "requests_per_second_median": 100.0,
            "latency_ms_median": {"p50": 1.0, "p95": 2.0, "p99": 3.0},
        },
    }


class ReportPrivacyTests(unittest.TestCase):
    def test_ebpf_setup_hint_uses_the_os_package_manager(self):
        self.assertEqual(ebpf_install_hint("cachyos"), "sudo pacman -S bpftrace")
        self.assertEqual(ebpf_install_hint("ubuntu"), "sudo apt install bpftrace")
        self.assertIsNone(ebpf_install_hint("unknown-linux"))

    def test_generated_reports_do_not_make_source_tree_dirty(self):
        self.assertTrue(
            _source_status_entries_clean(
                [
                    b"?? benchmarks/results/run.json",
                    b"?? benchmarks/profiling/results/profile.md",
                ]
            )
        )
        self.assertFalse(_source_status_entries_clean([b" M README.md"]))

    def test_environment_collector_returns_only_allowlisted_sections(self):
        environment = collect_environment()
        self.assertEqual(
            set(environment),
            {
                "os",
                "hardware",
                "container_limits",
                "tools",
                "git_commit",
                "working_tree_clean",
                "python",
            },
        )
        self.assertNotIn("hostname", json.dumps(environment).lower())
        self.assertNotIn("username", json.dumps(environment).lower())

    def test_schema_accepts_a_complete_safe_report(self):
        validate_report(valid_report())

    def test_report_rejects_sensitive_field_names(self):
        with self.assertRaisesRegex(ValueError, "sensitive field"):
            _assert_allowlisted({"hostname": "private-host"})

    def test_report_rejects_personal_paths(self):
        with self.assertRaisesRegex(ValueError, "filesystem path"):
            _assert_allowlisted({"note": "/home/person/private"})

    def test_ebpf_readiness_record_contains_no_raw_process_data(self):
        report = create_ebpf_report(probe_requested=False)
        self.assertFalse(report["probe"]["requested"])
        encoded = json.dumps(report).lower()
        self.assertNotIn("hostname", encoded)
        self.assertNotIn("username", encoded)
        self.assertNotIn("pid", encoded)

        legacy_record = create_ebpf_report(probe_requested=False)
        legacy_record["probe"].pop("privileged")
        validate_ebpf_report(legacy_record)

    def test_ebpf_probe_permission_errors_are_classified(self):
        self.assertEqual(
            classify_probe(1, "", "ERROR: Missing CAP_DAC_READ_SEARCH capability"),
            "permission-denied",
        )
        self.assertEqual(classify_probe(0, "", ""), "passed")


class OhaSummaryTests(unittest.TestCase):
    def test_summary_keeps_only_aggregate_http_measurements(self):
        summary = summarize_oha(
            {
                "summary": {"successRate": 1.0, "total": 2.0, "requestsPerSec": 5.0, "sizePerSec": 100.0},
                "latencyPercentiles": {"p50": 0.01, "p95": 0.02, "p99": 0.03},
                "statusCodeDistribution": {"202": 8, "429": 2},
                "errorDistribution": {"raw-detail-is-discarded": 1},
            }
        )
        self.assertEqual(summary["requests"], 11)
        self.assertEqual(summary["status_codes"], {"202": 8, "429": 2})
        self.assertEqual(summary["transport_errors"], 1)
        self.assertEqual(summary["latency_ms"]["p95"], 20.0)
        self.assertNotIn("raw-detail-is-discarded", json.dumps(summary))

    def test_pprof_summary_keeps_safe_symbols_and_discards_raw_output(self):
        profile = summarize_pprof(
            "Total samples = 2.50s\n  1.50s 60.00% 60.00% 1.80s 72.00% main.burnCPU\n",
            duration_seconds=2,
        )
        self.assertEqual(profile["total_cpu_samples_seconds"], 2.5)
        self.assertEqual(profile["top_functions"][0]["symbol"], "main.burnCPU")
        self.assertFalse(profile["raw_profile_retained"])

        report = {
            "schema_version": 1,
            "run_id": "0123456789ab",
            "started_at": "2026-01-01T00:00:00Z",
            "git_commit": None,
            "working_tree_clean": False,
            "stack": "go",
            "profiler": "go tool pprof",
            "environment": environment_stub(),
            "configuration": {},
            "load_result": {},
            "application_metrics": {},
            "service_resources": {},
            "profile_summary": profile,
            "graceful_shutdown": {"duration_seconds": 0.01, "exit_code": 0, "timed_out": False},
        }
        validate_profile_report(report)


if __name__ == "__main__":
    unittest.main()

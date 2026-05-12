# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the T4.8.1 multi-process Prometheus registry plumbing.

These tests assert two distinct things:

  1. `vargate_telemetry.metrics.get_registry()` returns the right
     registry shape depending on whether
     `PROMETHEUS_MULTIPROC_DIR` is set (the dev/multi-process toggle).
  2. An observation emitted inside a *separate Python process* is
     visible at the parent's registry-build time — the cross-process
     property the gateway's /metrics endpoint depends on. Without this,
     T4.7's `time_to_first_pull` histogram (observed in celery-worker)
     would stay invisible to the gateway forever, which is exactly the
     T4.8 finding this sprint is fixing.

The cross-process test uses `multiprocessing.Process` to spawn a real
OS child, increment a uniquely-named Counter inside it, and verify the
parent's `MultiProcessCollector`-backed registry surfaces the
increment after the child joins. Anything less (mocking, monkeypatch
of os.fork, threads) would silently pass even if the plumbing were
broken — single-process Python sees its own `REGISTRY` regardless.
"""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import pytest
from prometheus_client import REGISTRY, Counter


# ───────────────────────────────────────────────────────────────────────────
# Fixtures
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def multiproc_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Set up an isolated PROMETHEUS_MULTIPROC_DIR for the test.

    Each test gets a fresh dir, so stale files from a previous test's
    Counter writes never bleed in. The env var is restored on teardown
    (`monkeypatch` handles that for us).
    """
    prom_dir = tmp_path / "prom_multiproc"
    prom_dir.mkdir()
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(prom_dir))
    yield prom_dir


# ───────────────────────────────────────────────────────────────────────────
# get_registry() return-shape tests
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    bool(os.environ.get("PROMETHEUS_MULTIPROC_DIR")),
    reason=(
        "Test runs in dev mode (no PROMETHEUS_MULTIPROC_DIR). "
        "`prometheus_client.values` pins its storage backend at import "
        "time based on the env var; deleting the env after-the-fact "
        "wedges the library mid-construction. Skip when running inside "
        "the compose container (which always has the env set)."
    ),
)
def test_get_registry_returns_default_when_env_missing() -> None:
    """No PROMETHEUS_MULTIPROC_DIR → fall back to in-process REGISTRY.

    This is the dev / unit-test path on a developer's laptop without
    docker compose: `get_registry()` must return the default REGISTRY
    so existing `test_onboarding_metrics.py` assertions still find
    their samples. Skipped inside the compose container — the test
    requires the env var to be unset at *prometheus_client import
    time*, not just at test execution time, and we can't unwind that.
    """
    from vargate_telemetry.metrics import get_registry

    assert get_registry() is REGISTRY


def test_get_registry_returns_fresh_multiprocess_registry(
    multiproc_dir: Path,
) -> None:
    """With env set → fresh CollectorRegistry + MultiProcessCollector.

    Two calls return two *different* registry instances (each scrape
    builds its own — that's the documented prometheus_client pattern
    for multiproc mode, ensures no stale-data caching).
    """
    from prometheus_client import CollectorRegistry

    from vargate_telemetry.metrics import get_registry

    r1 = get_registry()
    r2 = get_registry()

    assert isinstance(r1, CollectorRegistry)
    assert r1 is not REGISTRY
    assert r1 is not r2  # fresh per call


# ───────────────────────────────────────────────────────────────────────────
# Cross-process observation — the real test
# ───────────────────────────────────────────────────────────────────────────


def _subprocess_increment(counter_name: str, increments: int) -> None:
    """Target fn for the spawned child process.

    Imports prometheus_client AFTER the parent has set
    PROMETHEUS_MULTIPROC_DIR (which the fork inherits). Creates a fresh
    Counter under `counter_name` and increments it the requested
    number of times. The mmap'd files this writes survive process
    exit; the parent's MultiProcessCollector reads them.
    """
    # The child inherits PROMETHEUS_MULTIPROC_DIR from the parent's env;
    # the prometheus_client library checks for it at instrument-create
    # time and switches its storage backend.
    from prometheus_client import Counter as _Counter

    c = _Counter(counter_name, f"Test counter named {counter_name}")
    for _ in range(increments):
        c.inc()


def test_subprocess_observation_visible_via_multiprocess_collector(
    multiproc_dir: Path,
) -> None:
    """The smoking-gun test: an increment from a forked child shows up
    in the parent's MultiProcessCollector-backed registry.

    If this passes, the structural T4.8 gap is closed:
    celery-worker-side observations of `time_to_first_pull` will be
    visible at the gateway's /metrics scrape.
    """
    counter_name = "vargate_t481_subprocess_counter_visible"
    expected_increments = 3

    # Spawn the child with `fork` context so the env var is inherited.
    # On Linux the default context is already `fork`; being explicit
    # here pins behavior for any future port.
    ctx = multiprocessing.get_context("fork")
    proc = ctx.Process(
        target=_subprocess_increment,
        args=(counter_name, expected_increments),
    )
    proc.start()
    proc.join(timeout=10)
    assert proc.exitcode == 0, (
        f"subprocess exited with {proc.exitcode}; "
        f"check stderr for prometheus_client import errors"
    )

    # Now build the parent-side registry and look for the sample.
    from vargate_telemetry.metrics import get_registry

    registry = get_registry()
    samples = {
        sample.name: sample.value
        for metric in registry.collect()
        for sample in metric.samples
    }

    # Counters under multiproc mode expose `<name>_total` as the
    # sample name (the _total suffix is added by prometheus_client at
    # serialization time, matching the OpenMetrics convention).
    assert samples.get(f"{counter_name}_total") == float(expected_increments), (
        f"expected {counter_name}_total = {expected_increments}, "
        f"got samples={list(samples.keys())[:20]}"
    )


def test_two_subprocesses_aggregate_correctly(multiproc_dir: Path) -> None:
    """Multiple workers all writing to the same Counter sum cleanly.

    The real onboarding flow has one gateway process plus a celery
    worker pool of N children. Each child's observations should add.
    This test pins the aggregation property with two children.
    """
    counter_name = "vargate_t481_two_subprocess_counter"
    per_child_increments = 5
    num_children = 2

    ctx = multiprocessing.get_context("fork")
    procs = [
        ctx.Process(
            target=_subprocess_increment,
            args=(counter_name, per_child_increments),
        )
        for _ in range(num_children)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=10)
        assert p.exitcode == 0

    from vargate_telemetry.metrics import get_registry

    registry = get_registry()
    samples = {
        sample.name: sample.value
        for metric in registry.collect()
        for sample in metric.samples
    }

    expected_sum = float(per_child_increments * num_children)
    assert samples.get(f"{counter_name}_total") == expected_sum


# ───────────────────────────────────────────────────────────────────────────
# Worker shutdown hook
# ───────────────────────────────────────────────────────────────────────────


def test_worker_shutdown_hook_marks_process_dead(
    multiproc_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The signal handler in `celery_app` calls
    `multiprocess.mark_process_dead(pid)` when a prefork worker exits.

    Mocks `mark_process_dead`, fires the signal manually with a fake
    pid, asserts the mock was called with that pid. We don't need a
    real worker shutdown to test this — the connection is what we're
    pinning.
    """
    import prometheus_client.multiprocess as multiprocess_mod

    from vargate_telemetry.celery_app import _cleanup_prometheus_multiproc

    mock_mark = MagicMock()
    monkeypatch.setattr(multiprocess_mod, "mark_process_dead", mock_mark)

    fake_pid = 12345
    _cleanup_prometheus_multiproc(pid=fake_pid, exitcode=0)

    mock_mark.assert_called_once_with(fake_pid)


def test_worker_shutdown_hook_noop_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without PROMETHEUS_MULTIPROC_DIR, the hook returns without
    touching prometheus_client.multiprocess.

    Defensive: the import inside the hook is gated on the env var,
    so dev environments without the multiproc dir set won't ever
    import the module — and even if they did, mark_process_dead
    would be a no-op there. Just pin the early return.
    """
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    import prometheus_client.multiprocess as multiprocess_mod

    from vargate_telemetry.celery_app import _cleanup_prometheus_multiproc

    mock_mark = MagicMock()
    monkeypatch.setattr(multiprocess_mod, "mark_process_dead", mock_mark)

    _cleanup_prometheus_multiproc(pid=99999, exitcode=0)

    mock_mark.assert_not_called()

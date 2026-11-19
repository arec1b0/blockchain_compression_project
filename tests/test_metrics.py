# test_metrics.py

import json
import threading

import pytest

from blockchain_compression.observability import (
    HistogramSnapshot,
    MetricsRegistry,
    get_metrics_registry,
)
from blockchain_compression.observability.metrics import _escape_label_value


def test_increment_counter_accumulates():
    m = MetricsRegistry()
    m.increment_counter("events_total")
    m.increment_counter("events_total", value=2.5)
    assert m.get_counter("events_total") == 3.5


def test_increment_counter_default_value_is_one():
    m = MetricsRegistry()
    m.increment_counter("events_total")
    assert m.get_counter("events_total") == 1.0


def test_increment_counter_rejects_negative():
    m = MetricsRegistry()
    with pytest.raises(ValueError):
        m.increment_counter("events_total", value=-1)


def test_set_gauge_overwrites():
    m = MetricsRegistry()
    m.set_gauge("temperature", 10.0)
    m.set_gauge("temperature", 20.0)
    assert m.get_gauge("temperature") == 20.0


def test_labels_distinguish_separate_series():
    m = MetricsRegistry()
    m.set_gauge("compression_ratio", 3.0, labels={"block_index": "1"})
    m.set_gauge("compression_ratio", 4.0, labels={"block_index": "2"})
    assert m.get_gauge("compression_ratio", labels={"block_index": "1"}) == 3.0
    assert m.get_gauge("compression_ratio", labels={"block_index": "2"}) == 4.0


def test_metric_name_validation_rejects_dashes():
    m = MetricsRegistry()
    with pytest.raises(ValueError):
        m.increment_counter("bad-name")


def test_kind_mismatch_raises():
    m = MetricsRegistry()
    m.increment_counter("thing_total")
    with pytest.raises(ValueError):
        m.set_gauge("thing_total", 1.0)


def test_observe_histogram_buckets_are_cumulative():
    m = MetricsRegistry()
    m.observe_histogram("latency_seconds", 0.0003, buckets=(0.001, 0.01, 0.1))
    m.observe_histogram("latency_seconds", 0.05, buckets=(0.001, 0.01, 0.1))

    snapshot = m.get_histogram("latency_seconds")
    assert isinstance(snapshot, HistogramSnapshot)
    assert snapshot.buckets[0.001] == 1  # only the 0.0003 observation
    assert snapshot.buckets[0.01] == 1  # still just 0.0003
    assert snapshot.buckets[0.1] == 2  # both observations
    assert snapshot.buckets[float("inf")] == 2  # +Inf always equals the total


def test_observe_histogram_sum_and_count():
    m = MetricsRegistry()
    m.observe_histogram("latency_seconds", 0.1)
    m.observe_histogram("latency_seconds", 0.2)

    snapshot = m.get_histogram("latency_seconds")
    assert snapshot is not None
    assert snapshot.count == 2
    assert snapshot.sum == pytest.approx(0.3)


def test_histogram_buckets_fixed_after_first_observation():
    m = MetricsRegistry()
    m.observe_histogram("latency_seconds", 1.0, buckets=(1.0, 2.0))
    m.observe_histogram("latency_seconds", 1.0, buckets=(100.0, 200.0))  # ignored - already set

    snapshot = m.get_histogram("latency_seconds")
    assert snapshot is not None
    assert 1.0 in snapshot.buckets
    assert 100.0 not in snapshot.buckets


def test_metric_accessors_return_none_when_missing_and_read_only_snapshots():
    m = MetricsRegistry()
    assert m.get_counter("missing_total") is None
    assert m.get_gauge("missing_gauge") is None
    assert m.get_histogram("missing_hist") is None

    m.observe_histogram("latency_seconds", 0.05, buckets=(0.1,))
    snapshot = m.get_histogram("latency_seconds")
    assert snapshot is not None
    with pytest.raises(TypeError):
        snapshot.buckets[0.1] = 999  # MappingProxyType is read-only

    # Reading a missing series must not register it.
    assert m.to_prometheus_text().count("# TYPE") == 1


def test_to_json_is_valid_and_matches_recorded_values():
    m = MetricsRegistry()
    m.increment_counter("blocks_added_total", value=3)
    m.set_gauge("compression_ratio", 2.5, labels={"block_index": "1"})
    m.observe_histogram("proof_generation_seconds", 0.01)

    parsed = json.loads(m.to_json())
    assert parsed["counters"] == [{"name": "blocks_added_total", "labels": {}, "value": 3.0}]
    assert parsed["gauges"][0]["value"] == 2.5
    assert parsed["histograms"][0]["count"] == 1
    assert parsed["histograms"][0]["sum"] == pytest.approx(0.01)


def test_to_json_is_deterministic():
    m = MetricsRegistry()
    m.increment_counter("a_total")
    m.increment_counter("b_total")
    m.set_gauge("g", 1.0, labels={"z": "1", "a": "2"})

    assert m.to_json() == m.to_json()


def test_empty_registry_exports_cleanly():
    m = MetricsRegistry()
    assert json.loads(m.to_json()) == {"counters": [], "gauges": [], "histograms": []}
    assert m.to_prometheus_text() == ""


def test_to_prometheus_text_has_help_and_type_lines():
    m = MetricsRegistry()
    m.increment_counter("blocks_added_total", help_text="Total blocks added")
    text = m.to_prometheus_text()

    assert "# HELP blocks_added_total Total blocks added" in text
    assert "# TYPE blocks_added_total counter" in text
    assert "blocks_added_total 1.0" in text


def test_to_prometheus_text_renders_gauges():
    m = MetricsRegistry()
    m.set_gauge("compression_ratio", 3.5, labels={"block_index": "1"}, help_text="Ratio")
    text = m.to_prometheus_text()

    assert "# HELP compression_ratio Ratio" in text
    assert "# TYPE compression_ratio gauge" in text
    assert 'compression_ratio{block_index="1"} 3.5' in text


def test_to_prometheus_text_histogram_has_le_buckets_and_inf():
    m = MetricsRegistry()
    m.observe_histogram("latency_seconds", 0.5, buckets=(1.0,))
    text = m.to_prometheus_text()

    assert 'latency_seconds_bucket{le="1.0"} 1' in text
    assert 'latency_seconds_bucket{le="+Inf"} 1' in text
    assert "latency_seconds_sum 0.5" in text
    assert "latency_seconds_count 1" in text


def test_to_prometheus_text_dash_containing_values_go_in_labels_not_names():
    m = MetricsRegistry()
    m.increment_counter("dataset_bytes_total", labels={"dataset": "synthetic-blocks"})
    text = m.to_prometheus_text()

    assert 'dataset_bytes_total{dataset="synthetic-blocks"}' in text


def test_label_value_escaping_order():
    # Backslash first, then quote, then newline - escaping the quote before the
    # backslash would double-escape the backslash just introduced.
    raw = 'a"b\\c\nd'
    escaped = _escape_label_value(raw)
    assert escaped == 'a\\"b\\\\c\\nd'


def test_get_metrics_registry_returns_same_instance():
    assert get_metrics_registry() is get_metrics_registry()


@pytest.mark.parametrize(
    "bad_label_key",
    ["bad-key!", "1starts_with_digit", "has space", "has\nnewline", "", "a:b"],
)
def test_invalid_label_keys_are_rejected(bad_label_key):
    m = MetricsRegistry()
    with pytest.raises(ValueError, match="label name"):
        m.increment_counter("events_total", labels={bad_label_key: "v"})
    with pytest.raises(ValueError, match="label name"):
        m.set_gauge("gauge_value", 1.0, labels={bad_label_key: "v"})
    with pytest.raises(ValueError, match="label name"):
        m.observe_histogram("latency_seconds", 0.1, labels={bad_label_key: "v"})


def test_reserved_le_label_is_rejected_on_histograms():
    m = MetricsRegistry()
    with pytest.raises(ValueError, match="reserved"):
        m.observe_histogram("latency_seconds", 0.1, labels={"le": "0.5"})
    # Allowed on non-histogram metrics.
    m.set_gauge("custom_gauge", 1.0, labels={"le": "custom"})
    assert m.get_gauge("custom_gauge", labels={"le": "custom"}) == 1.0


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf"), True, "1.0"])
def test_non_finite_or_non_numeric_metric_values_are_rejected(bad_value):
    m = MetricsRegistry()
    with pytest.raises(ValueError, match="finite"):
        m.increment_counter("events_total", value=bad_value)
    with pytest.raises(ValueError, match="finite"):
        m.set_gauge("gauge_value", bad_value)
    with pytest.raises(ValueError, match="finite"):
        m.observe_histogram("latency_seconds", bad_value)
    assert m.to_prometheus_text() == ""


def test_to_json_rejects_nan_if_injected():
    m = MetricsRegistry()
    m._gauges[("corrupted_gauge", ())] = float("nan")
    with pytest.raises(ValueError):
        m.to_json()


def test_metrics_registry_thread_safety_under_concurrent_updates_and_exports():
    registry = MetricsRegistry()
    num_writers = 8
    iterations = 500
    stop_event = threading.Event()
    errors: list[BaseException] = []
    start_barrier = threading.Barrier(num_writers)

    def _writer(worker_id: int) -> None:
        try:
            start_barrier.wait(timeout=5)
            for i in range(iterations):
                registry.increment_counter("concurrent_ops_total")
                registry.set_gauge("concurrent_latest_worker", float(worker_id))
                registry.observe_histogram(
                    "concurrent_latency_seconds",
                    0.001 + (i % 5) * 0.001,
                    buckets=(0.002, 0.01),
                )
        except BaseException as exc:  # pragma: no cover - surfaced by assertion
            errors.append(exc)

    def _exporter() -> None:
        try:
            while not stop_event.is_set():
                payload = json.loads(registry.to_json())
                text = registry.to_prometheus_text()
                snap = registry.get_histogram("concurrent_latency_seconds")
                if snap is not None:
                    assert snap.buckets[float("inf")] == snap.count
                assert isinstance(payload, dict)
                assert isinstance(text, str)
        except BaseException as exc:  # pragma: no cover - surfaced by assertion
            errors.append(exc)

    exporters = [threading.Thread(target=_exporter) for _ in range(2)]
    writers = [threading.Thread(target=_writer, args=(idx,)) for idx in range(num_writers)]
    for t in exporters + writers:
        t.start()
    for t in writers:
        t.join(timeout=15)
    stop_event.set()
    for t in exporters:
        t.join(timeout=5)

    assert not any(t.is_alive() for t in exporters + writers)
    assert errors == []

    expected_total = num_writers * iterations
    assert registry.get_counter("concurrent_ops_total") == float(expected_total)
    gauge_val = registry.get_gauge("concurrent_latest_worker")
    assert gauge_val is not None and 0.0 <= gauge_val < float(num_writers)
    hist_snap = registry.get_histogram("concurrent_latency_seconds")
    assert hist_snap is not None
    assert hist_snap.count == expected_total
    assert hist_snap.buckets[float("inf")] == expected_total


def test_get_metrics_registry_thread_safe():
    barrier = threading.Barrier(6)
    seen: list[int] = []
    lock = threading.Lock()

    def _grab():
        barrier.wait(timeout=5)
        reg = get_metrics_registry()
        with lock:
            seen.append(id(reg))

    threads = [threading.Thread(target=_grab) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert len(set(seen)) == 1

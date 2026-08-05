# test_metrics.py

import json

import pytest

from blockchain_compression.observability import MetricsRegistry, get_metrics_registry
from blockchain_compression.observability.metrics import _escape_label_value


def test_increment_counter_accumulates():
    m = MetricsRegistry()
    m.increment_counter("events_total")
    m.increment_counter("events_total", value=2.5)
    assert m._counters[("events_total", ())] == 3.5


def test_increment_counter_default_value_is_one():
    m = MetricsRegistry()
    m.increment_counter("events_total")
    assert m._counters[("events_total", ())] == 1.0


def test_increment_counter_rejects_negative():
    m = MetricsRegistry()
    with pytest.raises(ValueError):
        m.increment_counter("events_total", value=-1)


def test_set_gauge_overwrites():
    m = MetricsRegistry()
    m.set_gauge("temperature", 10.0)
    m.set_gauge("temperature", 20.0)
    assert m._gauges[("temperature", ())] == 20.0


def test_labels_distinguish_separate_series():
    m = MetricsRegistry()
    m.set_gauge("compression_ratio", 3.0, labels={"block_index": "1"})
    m.set_gauge("compression_ratio", 4.0, labels={"block_index": "2"})
    assert m._gauges[("compression_ratio", (("block_index", "1"),))] == 3.0
    assert m._gauges[("compression_ratio", (("block_index", "2"),))] == 4.0


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

    counts = m._histogram_counts[("latency_seconds", ())]
    assert counts[0.001] == 1  # only the 0.0003 observation
    assert counts[0.01] == 1  # still just 0.0003
    assert counts[0.1] == 2  # both observations
    assert counts[float("inf")] == 2  # +Inf always equals the total


def test_observe_histogram_sum_and_count():
    m = MetricsRegistry()
    m.observe_histogram("latency_seconds", 0.1)
    m.observe_histogram("latency_seconds", 0.2)

    key = ("latency_seconds", ())
    assert m._histogram_totals[key] == 2
    assert m._histogram_sums[key] == pytest.approx(0.3)


def test_histogram_buckets_fixed_after_first_observation():
    m = MetricsRegistry()
    m.observe_histogram("latency_seconds", 1.0, buckets=(1.0, 2.0))
    m.observe_histogram("latency_seconds", 1.0, buckets=(100.0, 200.0))  # ignored - already set

    assert 1.0 in m._histogram_buckets["latency_seconds"]
    assert 100.0 not in m._histogram_buckets["latency_seconds"]


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

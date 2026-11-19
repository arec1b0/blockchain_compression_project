"""Structured metrics: counters, gauges, and histograms, exportable as JSON or
Prometheus text exposition format.

Explicit dependency injection, not an implicit global. Unlike ``logging.getLogger``
(a cheap no-op when unconfigured, with global state limited to configuration), a
registry *accumulates* data for the life of the process whether or not anyone
reads it - an implicit global would hand a host application embedding this
library unrequested shared mutable state (name collisions, cross-instance
sharing). So every call site here takes ``metrics: MetricsRegistry | None`` and
treats ``None`` as a true no-op; :func:`get_metrics_registry` is only meant for
the outermost layer (``main.py``, benchmark scripts) - the same layer that
already calls ``logging.basicConfig()``.
"""

import json
import math
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

#: Seconds. Spans sub-millisecond to multi-second, appropriate for the hashing
#: and modular-exponentiation latencies this project measures.
DEFAULT_HISTOGRAM_BUCKETS = (0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0)

#: Dimensionless raw / stored block body ratios.
COMPRESSION_RATIO_BUCKETS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 5.0, 10.0, 25.0)


def _validate_metric_name(name) -> None:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"metric name {name!r} must match {_NAME_RE.pattern!r} "
            "(Prometheus exposition format naming rules)"
        )


def _validate_metric_value(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"metric value must be a finite number, got {type(value).__name__}")
    if not math.isfinite(value):
        raise ValueError(f"metric value must be finite, got {value!r}")
    return float(value)


def _label_key(labels: dict | None, *, kind: str | None = None) -> tuple:
    """Validate and canonicalize a labels dict into a sorted, hashable tuple of string pairs."""
    if labels is None:
        return ()
    if not isinstance(labels, dict):
        raise TypeError(f"labels must be a dict, got {type(labels).__name__}")
    pairs = []
    for k, v in labels.items():
        if not isinstance(k, str) or not _LABEL_NAME_RE.fullmatch(k):
            raise ValueError(
                f"label name {k!r} must match {_LABEL_NAME_RE.pattern!r} "
                "(Prometheus label naming rules)"
            )
        if kind == "histogram" and k == "le":
            raise ValueError("label name 'le' is reserved for histogram bucket bounds")
        pairs.append((k, str(v)))
    return tuple(sorted(pairs))


def _escape_label_value(value: str) -> str:
    """Prometheus label-value escaping. Order matters: escaping the quote
    before the backslash would double-escape the backslash just introduced."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_labels(label_key: tuple, extra: dict | None = None) -> str:
    pairs = list(label_key) + (sorted(extra.items()) if extra else [])
    if not pairs:
        return ""
    parts = [f'{k}="{_escape_label_value(str(v))}"' for k, v in sorted(pairs)]
    return "{" + ",".join(parts) + "}"


def _bound_label(bound: float) -> str:
    return "+Inf" if bound == float("inf") else str(bound)


@dataclass(frozen=True)
class HistogramSnapshot:
    """Immutable point-in-time snapshot of one histogram label series."""

    count: int
    sum: float
    buckets: Mapping[float, int]


class MetricsRegistry:
    """Thread-safe in-process metrics registry: counters, gauges, and histograms."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counters: dict[tuple, float] = {}
        self._gauges: dict[tuple, float] = {}
        self._histogram_buckets: dict[str, tuple] = {}
        self._histogram_counts: dict[tuple, dict] = {}
        self._histogram_sums: dict[tuple, float] = {}
        self._histogram_totals: dict[tuple, int] = {}
        self._help: dict[str, str] = {}
        self._kinds: dict[str, str] = {}

    def _register(self, name: str, kind: str, help_text: str | None) -> None:
        existing_kind = self._kinds.get(name)
        if existing_kind is not None and existing_kind != kind:
            raise ValueError(f"metric {name!r} already registered as {existing_kind}, not {kind}")
        self._kinds[name] = kind
        if help_text is not None:
            self._help[name] = help_text
        else:
            self._help.setdefault(name, "")

    def increment_counter(
        self,
        name: str,
        value: float = 1.0,
        labels: dict | None = None,
        help_text: str | None = None,
    ) -> None:
        _validate_metric_name(name)
        validated_value = _validate_metric_value(value)
        if validated_value < 0:
            raise ValueError("counter increments must be non-negative")
        key = (name, _label_key(labels, kind="counter"))
        with self._lock:
            self._register(name, "counter", help_text)
            self._counters[key] = self._counters.get(key, 0.0) + validated_value

    def set_gauge(
        self, name: str, value: float, labels: dict | None = None, help_text: str | None = None
    ) -> None:
        _validate_metric_name(name)
        validated_value = _validate_metric_value(value)
        key = (name, _label_key(labels, kind="gauge"))
        with self._lock:
            self._register(name, "gauge", help_text)
            self._gauges[key] = validated_value

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: dict | None = None,
        help_text: str | None = None,
        buckets=None,
    ) -> None:
        """Record one observation. ``buckets`` (ascending) only takes effect
        the first time ``name`` is observed - a histogram's bucket boundaries
        must stay fixed for its samples to mean anything."""
        _validate_metric_name(name)
        validated_value = _validate_metric_value(value)
        key = (name, _label_key(labels, kind="histogram"))
        chosen = tuple(buckets) if buckets else DEFAULT_HISTOGRAM_BUCKETS
        sorted_buckets = tuple(sorted({*chosen, float("inf")}))
        with self._lock:
            self._register(name, "histogram", help_text)
            if name not in self._histogram_buckets:
                self._histogram_buckets[name] = sorted_buckets

            counts = self._histogram_counts.setdefault(key, {})
            for bound in self._histogram_buckets[name]:
                if validated_value <= bound:
                    counts[bound] = counts.get(bound, 0) + 1
            self._histogram_sums[key] = self._histogram_sums.get(key, 0.0) + validated_value
            self._histogram_totals[key] = self._histogram_totals.get(key, 0) + 1

    def get_counter(self, name: str, labels: dict | None = None) -> float | None:
        """Return the recorded counter value for ``(name, labels)``, or ``None``."""
        _validate_metric_name(name)
        key = (name, _label_key(labels, kind="counter"))
        with self._lock:
            return self._counters.get(key)

    def get_gauge(self, name: str, labels: dict | None = None) -> float | None:
        """Return the recorded gauge value for ``(name, labels)``, or ``None``."""
        _validate_metric_name(name)
        key = (name, _label_key(labels, kind="gauge"))
        with self._lock:
            return self._gauges.get(key)

    def get_histogram(self, name: str, labels: dict | None = None) -> HistogramSnapshot | None:
        """Return a read-only :class:`HistogramSnapshot` for ``(name, labels)``,
        or ``None`` if no observations have been recorded for that series."""
        _validate_metric_name(name)
        key = (name, _label_key(labels, kind="histogram"))
        with self._lock:
            if key not in self._histogram_totals:
                return None
            counts = self._histogram_counts[key]
            bucket_snapshot = MappingProxyType(
                {bound: counts.get(bound, 0) for bound in self._histogram_buckets[name]}
            )
            return HistogramSnapshot(
                count=self._histogram_totals[key],
                sum=self._histogram_sums[key],
                buckets=bucket_snapshot,
            )

    def to_json(self, indent: int | None = 2) -> str:
        """A structured snapshot of every metric family, as one JSON document."""
        with self._lock:
            counters = [
                {"name": name, "labels": dict(label_key), "value": value}
                for (name, label_key), value in sorted(self._counters.items())
            ]
            gauges = [
                {"name": name, "labels": dict(label_key), "value": value}
                for (name, label_key), value in sorted(self._gauges.items())
            ]
            histograms = []
            for name, label_key in sorted(self._histogram_counts):
                key = (name, label_key)
                counts = self._histogram_counts[key]
                histograms.append(
                    {
                        "name": name,
                        "labels": dict(label_key),
                        "count": self._histogram_totals.get(key, 0),
                        "sum": self._histogram_sums.get(key, 0.0),
                        "buckets": {
                            _bound_label(bound): counts.get(bound, 0)
                            for bound in self._histogram_buckets[name]
                        },
                    }
                )
        payload = {"counters": counters, "gauges": gauges, "histograms": histograms}
        return json.dumps(payload, sort_keys=True, indent=indent, allow_nan=False)

    def to_prometheus_text(self) -> str:
        """Hand-rolled Prometheus text exposition format - HELP/TYPE lines
        grouped per metric family, no dependency on ``prometheus_client``."""
        with self._lock:
            kinds = dict(self._kinds)
            helps = dict(self._help)
            counters_items = sorted(self._counters.items())
            gauges_items = sorted(self._gauges.items())
            histogram_buckets = dict(self._histogram_buckets)
            histogram_counts = {k: dict(v) for k, v in self._histogram_counts.items()}
            histogram_sums = dict(self._histogram_sums)
            histogram_totals = dict(self._histogram_totals)

        lines = []
        for name in sorted(kinds):
            kind = kinds[name]
            lines.append(f"# HELP {name} {helps.get(name, '')}".rstrip())
            lines.append(f"# TYPE {name} {kind}")

            if kind == "counter":
                for (metric_name, label_key), value in counters_items:
                    if metric_name == name:
                        lines.append(f"{name}{_format_labels(label_key)} {value}")
            elif kind == "gauge":
                for (metric_name, label_key), value in gauges_items:
                    if metric_name == name:
                        lines.append(f"{name}{_format_labels(label_key)} {value}")
            elif kind == "histogram":
                buckets = histogram_buckets[name]
                series_keys = sorted(k for k in histogram_counts if k[0] == name)
                for metric_name, label_key in series_keys:
                    key = (metric_name, label_key)
                    counts = histogram_counts[key]
                    for bound in buckets:
                        labels = _format_labels(label_key, extra={"le": _bound_label(bound)})
                        lines.append(f"{name}_bucket{labels} {counts.get(bound, 0)}")
                    plain_labels = _format_labels(label_key)
                    lines.append(f"{name}_sum{plain_labels} {histogram_sums.get(key, 0.0)}")
                    lines.append(f"{name}_count{plain_labels} {histogram_totals.get(key, 0)}")
        return "\n".join(lines) + ("\n" if lines else "")


_default_registry: MetricsRegistry | None = None
_default_registry_lock = threading.Lock()


def get_metrics_registry() -> MetricsRegistry:
    """A process-wide convenience registry for the outermost composition layer
    (``main.py``, benchmark scripts). Library code should not call this on a
    caller's behalf - see the module docstring."""
    global _default_registry
    if _default_registry is None:
        with _default_registry_lock:
            if _default_registry is None:
                _default_registry = MetricsRegistry()
    return _default_registry

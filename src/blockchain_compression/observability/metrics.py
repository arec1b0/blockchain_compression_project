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
import re

_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")

#: Seconds. Spans sub-millisecond to multi-second, appropriate for the hashing
#: and modular-exponentiation latencies this project measures.
DEFAULT_HISTOGRAM_BUCKETS = (0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0)


def _validate_metric_name(name) -> None:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(
            f"metric name {name!r} must match {_NAME_RE.pattern!r} "
            "(Prometheus exposition format naming rules)"
        )


def _label_key(labels: dict | None) -> tuple:
    """Canonicalize a labels dict into a sorted, hashable tuple of string pairs."""
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


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


class MetricsRegistry:
    """An in-process metrics registry: counters, gauges, and histograms."""

    def __init__(self):
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
        if value < 0:
            raise ValueError("counter increments must be non-negative")
        self._register(name, "counter", help_text)
        key = (name, _label_key(labels))
        self._counters[key] = self._counters.get(key, 0.0) + value

    def set_gauge(
        self, name: str, value: float, labels: dict | None = None, help_text: str | None = None
    ) -> None:
        _validate_metric_name(name)
        self._register(name, "gauge", help_text)
        key = (name, _label_key(labels))
        self._gauges[key] = value

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: dict | None = None,
        help_text: str | None = None,
        buckets=None,
    ) -> None:
        """Record one observation. ``buckets`` (seconds, ascending) only takes
        effect the first time ``name`` is observed - a histogram's bucket
        boundaries must stay fixed for its samples to mean anything."""
        _validate_metric_name(name)
        self._register(name, "histogram", help_text)
        if name not in self._histogram_buckets:
            chosen = tuple(buckets) if buckets else DEFAULT_HISTOGRAM_BUCKETS
            self._histogram_buckets[name] = tuple(sorted({*chosen, float("inf")}))

        key = (name, _label_key(labels))
        counts = self._histogram_counts.setdefault(key, {})
        for bound in self._histogram_buckets[name]:
            if value <= bound:
                counts[bound] = counts.get(bound, 0) + 1
        self._histogram_sums[key] = self._histogram_sums.get(key, 0.0) + value
        self._histogram_totals[key] = self._histogram_totals.get(key, 0) + 1

    def to_json(self, indent: int | None = 2) -> str:
        """A structured snapshot of every metric family, as one JSON document."""
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
        return json.dumps(payload, sort_keys=True, indent=indent)

    def to_prometheus_text(self) -> str:
        """Hand-rolled Prometheus text exposition format - HELP/TYPE lines
        grouped per metric family, no dependency on ``prometheus_client``."""
        lines = []
        for name in sorted(self._kinds):
            kind = self._kinds[name]
            lines.append(f"# HELP {name} {self._help.get(name, '')}".rstrip())
            lines.append(f"# TYPE {name} {kind}")

            if kind == "counter":
                for (metric_name, label_key), value in sorted(self._counters.items()):
                    if metric_name == name:
                        lines.append(f"{name}{_format_labels(label_key)} {value}")
            elif kind == "gauge":
                for (metric_name, label_key), value in sorted(self._gauges.items()):
                    if metric_name == name:
                        lines.append(f"{name}{_format_labels(label_key)} {value}")
            elif kind == "histogram":
                buckets = self._histogram_buckets[name]
                series_keys = sorted(k for k in self._histogram_counts if k[0] == name)
                for metric_name, label_key in series_keys:
                    key = (metric_name, label_key)
                    counts = self._histogram_counts[key]
                    for bound in buckets:
                        labels = _format_labels(label_key, extra={"le": _bound_label(bound)})
                        lines.append(f"{name}_bucket{labels} {counts.get(bound, 0)}")
                    plain_labels = _format_labels(label_key)
                    lines.append(f"{name}_sum{plain_labels} {self._histogram_sums.get(key, 0.0)}")
                    lines.append(f"{name}_count{plain_labels} {self._histogram_totals.get(key, 0)}")
        return "\n".join(lines) + ("\n" if lines else "")


_default_registry: MetricsRegistry | None = None


def get_metrics_registry() -> MetricsRegistry:
    """A process-wide convenience registry for the outermost composition layer
    (``main.py``, benchmark scripts). Library code should not call this on a
    caller's behalf - see the module docstring."""
    global _default_registry
    if _default_registry is None:
        _default_registry = MetricsRegistry()
    return _default_registry

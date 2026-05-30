"""Observer/Researcher agent for the Dark Factory for Helix.

This package is Agent-C2 in the Dark Factory pipeline. It:

1. OBSERVES live Helix telemetry in Datadog (``helix.produce.latency_ms.*``).
2. RESEARCHES the cause by forming competing hypotheses grounded in the real
   Helix source, then runs an experiment to confirm one and rule out the rest.
3. DRIVES an ``ImprovementIssue`` through Temper (Observe -> BeginPlanning ->
   WritePlan), then hands off to Symphony (Agent-C1) after a human approves the
   plan via Cedar governance.

The package is intentionally split so the OBSERVE + RESEARCH phases can run with
zero Temper dependency (per the BC contract), and the DRIVE phase degrades
gracefully when Agent-B's ``ImprovementIssue`` spec is not yet deployed.
"""

from .observe import (
    LatencyObservation,
    MetricSource,
    SnapshotMetricSource,
    DatadogMetricSource,
    observe_produce_latency,
)
from .research import (
    Hypothesis,
    ResearchResult,
    SourceProbe,
    research_latency,
)
from .temper_client import TemperClient, TemperDenied, TemperUnavailable

__all__ = [
    "LatencyObservation",
    "MetricSource",
    "SnapshotMetricSource",
    "DatadogMetricSource",
    "observe_produce_latency",
    "Hypothesis",
    "ResearchResult",
    "SourceProbe",
    "research_latency",
    "TemperClient",
    "TemperDenied",
    "TemperUnavailable",
]

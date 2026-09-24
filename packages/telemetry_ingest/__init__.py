"""Telemetry and event ingest inside the host services (docs/satinav-fleet-agent-phase0-v2.md §5.2).
A library, not a service: hosts put rows on an IngestQueue and run one TelemetryWriter."""

from packages.telemetry_ingest.metrics import Metrics
from packages.telemetry_ingest.policy import (
    DEFAULT_LEVEL, PolicySources, RecordingLevel, RecordingPolicy, load_sources, resolve,
)
from packages.telemetry_ingest.queue import IngestQueue, SpillFile
from packages.telemetry_ingest.rehydrate import LatestRow, load_latest
from packages.telemetry_ingest.writer import TelemetryWriter, create_pool

__all__ = [
    "Metrics",
    "DEFAULT_LEVEL", "PolicySources", "RecordingLevel", "RecordingPolicy", "load_sources", "resolve",
    "IngestQueue", "SpillFile",
    "LatestRow", "load_latest",
    "TelemetryWriter", "create_pool",
]

"""Fleet event codes, payload models, detectors, cause mapping and emit()
(docs/satinav-fleet-agent-phase0-v2.md §5.1). A library, not a service."""

from packages.events.codes import CODES, CodeMeta, EventCode, Severity, Source, meta_for
from packages.events.emit import Event, EventContext, build_row, emit, row_params
from packages.events.ids import event_id, normalize_ts
from packages.events.schemas import InvalidPayloadError, set_strict_validation

__all__ = [
    "CODES", "CodeMeta", "EventCode", "Severity", "Source", "meta_for",
    "Event", "EventContext", "build_row", "emit", "row_params",
    "event_id", "normalize_ts",
    "InvalidPayloadError", "set_strict_validation",
]

"""packages/utils/capture_time.py and its use by graph-builder's _process_topology."""
import datetime
import threading
from unittest.mock import MagicMock

import pytest

from packages.services.graph_builder.server import GraphBuilderService
from packages.utils import capture_time as ct

pytestmark = pytest.mark.unit

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC)
WHEN = datetime.datetime(2026, 10, 11, 11, 59, 0, tzinfo=UTC)
ISO = WHEN.isoformat()
SECS = WHEN.timestamp()


class TestParse:
    @pytest.mark.parametrize("value", [
        SECS, int(SECS * 1000), int(SECS * 1e6), int(SECS * 1e9),
        ISO, "2026-10-11T11:59:00Z", "2026-10-11T13:59:00+02:00",
    ])
    def test_accepted_formats(self, value):
        assert ct.parse(value, NOW) == WHEN

    @pytest.mark.parametrize("value", [
        None, True, "", "yesterday", "2026-10-11T11:59:00",   # naive: no offset
        float("nan"), float("inf"), [], {}, 12.5, 1_700_000,  # small = sim time / garbage
        "2019-12-31T23:59:59+00:00", 1.0e30,
        "2026-10-11T12:10:00+00:00",                          # 10 min in the future
    ])
    def test_insane_values_are_dropped(self, value):
        assert ct.parse(value, NOW) is None

    def test_a_few_seconds_ahead_is_tolerated(self):
        assert ct.parse("2026-10-11T12:00:30+00:00", NOW) is not None


class TestExtract:
    def test_camera_metadata_ms_earliest_sane_wins(self):
        payload = {"camera_metadata": [{"timestamp": int(SECS * 1000) + 500},
                                       {"timestamp": 3}, {"camera_name": "x"},
                                       {"timestamp": int(SECS * 1000)}]}
        assert ct.extract(payload, NOW) == ISO

    def test_top_level_before_camera_metadata(self):
        payload = {"timestamp": "2026-10-11T11:58:00+00:00",
                   "camera_metadata": [{"timestamp": int(SECS * 1000)}]}
        assert ct.extract(payload, NOW) == "2026-10-11T11:58:00+00:00"

    def test_explicit_capture_time_first_and_output_is_utc(self):
        payload = {"capture_time": "2026-10-11T13:59:00+02:00", "stamp": SECS - 100}
        assert ct.extract(payload, NOW) == ISO

    def test_insane_top_level_falls_through_to_next_source(self):
        payload = {"timestamp": "nope", "camera_metadata": [{"timestamp": int(SECS * 1000)}]}
        assert ct.extract(payload, NOW) == ISO

    def test_metadata_timestamp_is_ignored(self):
        assert ct.extract({"metadata": {"timestamp": "2026-10-11T11:59:00"}}, NOW) is None

    def test_nothing_usable(self):
        assert ct.extract({}, NOW) is None
        assert ct.extract({"camera_metadata": "x"}, NOW) is None
        assert ct.extract({"camera_metadata": [{"timestamp": 5}]}, NOW) is None


class TestIngest:
    def _run(self, payload):
        svc = GraphBuilderService.__new__(GraphBuilderService)
        svc.logger = MagicMock()
        svc.stats = {"errors": 0, "session_mappings": 0}
        svc._upload_lock = threading.RLock()
        svc.session_to_global_map = {}
        svc._stored_nodes = {}
        svc.image_buffer = MagicMock()
        svc.image_buffer.take.return_value = []
        svc.image_buffer_timeout = 1
        svc._detect_and_clear_session_reset = lambda *a, **k: None
        svc._generate_global_node_id = lambda: "g1"
        svc._find_nearby_nodes = lambda *a, **k: []
        svc._create_edges = lambda *a, **k: []
        svc.graph_db = MagicMock()
        svc.graph_db.add_node.return_value = False   # stop right after add_node
        svc._process_topology(payload, map_id="m", session_id="s")
        return svc.graph_db.add_node.call_args.kwargs["metadata"]

    def test_capture_time_is_stored_next_to_the_unchanged_receipt_fields(self):
        now_ms = int(datetime.datetime.now(UTC).timestamp() * 1000)
        meta = self._run({"robot_name": "r", "session_node_id": 1, "x": 0, "y": 0,
                          "camera_metadata": [{"camera_name": "f", "timestamp": now_ms}],
                          "metadata": {"timestamp": "robot-said-this"}})
        assert meta["capture_time"].endswith("+00:00")
        # the receipt time is still the cloud's naive datetime.now().isoformat()
        parsed = datetime.datetime.fromisoformat(meta["timestamp"])
        assert parsed.tzinfo is None and meta["timestamp"] != "robot-said-this"
        assert meta["map_id"] == "m" and meta["session_id"] == "s"

    def test_no_or_insane_capture_time_leaves_the_field_absent(self):
        for extra in ({}, {"camera_metadata": [{"timestamp": 12}]},
                      {"metadata": {"capture_time": "2026-01-01T00:00:00+00:00"}}):
            meta = self._run({"robot_name": "r", "session_node_id": 1, "x": 0, "y": 0, **extra})
            assert "capture_time" not in meta and "timestamp" in meta

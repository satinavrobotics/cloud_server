"""Explicit-session ingest (import, packages/services/graph_builder/ingest.py "Import"): a bundle
with `import_session_id` goes to that session, finished or not, checked for its owner and frame;
its acks are keyed by the target session and never touch `ingest_state`; nothing live happens."""
import os
from unittest.mock import AsyncMock, Mock

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import pytest  # noqa: E402

from packages.services.graph_builder import acks, ingest  # noqa: E402
from tests.unit.test_graph_builder_ack import KEEP, last, svc  # noqa: E402
from tests.unit.test_maps_m2 import NODE, _row  # noqa: E402

pytestmark = pytest.mark.unit

TARGET = "00000000-0000-0000-0000-0000000000a1"


def target_row(owner="r1", **kw):
    return _row(sid=TARGET, **kw) + (None, None, "mapping", True, owner)


def import_svc(target=KEEP, open_row=None):
    """A graph-builder whose open session is `open_row` (None: no open session) and whose named
    session is `target`."""
    s = svc(open_row)
    row = target_row() if target is KEEP else target

    async def by_id(sid):
        return row if sid == TARGET else None

    async def fetch(_robot):
        return open_row
    s.sessions = ingest.SessionResolver(fetch, ttl=0, fetch_by_id=by_id)
    s._publish_node_update = AsyncMock()
    return s


IMPORT = {**NODE, "import_session_id": TARGET, "run_id": "run1"}


class TestDecideImport:
    def session(self, **kw):
        return ingest.OpenSession.from_row(target_row(**kw))

    def test_finished_session_of_the_robot_is_accepted(self):
        assert ingest.decide_import("r1", self.session(), TARGET).accepted

    def test_unknown_or_foreign_session(self):
        assert ingest.decide_import("r1", None).reason == ingest.SESSION_UNKNOWN
        assert ingest.decide_import("r2", self.session()).reason == ingest.SESSION_UNKNOWN

    def test_another_frame_is_refused(self):
        r = ingest.decide_import("r1", self.session(), "someone-else")
        assert r.reason == ingest.SOURCE_MISMATCH

    def test_open_session_of_the_robot_blocks(self):
        open_ = ingest.OpenSession.from_row(_row())
        assert ingest.decide_import("r1", self.session(), None, open_).reason == ingest.SESSION_BUSY

    def test_missing_and_deleting_map(self):
        assert ingest.decide_import("r1", self.session(lifecycle=None)).reason == ingest.MAP_MISSING
        assert (ingest.decide_import("r1", self.session(lifecycle="DELETING")).reason
                == ingest.MAP_DELETING)

    def test_reasons_are_classified_for_the_robot(self):
        assert ingest.SESSION_UNKNOWN in acks.TERMINAL_REASONS
        assert ingest.SOURCE_MISMATCH in acks.TERMINAL_REASONS
        assert ingest.SESSION_BUSY in acks.RETRYABLE_REASONS


class TestImportIngest:
    async def test_node_is_stored_in_the_target_and_acked_apart(self):
        s = import_svc()
        await s._handle_node_update(dict(IMPORT))
        b = last(s)
        assert b["status"] == "accepted" and b["session_id"] == TARGET and b["import"] is True
        assert s.mqtt_client.states() == []  # the live high-water mark is untouched
        s._publish_node_update.assert_not_called()

    async def test_other_robots_session_is_unknown_and_terminal(self):
        s = import_svc(target=target_row(owner="r2"))
        await s._handle_node_update(dict(IMPORT))
        b = last(s)
        assert (b["status"], b["reason"], b["resend"]) == ("rejected", "session_unknown", "none")

    async def test_frame_mismatch_is_refused(self):
        s = import_svc()
        await s._handle_node_update({**IMPORT, "source_session_id": "other"})
        assert last(s)["reason"] == "source_mismatch"

    async def test_open_session_makes_the_replay_wait(self):
        s = import_svc(open_row=_row())
        await s._handle_node_update(dict(IMPORT))
        b = last(s)
        assert (b["reason"], b["resend"]) == ("session_busy", "node")

    async def test_live_node_is_unchanged(self):
        s = import_svc(open_row=_row())
        await s._handle_node_update({**NODE, "run_id": "run1"})
        b = last(s)
        assert b["status"] == "accepted" and b["session_id"] == "s1" and "import" not in b


class TestImportHardening:
    async def test_malformed_session_id_is_terminal_not_retried(self):
        s = import_svc()
        await s._handle_node_update({**IMPORT, "import_session_id": "nope"})
        b = last(s)
        assert (b["reason"], b["resend"]) == ("session_unknown", "none")

    async def test_session_id_spelling_does_not_split_trackers(self):
        s = import_svc()
        await s._handle_node_update({**IMPORT, "import_session_id": TARGET.upper()})
        b = last(s)
        assert b["status"] == "accepted" and b["import_session_id"] == TARGET

    async def test_import_needs_a_run_id(self):
        s = import_svc()
        await s._handle_node_update({**NODE, "import_session_id": TARGET})
        assert last(s)["reason"] == "invalid_payload"

    async def test_rejected_import_leaves_live_buffers_and_counters(self):
        s = import_svc(open_row=_row())
        s.image_buffer.pop = Mock(return_value=3)
        await s._handle_node_update(dict(IMPORT))  # session_busy
        s.image_buffer.pop.assert_not_called()
        assert s.stats["nodes_rejected"] == 0 and s.stats["import_rejected"] == 1

    async def test_open_session_opened_after_a_cached_lookup_still_blocks(self):
        s = import_svc()
        s.sessions.ttl = 60.0
        await s.sessions.resolve_import("r1", TARGET)  # accepted; no open session yet

        async def fetch_open(_robot):
            return _row()
        s.sessions._fetch = fetch_open  # a session opened within the cached second
        r = await s.sessions.resolve_import("r1", TARGET)
        assert r.reason == ingest.SESSION_BUSY

    async def test_target_is_cached_across_a_replay(self):
        s = import_svc()
        calls = []
        inner = s.sessions._fetch_by_id

        async def counting(sid):
            calls.append(sid)
            return await inner(sid)
        s.sessions._fetch_by_id = counting
        s.sessions.ttl = 1.0
        for _ in range(3):
            await s.sessions.resolve_import("r1", TARGET)
        assert len(calls) == 1


class TestAckNamespace:
    def test_live_and_import_trackers_are_apart_and_import_never_sets_state(self):
        sent = []
        a = acks.AckManager(lambda *x: sent.append(x) or 0, {}, clock=lambda: 0.0)
        parts = [("image", "left")]
        a.begin("r1", "R", 5, parts, "live")
        a.begin("r1", "R", 5, parts, "tgt", ns="tgt")
        a.node_ok("r1", "R", 5, "accepted", "tgt", parts, ns="tgt")
        a.part_stored("r1", "R", 5, "image", "left", ns="tgt")
        bodies = [__import__("json").loads(m[1]) for m in sent]
        assert bodies[-1]["complete"] is True and bodies[-1]["import"] is True
        assert all("ingest_state" not in m[0] for m in sent)
        assert a.backlog("r1", "R")["open_trackers"] == 1  # the live one only

    def test_import_timeout_ack_is_flagged(self):
        t = [0.0]
        sent = []
        a = acks.AckManager(lambda *x: sent.append(x) or 0, {}, part_timeout_s=10,
                            clock=lambda: t[0])
        parts = [("image", "left")]
        a.begin("r1", "R", 5, parts, "tgt", ns="tgt")
        a.node_ok("r1", "R", 5, "accepted", "tgt", parts, ns="tgt")
        t[0] = 11.0
        a.flush()
        body = __import__("json").loads(sent[-1][1])
        assert body["import"] is True and body["resend"] == "parts"

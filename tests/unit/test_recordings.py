"""Recordings registry ingest (packages/topomap_dbs/rosbag_db/recordings.py): tar-header walker,
intervals and the sidecar fallback, grouping, dedupe. No database or MinIO."""
import datetime
import io
import tarfile

import pytest

yaml = pytest.importorskip("yaml")
from packages.topomap_dbs.rosbag_db import recordings as rec  # noqa: E402

pytestmark = pytest.mark.unit
UTC = datetime.timezone.utc

META = ("rosbag2_bagfile_information:\n  version: 5\n  storage_identifier: mcap\n"
        "  duration:\n    nanoseconds: 90500000000\n  starting_time:\n"
        "    nanoseconds_since_epoch: 1791059347624795000\n  message_count: 12\n").encode()


def make_tar(members, fmt=tarfile.GNU_FORMAT):
    """members: [(name, bytes | None)] (None: a directory) -> tar bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=fmt) as tf:
        for name, data in members:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            else:
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class Reader:
    def __init__(self, blob):
        self.blob, self.calls = blob, []

    def __call__(self, offset, length):
        self.calls.append((offset, length))
        return self.blob[offset:offset + length]


def test_walker_finds_metadata_after_large_unaligned_members():
    big = bytes(range(256)) * 400 + b"x" * 13                 # 102413 bytes: not a multiple of 512
    blob = make_tar([("bag/", None), ("bag/bag_0.mcap", big), ("bag/other.bin", b"abc"),
                     ("bag/metadata.yaml", META), ("bag/tail.bin", b"z" * 700)])
    r = Reader(blob)
    assert rec.find_tar_member(r, len(blob)) == META
    assert sum(n for _, n in r.calls) < 3000                  # headers + the member, not the 100 kB
    assert max(n for _, n in r.calls) <= 512


def test_walker_handles_long_names_in_pax_and_gnu_formats():
    deep = "d" * 120
    for fmt in (tarfile.PAX_FORMAT, tarfile.GNU_FORMAT):
        blob = make_tar([(f"bag/{deep}.mcap", b"q" * 1000), ("bag/metadata.yaml", META)], fmt)
        assert rec.find_tar_member(Reader(blob), len(blob)) == META
    long_dir = "/".join(["a" * 60, "metadata.yaml"])            # one directory down, long path
    blob = make_tar([(long_dir, META)], tarfile.PAX_FORMAT)
    assert rec.find_tar_member(Reader(blob), len(blob)) == META


def test_walker_returns_none_without_metadata_or_on_a_truncated_tar():
    blob = make_tar([("bag/bag_0.mcap", b"a" * 5000)])
    assert rec.find_tar_member(Reader(blob), len(blob)) is None
    assert rec.find_tar_member(Reader(blob[:700]), 700) is None       # cut inside the member
    assert rec.find_tar_member(Reader(b""), 0) is None
    nested = make_tar([("bag/sub/deeper/metadata.yaml", META)])        # not a rosbag2 root file
    assert rec.find_tar_member(Reader(nested), len(nested)) is None


def test_base256_size_field():
    assert rec._tar_size(b"\x80" + b"\0" * 6 + (10 * 2 ** 30).to_bytes(5, "big")) == 10 * 2 ** 30
    assert rec._tar_size(b"00000001750\0") == 1000


def test_metadata_interval_is_utc():
    meta = rec.parse_bag_metadata(META)
    start, end, how = rec.interval({"recorded_at": "2026-10-03T22:28:54"}, meta)
    assert how == "metadata"
    assert start == datetime.datetime(2026, 10, 3, 20, 29, 7, 624795, tzinfo=UTC)
    assert end - start == datetime.timedelta(seconds=90, milliseconds=500)
    with pytest.raises(ValueError):
        rec.parse_bag_metadata(b"rosbag2_bagfile_information: {}")


def test_a_start_stamped_from_boot_keeps_only_the_end():
    # Seen on two July 2026 bags: starting_time 81 s after the epoch, end in 2026.
    boot = {"start_ns": 81_031_301_000, "duration_ns": 1_783_437_543_000_000_000 - 81_031_301_000}
    start, end, how = rec.interval({"recorded_at": "2026-07-08T15:19:00"}, boot)
    assert how == "metadata_end_only" and start == end
    assert end.year == 2026


def test_sidecar_fallback_is_budapest_local_time_and_zero_length():
    start, end, how = rec.interval({"recorded_at": "2026-10-03T22:28:54"}, None)   # CEST, UTC+2
    assert (how, start, end) == ("sidecar_start_only", datetime.datetime(2026, 10, 3, 20, 28, 54, tzinfo=UTC),
                                 datetime.datetime(2026, 10, 3, 20, 28, 54, tzinfo=UTC))
    winter, _, _ = rec.interval({"recorded_at": "2026-12-01T10:00:00"}, None)      # CET, UTC+1
    assert winter == datetime.datetime(2026, 12, 1, 9, 0, tzinfo=UTC)
    assert rec.interval({"recorded_at": None}, None) == (None, None, "none")
    assert rec.interval({"recorded_at": "garbage"}, None) == (None, None, "none")


def test_classify_groups_nav_and_raw_of_a_run():
    nav = {"bag_id": "a", "description": "20261018_190021_navstack_sati_odin"}
    raw = {"bag_id": "b", "description": "20261018_190021_navstack_sati_odin_raw"}
    note = {"bag_id": "c", "description": "20261013_121024_navstack_sati_odin_raw (no metadata.yaml: x)"}
    assert rec.classify(nav) == ("run", "20261018_190021_navstack_sati_odin", "nav")
    assert rec.classify(raw) == ("run", "20261018_190021_navstack_sati_odin", "raw")
    assert rec.classify(note) == ("run", "20261013_121024_navstack_sati_odin", "raw")
    assert rec.classify({"bag_id": "d", "description": "lidar record"}) == ("run", "d", "bag")
    assert rec.classify({"bag_id": "e", "description": None}) == ("run", "e", "bag")


class FakeSources:
    def __init__(self, sidecars, objects, blobs):
        self._sidecars, self.objects, self.blobs = sidecars, objects, blobs

    def sidecars(self):
        return iter(self._sidecars)

    def stat(self, robot, bag_id):
        return self.objects.get(f"{robot}/{bag_id}")

    def read_range(self, key):
        return Reader(self.blobs[key])


def obj(key, blob, minute):
    return {"key": key, "size": len(blob), "etag": '"e-' + key + '"',
            "last_modified": datetime.datetime(2026, 10, 21, 15, minute, tzinfo=UTC)}


def car(bag_id, desc, at="2026-10-18T19:00:21"):
    return {"bag_id": bag_id, "robot_name": "formidable-peacock", "map_id": "office9",
            "description": desc, "recorded_at": at}


def test_collect_dedupes_reuploads_and_uses_every_fallback():
    good = make_tar([("p/bag_0.mcap", b"a" * 1234), ("p/metadata.yaml", META)])
    nometa = make_tar([("p/bag_0.mcap", b"a" * 1234)])
    run = "20261018_190021_navstack_sati_odin"
    side = [car("u1", run),                      # object, no metadata.yaml (older upload)
            car("u2", run),                      # object with metadata (wins)
            car("u3", run),                      # sidecar only
            car("r1", run + "_raw"),             # raw part of the same run: its own row
            car("x1", None, at=None)]            # nothing known
    objs = {"formidable-peacock/u1": obj("formidable-peacock/u1", nometa, 1),
            "formidable-peacock/u2": obj("formidable-peacock/u2", good, 2),
            "formidable-peacock/r1": obj("formidable-peacock/r1", good, 3)}
    blobs = {k: (good if k.endswith(("u2", "r1")) else nometa) for k in objs}
    rows, counts = rec.collect(FakeSources(side, objs, blobs))
    by = {(r["group_key"], r["part"]): r for r in rows}
    assert len(rows) == 3 and counts["duplicates_collapsed"] == 2 and counts["sidecars"] == 5
    win = by[(run, "nav")]
    assert win["storage_key"] == "formidable-peacock/u2" and win["source"]["interval_source"] == "metadata"
    assert win["source"]["duplicates"] == ["u1", "u3"] and win["content_hash"] == "e-formidable-peacock/u2"
    assert by[(run, "raw")]["source"]["interval_source"] == "metadata"
    assert by[("x1", "bag")]["start"] is None and by[("x1", "bag")]["source"]["interval_source"] == "none"
    assert counts["interval_source"] == {"metadata": 2, "metadata_end_only": 0,
                                         "sidecar_start_only": 0, "none": 1}
    assert counts["bag_missing"] == 2 and counts["metadata_missing"] == 1


def test_collapse_is_order_independent_and_ids_are_stable():
    a = rec.build_row(car("a", "20261018_190021_navstack_sati_odin"), None, None)
    b = rec.build_row(car("b", "20261018_190021_navstack_sati_odin"), None, None)
    one, _ = rec.collapse([a, b])
    two, _ = rec.collapse([b, a])
    assert one[0]["source"]["sidecar"]["bag_id"] == two[0]["source"]["sidecar"]["bag_id"] == "b"
    assert a["id"] == b["id"]                                     # uuid5 of (robot, group_key, part)


def test_sidecar_only_row_uses_the_intended_key_and_no_size():
    row = rec.build_row(car("gone", "20261018_190021_navstack_sati_odin"), None, None)
    assert row["storage_key"] == "formidable-peacock/gone" and row["size_bytes"] is None
    assert row["start"] == row["end"] and row["source"]["object_present"] is False


def test_former_robot_names_are_stored_under_the_fleet_name():
    old = dict(car("p1", "20261005_210517_navstack_sati_odin"), robot_name="Pincer01")
    row = rec.build_row(old, None, None)
    assert row["robot_name"] == "formidable-peacock"
    assert row["storage_key"] == "Pincer01/p1"                    # where the upload really is
    assert row["source"]["uploaded_as"] == "Pincer01"
    assert rec.build_row(car("f1", "x"), None, None)["source"]["uploaded_as"] is None


def test_overlap_counts_use_the_skew_tolerance():
    t = datetime.datetime(2026, 10, 18, 17, 0, tzinfo=UTC)
    run = {"robot_name": "r", "started_at": t, "ended_at": t + datetime.timedelta(minutes=5)}
    def row(offset_s, robot="r"):
        s = t + datetime.timedelta(seconds=offset_s)
        return {"robot_name": robot, "start": s, "end": s}
    rows = [row(10), row(-4), row(-6), row(304), row(306), row(10, "other"),
            {"robot_name": "r", "start": None, "end": None}]
    assert rec.overlap_counts(rows, [run]) == {"with_start": 6, "overlap_mission_runs": 3}


def test_mission_context_derives_the_mission_runs_uuid():
    from packages.controllers.mission.fleet_recorder import run_uuid
    block = {"run_id": "1a2b3c4d", "order_id": "o-7", "order_rev": 2, "mission_name": "patrol",
             "last_node_id": "n3"}
    assert rec.mission_context(block) == (str(run_uuid("patrol", "1a2b3c4d")), "o-7")
    assert rec.mission_context({"order_id": "o-7"}) == (None, "o-7")
    assert rec.mission_context(None) == (None, None)
    row = rec.build_row(car("m", "20261018_190021_navstack_sati_odin"), None, None, block)
    assert row["run_id"] == str(run_uuid("patrol", "1a2b3c4d")) and row["source"]["mission"] == block

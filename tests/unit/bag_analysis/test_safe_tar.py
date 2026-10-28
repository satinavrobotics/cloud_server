"""safe_tar.py: traversal, links, devices, bombs, collisions."""
import io
import os

import pytest

from packages.services.bag_analysis import safe_tar as st
from tests.unit.bag_analysis.helpers import bag_tar, make_tar

pytestmark = pytest.mark.unit
LIM = dict(max_members=100, max_bytes=10_000)


def extract(tmp_path, data, **kw):
    dest = tmp_path / "dest"
    dest.mkdir(exist_ok=True)
    return dest, st.extract_stream(io.BytesIO(data), dest, **{**LIM, **kw})


def test_good_bag_extracts_and_is_found(tmp_path):
    dest, info = extract(tmp_path, bag_tar())
    assert info["bag_dir"].name == "20261004_200002_navstack_sati_odin"
    assert (info["bag_dir"] / "metadata.yaml").is_file() and info["bytes"] > 100


def test_flat_tar_with_metadata_at_top(tmp_path):
    dest, info = extract(tmp_path, make_tar([("metadata.yaml", b"a: 1\n"), ("x_0.db3", b"z")]))
    assert info["bag_dir"] == dest.resolve()


@pytest.mark.parametrize("name", ["/etc/evil", "../evil", "bag/../../evil", "bag/a\\b"])  # tarfile itself cuts a name at NUL
def test_bad_names_rejected(tmp_path, name):
    with pytest.raises(st.UnsafeArchive):
        extract(tmp_path, make_tar([("bag", ("dir",)), ("bag/metadata.yaml", b"x"), (name, b"x")]))
    assert not (tmp_path / "evil").exists() and not os.path.exists("/etc/evil")


@pytest.mark.parametrize("spec", [("symlink", "/etc/passwd"), ("symlink", "../../x"), ("symlink", "metadata.yaml"),
                                  ("hardlink", "/etc/passwd"), ("hardlink", "metadata.yaml"), ("dev",)])
def test_links_and_devices_rejected(tmp_path, spec):
    with pytest.raises(st.UnsafeArchive):
        extract(tmp_path, make_tar([("bag", ("dir",)), ("bag/metadata.yaml", b"x"), ("bag/evil", spec)]))


def test_symlink_then_write_through_it_cannot_happen(tmp_path):
    # the classic: a symlink member 'bag/out' -> outside, then a file 'bag/out/pwned'
    outside = tmp_path / "outside"
    outside.mkdir()
    data = make_tar([("bag", ("dir",)), ("bag/out", ("symlink", str(outside))), ("bag/out/pwned", b"x")])
    with pytest.raises(st.UnsafeArchive):
        extract(tmp_path, data)
    assert list(outside.iterdir()) == []


def test_member_count_bomb(tmp_path):
    data = make_tar([(f"bag/f{i}", b"") for i in range(50)])
    with pytest.raises(st.UnsafeArchive, match="members"):
        extract(tmp_path, data, max_members=10)


def test_byte_bomb_declared_and_stream(tmp_path):
    with pytest.raises(st.UnsafeArchive, match="bytes"):
        extract(tmp_path, make_tar([("bag/metadata.yaml", b"x" * 5000), ("bag/b", b"x" * 6000)]))
    with pytest.raises(st.UnsafeArchive, match="stream"):
        extract(tmp_path, bag_tar(db3=b"x" * 5000), max_stream_bytes=3000, max_bytes=10**9)


def test_duplicate_file_and_dir_collision(tmp_path):
    with pytest.raises(st.UnsafeArchive, match="twice"):
        extract(tmp_path, make_tar([("bag/metadata.yaml", b"1"), ("bag/metadata.yaml", b"2")]))
    with pytest.raises(st.UnsafeArchive):
        extract(tmp_path, make_tar([("bag/a", b"1"), ("bag/a/b", b"2")]))


def test_garbage_and_missing_metadata(tmp_path):
    with pytest.raises(st.UnsafeArchive, match="tar"):
        extract(tmp_path, b"this is not a tar archive" * 50)
    with pytest.raises(st.UnsafeArchive, match="metadata"):
        extract(tmp_path, make_tar([("bag/a.db3", b"x")]))
    with pytest.raises(st.UnsafeArchive, match="found 2"):
        extract(tmp_path, make_tar([("a/metadata.yaml", b"x"), ("b/metadata.yaml", b"x")]))


def test_read_only_and_remove_tree(tmp_path):
    dest, info = extract(tmp_path, bag_tar())
    st.make_read_only(dest)
    f = info["bag_dir"] / "metadata.yaml"
    if os.geteuid() != 0:
        with pytest.raises(PermissionError):
            open(f, "ab")
    st.remove_tree(dest)
    assert not dest.exists()

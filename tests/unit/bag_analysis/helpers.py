"""Shared builders for the bag-analysis unit tests."""
import copy
import hashlib
import io
import json
import tarfile
from pathlib import Path

import yaml

DOCS = Path(__file__).resolve().parents[3] / "docs" / "bag_analysis"


def lost_time_manifest():
    return yaml.safe_load((DOCS / "lost_time.analyzer.yaml").read_text())


def lost_time_envelope(bag_id="bag1", **over):
    env = json.loads((DOCS / "lost_time.result.example.json").read_text())
    env["bag_id"] = bag_id
    env["artifacts"] = []
    env.update(over)
    return copy.deepcopy(env)


def write_result(analyzer_dir: Path, envelope, artifacts=None):
    """Write result.json (and artifact files {relpath: bytes}) into analyzer_dir."""
    analyzer_dir.mkdir(parents=True, exist_ok=True)
    arts = []
    for rel, data in (artifacts or {}).items():
        p = analyzer_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        arts.append({"name": rel, "path": rel, "media_type": "text/plain", "size": len(data),
                     "sha256": hashlib.sha256(data).hexdigest()})
    if artifacts is not None:
        envelope = dict(envelope, artifacts=arts)
    (analyzer_dir / "result.json").write_text(json.dumps(envelope))
    return envelope


def make_tar(members, path=None) -> bytes:
    """A tar of (name, bytes | ('symlink', target) | ('hardlink', target) | ('dir',) | ('dev',)) members."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, spec in members:
            ti = tarfile.TarInfo(name)
            if isinstance(spec, bytes):
                ti.size = len(spec)
                tf.addfile(ti, io.BytesIO(spec))
            elif spec[0] == "symlink":
                ti.type, ti.linkname = tarfile.SYMTYPE, spec[1]
                tf.addfile(ti)
            elif spec[0] == "hardlink":
                ti.type, ti.linkname = tarfile.LNKTYPE, spec[1]
                tf.addfile(ti)
            elif spec[0] == "dir":
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            elif spec[0] == "dev":
                ti.type = tarfile.CHRTYPE
                tf.addfile(ti)
    data = buf.getvalue()
    if path:
        Path(path).write_bytes(data)
    return data


def bag_tar(bag_name="20261004_200002_navstack_sati_odin", db3=b"x" * 100, params=False) -> bytes:
    members = [(bag_name, ("dir",)), (f"{bag_name}/metadata.yaml", b"rosbag2_bagfile_information: {}\n"),
               (f"{bag_name}/{bag_name}_0.db3", db3)]
    if params:
        members += [(f"{bag_name}/params", ("dir",)), (f"{bag_name}/params/RUN.txt", b"git_describe: v1\n")]
    return make_tar(members)

"""3D reconstruction: the cloud-side top view (packages/api/reconstruction_topview.py).

docs/reconstruction/design.md §7.2: ortho.png + height.png derived from cloud.ply by the API in
a child process. The PNGs are decoded with Pillow (independent of the encoder under test).
"""
import json
import math
import os

import pytest

from packages.api import reconstruction_topview as tv

pytestmark = pytest.mark.unit

PROPS = [("x", "float"), ("y", "float"), ("z", "float"), ("red", "uchar"),
         ("green", "uchar"), ("blue", "uchar"), ("count", "ushort")]


def ply_bytes(points, props=PROPS, fmt="binary_little_endian 1.0", count=None):
    """points: [(x, y, z, r, g, b)]; count ushort 1."""
    import struct
    header = f"ply\nformat {fmt}\ncomment satinav map=t voxel_m=0.05 frame=map\n"
    header += f"element vertex {len(points) if count is None else count}\n"
    header += "".join(f"property {t} {n}\n" for n, t in props) + "end_header\n"
    body = b"".join(struct.pack("<fffBBBH", x, y, z, r, g, b, 1)
                    for x, y, z, r, g, b in points)
    return header.encode() + body


def write(tmp_path, data, name="cloud.ply"):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


# --- header (no numpy) -------------------------------------------------------------------------

class TestHeader:
    def test_parse_and_size(self):
        data = ply_bytes([(0, 0, 0, 1, 2, 3)] * 3)
        header_len, n, props = tv.read_ply_header(data)
        assert n == 3 and props == PROPS and data[header_len - 11:header_len] == b"end_header\n"
        assert tv.ply_size(header_len, n, props) == len(data)

    @pytest.mark.parametrize("data,message", [
        (b"PLY\n", "not a PLY"),
        (b"ply\nformat ascii 1.0\nelement vertex 0\nproperty float x\nend_header\n", "format"),
        (b"ply\nformat binary_little_endian 1.0\nelement vertex 1\nproperty float x\n"
         b"property float y\nend_header\n", "x, y, z"),
        (b"ply\nformat binary_little_endian 1.0\nelement vertex 1\nproperty list uchar int i\n"
         b"end_header\n", "unsupported"),
        (b"ply\nformat binary_little_endian 1.0\nelement vertex 1\nproperty float x\n"
         b"property float y\nproperty float z\nelement face 2\nend_header\n", "element"),
        (b"ply\nformat binary_little_endian 1.0\nelement vertex 1\n", "end_header"),
    ])
    def test_bad_headers(self, data, message):
        with pytest.raises(tv.PlyError) as exc:
            tv.read_ply_header(data)
        assert message in str(exc.value)

    def test_empty_face_element_is_fine(self):
        data = (b"ply\nformat binary_little_endian 1.0\nelement vertex 0\nproperty float x\n"
                b"property float y\nproperty float z\nelement face 0\n"
                b"property list uchar int vertex_indices\nend_header\n")
        assert tv.read_ply_header(data)[1] == 0

    def test_grid_rules(self):
        g = tv.grid(-1.02, 0.5, 5.0, 3.0, voxel_m=0.05, raster_max_px=4096)
        assert g["resolution_m"] == 0.05
        assert g["origin"]["x"] == pytest.approx(-1.05) and g["origin"]["y"] == pytest.approx(0.5)
        assert g["width"] == math.floor((5.0 + 1.05) / 0.05) + 1
        # a big extent: res grows so that neither side exceeds raster_max_px
        g = tv.grid(-100.0, -3.0, 300.0, 50.0, voxel_m=0.05, raster_max_px=100)
        assert g["resolution_m"] == pytest.approx(400 / 98)
        assert g["width"] <= 100 and g["height"] <= 100


# --- derive (numpy) ----------------------------------------------------------------------------

@pytest.fixture
def np():
    return pytest.importorskip("numpy")


def png(path):
    from PIL import Image
    img = Image.open(path)
    img.load()
    return img


# a small scene (map frame): floor at z 0 (grey), a box top at z 0.5 (blue) over the floor, a
# wall at x 2 up to z 2.8 (red), the ceiling at 2.8 (white) - above floor + clip_z
SCENE = []
for i in range(20):
    for j in range(10):
        SCENE.append((i * 0.1 + 0.05, j * 0.1 + 0.05, 0.0, 128, 128, 128))       # floor
for i in range(5):
    for j in range(3):
        SCENE.append((0.5 + i * 0.1 + 0.05, 0.3 + j * 0.1 + 0.05, 0.5, 40, 40, 200))  # box
for k in range(28):
    SCENE.append((2.05, 0.55, k * 0.1, 200, 40, 40))                          # wall column
for i in range(20):
    SCENE.append((i * 0.1 + 0.05, 0.95, 2.8, 240, 240, 240))                  # ceiling


class TestDerive:
    def _derive(self, tmp_path, points, **kw):
        args = dict(z_floor=0.0, clip_z=2.0, voxel_m=0.1, raster_max_px=4096)
        args.update(kw)
        path = write(tmp_path, ply_bytes(points))
        out = tmp_path / "out"
        out.mkdir(exist_ok=True)
        grid = tv.derive(path, str(out), **args)
        return grid, png(out / "ortho.png"), png(out / "height.png")

    def cell(self, grid, x, y):
        res, ox, oy = grid["resolution_m"], grid["origin"]["x"], grid["origin"]["y"]
        return (math.floor((x - ox) / res),
                grid["height"] - 1 - math.floor((y - oy) / res))

    @pytest.mark.parametrize("chunk", [tv.CHUNK_POINTS, 7])
    def test_scene(self, np, tmp_path, chunk):
        grid, ortho, height = self._derive(tmp_path, SCENE, chunk=chunk)
        assert (grid["resolution_m"], grid["z_floor"], grid["clip_abs"]) == (0.1, 0.0, 2.0)
        assert grid["origin"]["x"] == pytest.approx(0.0) and grid["origin"]["y"] == 0.0
        assert (grid["width"], grid["height"]) == (21, 10)
        assert grid["z_offset"] == 0.0 and grid["z_scale"] == 0.01
        assert ortho.mode == "RGBA" and ortho.size == (21, 10)
        assert height.mode.startswith("I") and height.size == (21, 10)
        o, h = np.array(ortho), np.array(height).astype(np.int64)
        # the box wins over the floor below it; its height is 0.5 m
        c, r = self.cell(grid, 0.75, 0.45)
        assert tuple(o[r, c]) == (40, 40, 200, 255) and h[r, c] == 51
        # the floor: grey at height 0 -> value 1
        c, r = self.cell(grid, 1.25, 0.15)
        assert tuple(o[r, c]) == (128, 128, 128, 255) and h[r, c] == 1
        # the wall column: its highest point below the clip (1.9 m) wins; never the ceiling
        c, r = self.cell(grid, 2.05, 0.55)
        assert tuple(o[r, c]) == (200, 40, 40, 255) and h[r, c] == 191
        assert not ((o[..., 0] == 240) & (o[..., 3] == 255)).any()
        # row 0 is the +y edge: y 0.95 is in row 0, y 0.05 in the last row
        assert self.cell(grid, 0.05, 0.95)[1] == 0 and self.cell(grid, 0.05, 0.05)[1] == 9
        # empty cells: transparent and 0 (the wall's x = 2.05 column, off the wall's row)
        c, r = self.cell(grid, 2.05, 0.15)
        assert tuple(o[r, c]) == (0, 0, 0, 0) and h[r, c] == 0
        assert grid["top_view"] == {"points": len(SCENE),
                                    "points_rastered": len(SCENE) - 20 - 8,
                                    "cells_filled": 201}

    def test_floor_and_clip_from_the_arguments(self, np, tmp_path):
        grid, ortho, _ = self._derive(tmp_path, SCENE, z_floor=0.3, clip_z=0.1)
        # only points below 0.4: the floor; the box top (0.5) is clipped away
        o = np.array(ortho)
        c, r = self.cell(grid, 0.75, 0.45)
        assert grid["clip_abs"] == pytest.approx(0.4)
        assert tuple(o[r, c]) == (128, 128, 128, 255)

    def test_resolution_is_capped_by_raster_max_px(self, np, tmp_path):
        pts = [(-50.0, -1.0, 0.0, 1, 2, 3), (50.0, 1.0, 0.0, 4, 5, 6)]
        grid, ortho, height = self._derive(tmp_path, pts, voxel_m=0.05, raster_max_px=64)
        assert grid["resolution_m"] == pytest.approx(100 / 62)
        assert ortho.size[0] <= 64 and ortho.size == height.size

    def test_height_clamps_at_65535(self, np, tmp_path):
        pts = [(0.0, 0.0, -700.0, 1, 1, 1), (1.0, 0.0, 10.0, 2, 2, 2)]
        grid, _, height = self._derive(tmp_path, pts, clip_z=20.0, voxel_m=1.0)
        h = np.array(height).astype(np.int64)
        assert h.max() == 65535 and grid["z_offset"] == -700.0

    def test_no_point_below_the_clip(self, np, tmp_path):
        with pytest.raises(tv.TopViewError) as exc:
            self._derive(tmp_path, [(0, 0, 3.0, 1, 1, 1)])
        assert exc.value.reason == "top_view_failed" and "clip height" in exc.value.message

    def test_truncated_or_mis_sized(self, np, tmp_path):
        data = ply_bytes(SCENE[:5])
        path = write(tmp_path, data[:-3])
        with pytest.raises(tv.PlyError):
            tv.derive(path, str(tmp_path), z_floor=0, clip_z=2, voxel_m=0.1, raster_max_px=64)

    def test_nan_points_are_ignored_and_no_colour_is_grey(self, np, tmp_path):
        props = [("x", "float"), ("y", "float"), ("z", "float"), ("i", "uchar"),
                 ("red", "uchar"), ("green", "uchar"), ("blue", "uchar")]
        import struct
        header = ("ply\nformat binary_little_endian 1.0\nelement vertex 2\n"
                  + "".join(f"property {t} {n}\n" for n, t in props) + "end_header\n").encode()
        body = struct.pack("<fffBBBB", 0.0, 0.0, 0.0, 9, 10, 20, 30) + \
            struct.pack("<fffBBBB", float("nan"), 1.0, 0.0, 9, 1, 1, 1)
        path = write(tmp_path, header + body)
        out = tmp_path / "o"
        out.mkdir()
        grid = tv.derive(path, str(out), z_floor=0, clip_z=2, voxel_m=0.1, raster_max_px=64)
        assert (grid["width"], grid["height"]) == (1, 1)
        assert tuple(np.array(png(out / "ortho.png"))[0, 0]) == (10, 20, 30, 255)
        path = write(tmp_path, ply_bytes([], props=PROPS[:3], count=1) + b"\0" * 12)
        grid = tv.derive(path, str(out), z_floor=0, clip_z=2, voxel_m=0.1, raster_max_px=64)
        assert tuple(np.array(png(out / "ortho.png"))[0, 0]) == (128, 128, 128, 255)

    def test_png_bands(self, np, tmp_path, monkeypatch):
        """More rows than one compressed band: every row survives the Up filter."""
        monkeypatch.setattr(tv, "PNG_BAND_ROWS", 3)
        pts = [(0.05, j * 0.1 + 0.05, j * 0.01, j, 255 - j, 7) for j in range(11)]
        grid, ortho, height = self._derive(tmp_path, pts)
        o, h = np.array(ortho), np.array(height).astype(np.int64)
        assert grid["height"] == 11
        for j in range(11):
            r = 10 - j
            assert tuple(o[r, 0]) == (j, 255 - j, 7, 255) and h[r, 0] == j + 1


class TestSubprocess:
    async def test_runs_in_a_child_and_maps_errors(self, np, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        path = write(tmp_path, ply_bytes(SCENE))
        kw = dict(z_floor=0.0, clip_z=2.0, voxel_m=0.1, raster_max_px=4096, mem_mb=1024,
                  timeout_s=120)
        grid = await tv.derive_in_subprocess(path, str(out), **kw)
        assert (grid["width"], grid["height"]) == (21, 10)
        assert os.path.getsize(out / "ortho.png") > 0 and os.path.getsize(out / "height.png") > 0
        bad = write(tmp_path, b"ply\nformat ascii 1.0\nend_header\n", "bad.ply")
        with pytest.raises(tv.TopViewError) as exc:
            await tv.derive_in_subprocess(bad, str(out), **kw)
        assert exc.value.reason == "bad_output" and "format" in exc.value.message
        high = write(tmp_path, ply_bytes([(0, 0, 9.0, 1, 1, 1)]), "high.ply")
        with pytest.raises(tv.TopViewError) as exc:
            await tv.derive_in_subprocess(high, str(out), **kw)
        assert exc.value.reason == "top_view_failed" and "clip height" in exc.value.message

    async def test_timeout_kills_the_child(self, tmp_path, monkeypatch):
        slow = tmp_path / "slow.py"
        slow.write_text("import time; time.sleep(30)\n")
        monkeypatch.setattr(tv, "SCRIPT", str(slow))
        with pytest.raises(tv.TopViewError) as exc:
            await tv.derive_in_subprocess("x", str(tmp_path), z_floor=0, clip_z=2, voxel_m=0.1,
                                          raster_max_px=64, mem_mb=0, timeout_s=0.5)
        assert "took over" in exc.value.message

    async def test_a_crashed_child_is_top_view_failed(self, tmp_path, monkeypatch):
        crash = tmp_path / "crash.py"
        crash.write_text("import os, signal; os.kill(os.getpid(), signal.SIGKILL)\n")
        monkeypatch.setattr(tv, "SCRIPT", str(crash))
        with pytest.raises(tv.TopViewError) as exc:
            await tv.derive_in_subprocess("x", str(tmp_path), z_floor=0, clip_z=2, voxel_m=0.1,
                                          raster_max_px=64, mem_mb=0, timeout_s=10)
        assert exc.value.reason == "top_view_failed" and "signal 9" in exc.value.message

    def test_main_prints_the_grid(self, np, tmp_path, capsys):
        out = tmp_path / "o"
        out.mkdir()
        path = write(tmp_path, ply_bytes(SCENE))
        assert tv.main(["x", json.dumps({"ply": path, "out_dir": str(out), "z_floor": 0,
                                         "clip_z": 2, "voxel_m": 0.1,
                                         "raster_max_px": 4096})]) == 0
        assert json.loads(capsys.readouterr().out)["width"] == 21

"""packages/utils/cell_raster.py: the rasteriser moved out of drive_stats (its own tests stay in
test_drive_stats.py) and the block placement (docs/region_stats_2026-10-27.md 6.1)."""
import numpy as np
import pytest

from packages.api import drive_stats
from packages.utils import cell_raster as cr

pytestmark = pytest.mark.unit


def seg_of(*rows):
    """rows: (run, t0, t1, x0, y0, x1, y1)."""
    a = np.array(rows, float)
    return cr.Segments(a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5], a[:, 6])


def cells(keys):
    ax, ay = cr.key_cells(keys)
    return sorted(zip(ax.tolist(), ay.tolist()))


def test_drive_stats_reexports_the_same_objects():
    for name in ("Params", "Accumulator", "Tiles", "Segments", "track_segments", "recovery_overlap", "leg_movements",
                 "cell_key", "key_cells", "CELL_M", "SAMPLE_DIV", "MAX_SAMPLES_PER_SEGMENT", "CHUNK_SEGMENTS", "_OFF"):
        assert getattr(drive_stats, name) is getattr(cr, name)


class TestBlockCells:
    seg = seg_of((0, 100.0, 110.0, 0.0, 0.05, 10.0, 0.05), (0, 112.0, 114.0, 10.0, 0.05, 11.0, 0.05),
                 (1, 100.0, 110.0, 50.0, 0.05, 60.0, 0.05))

    def test_inside_a_segment_interpolates(self):
        keys, lost = cr.block_cells(self.seg, np.array([0, 1]), np.array([105.0, 105.0]), 5.0)
        assert lost == 0 and cells(keys) == [(50, 0), (550, 0)]

    def test_gap_takes_the_nearest_end_within_max_gap(self):
        keys, lost = cr.block_cells(self.seg, np.array([0]), np.array([111.0]), 5.0)   # 1 s after seg 0, 1 s before seg 1
        assert lost == 0 and len(keys) == 1
        assert cells(keys)[0][0] in (99, 100)          # x = 10.0 (float rounding decides the cell)
        keys, lost = cr.block_cells(self.seg, np.array([0]), np.array([100.5]), 5.0)
        assert lost == 0 and cells(keys) == [(5, 0)]   # inside the first segment: x = 0.5

    def test_too_far_or_no_segment_is_unplaced(self):
        keys, lost = cr.block_cells(self.seg, np.array([0, 0, 7]), np.array([130.0, 90.0, 105.0]), 5.0)
        assert len(keys) == 0 and lost == 3

    def test_before_first_segment_within_gap(self):
        keys, lost = cr.block_cells(self.seg, np.array([1]), np.array([98.0]), 5.0)
        assert lost == 0 and cells(keys) == [(500, 0)]

    def test_empty(self):
        keys, lost = cr.block_cells(self.seg, np.array([], np.int64), np.array([]), 5.0)
        assert len(keys) == 0 and lost == 0


class TestLegBlockCells:
    legs = [{"seq": 3, "t0": 100.0, "t1": 110.0, "a": (0.0, 0.05), "b": (10.0, 0.05)},
            {"seq": 4, "t0": None, "t1": None, "a": (10.0, 0.05), "b": (20.0, 0.05)}]

    def test_seq_goes_to_the_end_node(self):
        keys, lost = cr.leg_block_cells([(None, 3), (105.0, 4)], self.legs)
        assert lost == 0 and cells(keys) == [(100, 0), (200, 0)]

    def test_time_inside_a_leg_interpolates(self):
        keys, lost = cr.leg_block_cells([(105.0, None)], self.legs)
        assert lost == 0 and cells(keys) == [(50, 0)]

    def test_nothing_known(self):
        keys, lost = cr.leg_block_cells([(500.0, None), (None, 9), (None, None)], self.legs)
        assert len(keys) == 0 and lost == 3

from packages.services.graph_builder import scene_sources as ss


class FakeDb:
    def __init__(self, png=b"png"):
        self.png, self.asked = png, []

    def get_costmap(self, node_id, layer, map_id=None):
        self.asked.append((node_id, layer, map_id))
        return self.png


def test_png_fetcher_argument_order():
    db = FakeDb()
    assert ss.png_fetcher(db)("m", "n1", "occupancy") == b"png"
    assert db.asked == [("n1", "occupancy", "m")]


class Conn:
    def __init__(self, row=None, boom=False):
        self.row, self.boom, self.closed, self.sql = row, boom, False, None

    def execute(self, sql, params=None):
        if self.boom:
            raise RuntimeError("down")
        self.sql, self.params = sql, params
        return self

    def fetchone(self):
        return self.row

    def close(self):
        self.closed = True


def test_footprint_reads_factsheet_and_closes():
    c = Conn(("1.2", "0.6"))
    assert ss.PgFootprint(lambda: c)("r1") == (1.2, 0.6)
    assert c.closed and c.params == ("r1",) and "factsheet" in c.sql


def test_footprint_unknown_cases():
    assert ss.PgFootprint(lambda: Conn(None))("r") == ss.UNKNOWN
    assert ss.PgFootprint(lambda: Conn((None, None)))("r") == ss.UNKNOWN
    c = Conn(boom=True)
    assert ss.PgFootprint(lambda: c)("r") == ss.UNKNOWN and c.closed
    def no_conn():
        raise RuntimeError("no db")
    assert ss.PgFootprint(no_conn)("r") == ss.UNKNOWN

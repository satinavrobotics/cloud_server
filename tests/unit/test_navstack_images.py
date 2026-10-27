"""packages/api/navstack_images.py: the registry client against a fake registry (index -> arm64 manifest
-> config labels, `-latest` folded as an alias, caching, unreachable registry), parse_sw_version, the
fleet software view and the two read routes."""
import hashlib
import json
import os
from unittest.mock import MagicMock, patch

for _k in ("ARANGO_PASSWORD", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_PASSWORD"):
    os.environ.setdefault(_k, "test")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from packages.api import navstack_images as ni  # noqa: E402

pytestmark = pytest.mark.unit

INDEX = "application/vnd.oci.image.index.v1+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"


def _digest(obj) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


class FakeRegistry:
    """A registry of sati/navstack: add() pushes an image under tags, requests are counted."""

    def __init__(self):
        self.tags = {}            # tag -> index digest
        self.bodies = {}          # digest -> (body, content-type)
        self.blobs = {}
        self.requests = []
        self.down = None          # an exception to raise, or an HTTP status
        self.no_repo = False

    def add(self, tag, sha, created, subject="subject", tags=(), arch="arm64", labels=None, size=100):
        info = {"git_sha": sha, "commit_subject": subject, "branch": tag.split("-")[0]}
        cfg = {"created": created, "config": {"Labels": labels if labels is not None else {
            "org.opencontainers.image.revision": sha,
            ni.BUILD_INFO_LABEL: json.dumps(info)}}}
        cfg_digest = _digest(cfg)
        self.blobs[cfg_digest] = cfg
        manifest = {"mediaType": MANIFEST, "config": {"digest": cfg_digest},
                    "layers": [{"size": size}, {"size": size}]}
        m_digest = _digest(manifest)
        self.bodies[m_digest] = (manifest, MANIFEST)
        index = {"mediaType": INDEX, "manifests": [
            {"digest": _digest({"amd64": sha}), "platform": {"os": "linux", "architecture": "amd64"}},
            {"digest": m_digest, "platform": {"os": "linux", "architecture": arch}}]}
        i_digest = _digest(index)
        self.bodies[i_digest] = (index, INDEX)
        for t in (tag,) + tuple(tags):
            self.tags[t] = i_digest
        return m_digest

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        if isinstance(self.down, Exception):
            raise self.down
        if self.down:
            return httpx.Response(self.down)
        path = request.url.path
        assert path.startswith("/v2/sati/navstack/")
        rest = path[len("/v2/sati/navstack/"):]
        if rest == "tags/list":
            if self.no_repo:
                return httpx.Response(404, json={"errors": [{"code": "NAME_UNKNOWN"}]})
            return httpx.Response(200, json={"name": "sati/navstack", "tags": sorted(self.tags)})
        if rest.startswith("manifests/"):
            ref = rest[len("manifests/"):]
            digest = self.tags.get(ref, ref)
            if digest not in self.bodies:
                return httpx.Response(404)
            body, ctype = self.bodies[digest]
            return httpx.Response(200, json=body, headers={"Docker-Content-Digest": digest,
                                                           "Content-Type": ctype})
        if rest.startswith("blobs/"):
            blob = self.blobs.get(rest[len("blobs/"):])
            return httpx.Response(200, json=blob) if blob else httpx.Response(404)
        return httpx.Response(404)

    def client(self):
        return ni.NavstackRegistry(
            base_url="http://reg", image="sati/navstack", arch="arm64", ttl_s=60,
            http_factory=lambda timeout=None: httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
            clock=self.now)

    t = 0.0

    def now(self):
        return self.t


@pytest.fixture
def reg():
    r = FakeRegistry()
    r.add("humble-20261027-c879696", "c879696aaaa", "2026-10-27T10:00:00Z", "newest", tags=("humble-latest",))
    r.add("humble-20261026-9497ec0", "9497ec0bbbb", "2026-10-26T10:00:00Z", "older", size=50)
    r.add("jazzy-20261020-1111111", "1111111cccc", "2026-10-20T10:00:00Z")
    return r


class TestRegistry:
    async def test_rows_newest_first_with_latest_folded_as_alias(self, reg):
        out = await reg.client().list_images("humble")
        assert out["branch"] == "humble" and out["latest_tag"] == "humble-20261027-c879696"
        assert [r["tag"] for r in out["images"]] == ["humble-20261027-c879696", "humble-20261026-9497ec0"]
        first = out["images"][0]
        assert first["aliases"] == ["humble-latest"]
        assert first["git_sha"] == "c879696aaaa" and first["commit_subject"] == "newest"
        assert first["created"] == "2026-10-27T10:00:00Z" and first["size_bytes"] == 200
        assert first["digest"].startswith("sha256:") and first["build_info"]["branch"] == "humble"
        assert out["images"][1]["aliases"] == [] and out["images"][1]["size_bytes"] == 100
        assert set(first) == {"tag", "digest", "git_sha", "created", "commit_subject", "size_bytes",
                              "aliases", "build_info"}
        assert out["fetched_at"].endswith("+00:00")
        # the arm64 manifest was used, not the amd64 entry
        assert not any("1111111" in p for p in reg.requests)

    async def test_other_branches_are_not_listed(self, reg):
        out = await reg.client().list_images("jazzy")
        assert [r["tag"] for r in out["images"]] == ["jazzy-20261020-1111111"]
        assert out["latest_tag"] is None

    async def test_sha_falls_back_to_the_oci_revision_label(self, reg):
        reg.add("humble-20261028-abcdef0", "abcdef0dddd", "2026-10-28T10:00:00Z",
                labels={"org.opencontainers.image.revision": "abcdef0dddd"})
        out = await reg.client().list_images("humble")
        row = out["images"][0]
        assert row["tag"] == "humble-20261028-abcdef0" and row["git_sha"] == "abcdef0dddd"
        assert row["commit_subject"] is None and row["build_info"] is None

    async def test_latest_alone_is_its_own_row_and_is_the_latest_tag(self, reg):
        reg.add("rolling-latest", "ffffffeeee", "2026-10-28T10:00:00Z")
        out = await reg.client().list_images("rolling")
        assert [r["tag"] for r in out["images"]] == ["rolling-latest"]
        assert out["latest_tag"] == "rolling-latest"

    async def test_latest_sorting_before_the_versioned_tag_still_folds(self, reg):
        # branch "a": "a-latest" < "a-z..." sorts first; the versioned name must still win
        reg.add("a-zeta", "aaaaaaa1111", "2026-10-28T10:00:00Z", tags=("a-latest",))
        out = await reg.client().list_images("a")
        assert out["images"][0]["tag"] == "a-zeta" and out["images"][0]["aliases"] == ["a-latest"]
        assert out["latest_tag"] == "a-zeta"

    async def test_a_tag_without_the_arch_is_skipped(self, reg):
        reg.add("humble-20261030-badbad0", "badbad0eeee", "2026-10-30T10:00:00Z", arch="amd64")
        out = await reg.client().list_images("humble")
        assert "humble-20261030-badbad0" not in [r["tag"] for r in out["images"]]
        assert len(out["images"]) == 2

    async def test_nothing_pushed_is_an_empty_list_not_an_error(self, reg):
        reg.no_repo = True
        out = await reg.client().list_images("humble")
        assert out["images"] == [] and out["latest_tag"] is None

    async def test_cache_60s_and_immutable_rows_forever(self, reg):
        c = reg.client()
        await c.list_images("humble")
        n = len(reg.requests)
        await c.list_images("humble")
        assert len(reg.requests) == n                       # inside 60 s: no request at all
        reg.t = 61
        reg.add("humble-20261029-0a0a0a0", "0a0a0a0ffff", "2026-10-29T10:00:00Z")
        out = await c.list_images("humble")
        assert out["images"][0]["tag"] == "humble-20261029-0a0a0a0"
        again = [p for p in reg.requests[n:] if "20261026-9497ec0" in p or "20261027-c879696" in p]
        assert again == []                                   # versioned rows were not refetched
        assert "/v2/sati/navstack/tags/list" in reg.requests[n:]
        assert any(p.endswith("manifests/humble-latest") for p in reg.requests[n:])   # latest is

    async def test_latest_moving_is_picked_up_after_the_ttl(self, reg):
        c = reg.client()
        first = await c.list_images("humble")
        assert first["latest_tag"] == "humble-20261027-c879696"
        reg.add("humble-20261029-0a0a0a0", "0a0a0a0ffff", "2026-10-29T10:00:00Z", tags=("humble-latest",))
        reg.t = 61
        second = await c.list_images("humble")
        assert second["latest_tag"] == "humble-20261029-0a0a0a0"
        old = [r for r in second["images"] if r["tag"] == "humble-20261027-c879696"][0]
        assert old["aliases"] == []

    async def test_concurrent_refreshes_share_one_fetch(self, reg):
        import asyncio
        c = reg.client()
        await asyncio.gather(*[c.list_images("humble") for _ in range(5)])
        assert reg.requests.count("/v2/sati/navstack/tags/list") == 1

    async def test_unreachable_registry_is_an_error_never_an_empty_list(self, reg):
        reg.down = httpx.ConnectError("no route")
        with pytest.raises(ni.RegistryError) as err:
            await reg.client().list_images("humble")
        assert "not reachable" in err.value.detail and err.value.status == 502

    async def test_timeout_and_server_error(self, reg):
        reg.down = httpx.ReadTimeout("slow")
        with pytest.raises(ni.RegistryError) as err:
            await reg.client().list_images("humble")
        assert "timed out" in err.value.detail
        reg.down = 503
        with pytest.raises(ni.RegistryError) as err:
            await reg.client().list_images("humble")
        assert "HTTP 503" in err.value.detail

    async def test_transport_failure_on_one_tag_is_an_error_not_a_skip(self, reg):
        c = reg.client()
        calls = {"n": 0}
        base = reg.handler

        def flaky(request):
            if "manifests/humble-2026" in request.url.path:
                calls["n"] += 1
                raise httpx.ConnectError("reset")
            return base(request)
        c._http_factory = lambda timeout=None: httpx.AsyncClient(transport=httpx.MockTransport(flaky))
        with pytest.raises(ni.RegistryError):
            await c.list_images("humble")

    async def test_has_tag_and_resolve_know_aliases(self, reg):
        c = reg.client()
        assert await c.has_tag("humble-20261026-9497ec0")
        assert await c.has_tag("humble-latest")
        assert not await c.has_tag("humble-20200101-0000000")
        row = await c.resolve("humble-latest")
        assert row["tag"] == "humble-20261027-c879696"
        assert await c.resolve("humble-20200101-0000000") is None


class TestParse:
    @pytest.mark.parametrize("sw,expected", [
        ("sati/navstack:humble-20261027-9497ec0", ("image", "humble-20261027-9497ec0", "9497ec0")),
        ("100.85.3.47:5050/sati/navstack:humble-20261027-9497ec0",
         ("image", "humble-20261027-9497ec0", "9497ec0")),
        ("sati/navstack:humble-latest", ("image", "humble-latest", None)),
        ("sati/navstack:jazzy-fix-a-b-20261027-9497ec0abc", ("image", "jazzy-fix-a-b-20261027-9497ec0abc",
                                                              "9497ec0abc")),
        ("dev:c879696", ("dev", None, "c879696")),
        ("dev:v1.2-3-gc879696-dirty", ("dev", None, "c879696")),
        ("dev:unknown", ("dev", None, None)),
        ("1.2.3", (None, None, None)),
        ("other/image:tag", (None, None, None)),
        ("", (None, None, None)),
        (None, (None, None, None)),
        ("  sati/navstack:humble-20261027-9497ec0 ", ("image", "humble-20261027-9497ec0", "9497ec0")),
    ])
    def test_parse_sw_version(self, sw, expected):
        got = ni.parse_sw_version(sw)
        assert (got["mode"], got["tag"], got["sha"]) == expected

    def test_matches_for_verification(self):
        assert ni.sw_version_matches("image", "humble-1-abcdef0", "sati/navstack:humble-1-abcdef0")
        assert not ni.sw_version_matches("image", "humble-1-abcdef0", "sati/navstack:humble-2-abcdef1")
        assert not ni.sw_version_matches("image", "humble-1-abcdef0", "dev:abcdef0")
        assert ni.sw_version_matches("dev", None, "dev:abcdef0")
        assert not ni.sw_version_matches("dev", None, "sati/navstack:humble-1-abcdef0")
        assert not ni.sw_version_matches("dev", None, None)

    def test_branch_check(self):
        assert ni.check_branch(None) == "humble" and ni.check_branch("jazzy") == "jazzy"
        for bad in ("", "a b", "../x", "-x", "a" * 70):
            with pytest.raises(HTTPException) as err:
                ni.check_branch(bad)
            assert err.value.status_code == 422


LATEST = "humble-20261027-c879696"


class TestSoftwareRow:
    def row(self, sw, **kw):
        return ni.software_row("r1", sw, None, "humble", kw.get("latest", LATEST), kw.get("aliases", ()))

    def test_image_on_latest_is_current(self):
        r = self.row(f"sati/navstack:{LATEST}")
        assert r["behind_latest"] is False and r["mode"] == "image" and r["sha"] == "c879696"

    def test_image_on_another_tag_is_behind(self):
        assert self.row("sati/navstack:humble-20261026-9497ec0")["behind_latest"] is True

    def test_the_latest_alias_counts_as_current(self):
        r = self.row("sati/navstack:humble-latest", aliases=("humble-latest",))
        assert r["behind_latest"] is False

    def test_dev_unknown_other_branch_and_no_latest_are_never_behind(self):
        assert self.row("dev:abc1234")["behind_latest"] is False
        assert self.row("weird")["behind_latest"] is False and self.row(None)["mode"] is None
        assert self.row("sati/navstack:jazzy-20200101-9497ec0")["behind_latest"] is False
        assert self.row("sati/navstack:humble-20200101-9497ec0", latest=None)["behind_latest"] is False

    def test_shape(self):
        r = self.row("dev:abc1234")
        assert set(r) == {"robot_name", "sw_version", "mode", "tag", "sha", "last_seen", "behind_latest"}


# ---- fleet view and routes ------------------------------------------------------------------

import datetime  # noqa: E402

SEEN = datetime.datetime(2026, 10, 27, 12, 0, tzinfo=datetime.timezone.utc)


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = None

    async def execute(self, sql, params=()):
        self.sql = sql

    async def fetchall(self):
        return self.rows


class FakeDb:
    def __init__(self, rows):
        self.cursor_ = FakeCursor(rows)

    def connection(self):
        db = self

        class Conn:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def cursor(self):
                class Cur:
                    async def __aenter__(s):
                        return db.cursor_

                    async def __aexit__(s, *exc):
                        return False
                return Cur()
        return Conn()


ROWS = [("alpha", f"sati/navstack:{LATEST}", SEEN),
        ("beta", "sati/navstack:humble-20261026-9497ec0", SEEN),
        ("gamma", "dev:abc1234", None),
        ("delta", None, None)]


class TestFleet:
    async def test_behind_latest_per_robot(self, reg):
        out = await ni.fleet_software(FakeDb(ROWS), reg.client(), "humble")
        assert out["branch"] == "humble" and out["latest_tag"] == LATEST and out["registry_error"] is None
        by = {r["robot_name"]: r for r in out["robots"]}
        assert by["alpha"]["behind_latest"] is False and by["beta"]["behind_latest"] is True
        assert by["gamma"]["mode"] == "dev" and by["gamma"]["behind_latest"] is False
        assert by["delta"]["mode"] is None and by["alpha"]["last_seen"] == SEEN.isoformat()
        assert by["gamma"]["last_seen"] is None

    async def test_registry_down_still_lists_robots(self, reg):
        reg.down = httpx.ConnectError("no route")
        out = await ni.fleet_software(FakeDb(ROWS), reg.client(), "humble")
        assert out["latest_tag"] is None and "not reachable" in out["registry_error"]
        assert len(out["robots"]) == 4 and not any(r["behind_latest"] for r in out["robots"])


class TestRoutes:
    @pytest.fixture
    def app(self, reg):
        import packages.api.main as main
        svc = MagicMock()
        svc.navstack_registry = reg.client()
        svc.database = FakeDb(ROWS)
        with patch.object(main, "service", svc):
            yield main.app

    async def get(self, app, url):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            return await c.get(url)

    async def test_images_route_shape(self, app):
        r = await self.get(app, "/api/v1/navstack/images?branch=humble")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"branch", "latest_tag", "images", "fetched_at"}
        assert body["images"][0]["aliases"] == ["humble-latest"]

    async def test_images_route_defaults_to_humble_and_validates(self, app):
        assert (await self.get(app, "/api/v1/navstack/images")).json()["branch"] == "humble"
        assert (await self.get(app, "/api/v1/navstack/images?branch=a%20b")).status_code == 422

    async def test_images_route_registry_down_is_502_with_detail(self, app, reg):
        reg.down = httpx.ConnectError("no route")
        r = await self.get(app, "/api/v1/navstack/images")
        assert r.status_code == 502 and "not reachable" in r.json()["detail"]

    async def test_fleet_software_route_shape(self, app):
        r = await self.get(app, "/api/v1/fleet/software")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"branch", "latest_tag", "robots", "registry_error"}
        assert [x["robot_name"] for x in body["robots"]] == ["alpha", "beta", "gamma", "delta"]

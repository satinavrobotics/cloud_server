"""A minimal ArangoDB HTTP client for the scene-graph test instance (stdlib only).

It refuses to talk to anything but the throw-away test instance: the URL must come from
SG_TEST_ARANGO_URL (see ~/.sg_test_arango.env) and must not be the live port.
"""
import base64
import json
import os
import urllib.request
from urllib.parse import urlparse

LIVE_PORT = 8529


def _load_env_file(path):
    env = {}
    if os.path.exists(path):
        for line in open(path):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                env[k] = v
    return env


class TestArango:
    def __init__(self, env_file=os.path.expanduser("~/.sg_test_arango.env")):
        env = {**_load_env_file(env_file), **{k: v for k, v in os.environ.items() if k.startswith("SG_TEST_")}}
        self.url = env["SG_TEST_ARANGO_URL"].rstrip("/")
        if urlparse(self.url).port == LIVE_PORT:
            raise RuntimeError("refusing to use the live ArangoDB port")
        self.db = env.get("SG_TEST_ARANGO_DB", "sg_test")
        tok = base64.b64encode(f"{env['SG_TEST_ARANGO_USER']}:{env['SG_TEST_ARANGO_PASSWORD']}".encode()).decode()
        self._auth = f"Basic {tok}"

    def _req(self, method, path, body=None, db=True):
        url = self.url + (f"/_db/{self.db}" if db else "") + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={"Authorization": self._auth})
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read())

    def ensure_db(self):
        r = self._req("POST", "/_api/database", {"name": self.db}, db=False)
        assert not r.get("error") or r.get("errorNum") == 1207, r

    def recreate_collection(self, name, edge=False):
        self._req("DELETE", f"/_api/collection/{name}")
        r = self._req("POST", "/_api/collection", {"name": name, "type": 3 if edge else 2})
        assert not r.get("error"), r

    def insert(self, name, docs):
        for i in range(0, len(docs), 500):
            r = self._req("POST", f"/_api/document/{name}", docs[i:i + 500])
            errs = [x for x in r if isinstance(x, dict) and x.get("error")]
            assert not errs, errs[:2]

    def query(self, aql, bind=None):
        r = self._req("POST", "/_api/cursor", {"query": aql, "bindVars": bind or {}, "batchSize": 1000})
        assert not r.get("error"), r
        out = list(r["result"])
        while r.get("hasMore"):
            r = self._req("PUT", f"/_api/cursor/{r['id']}")
            out += r["result"]
        return out

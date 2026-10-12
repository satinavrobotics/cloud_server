"""Connection settings and plumbing for the end-to-end harness: the throw-away stack of
docker_compose/scene_graph_e2e.yaml (ArangoDB, MinIO, Postgres), the async runtime the Postgres
side needs, and the schema step (runtime table creation + alembic `upgrade head`).

Nothing here reads the production settings: every value comes from `SG_E2E_*` variables whose
defaults are the compose file's, and the production ports are refused outright.
"""
import asyncio
import importlib.util
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "packages" / "api" / "migrations" / "versions"
ALEMBIC_INI = REPO / "packages" / "api" / "alembic.ini"
PRODUCTION_PORTS = {8529, 9000, 5432}


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Cfg:
    arango_host: str
    arango_port: int
    arango_user: str
    arango_password: str
    arango_db: str
    minio_host: str
    minio_port: int
    minio_user: str
    minio_password: str
    pg_host: str
    pg_port: int
    pg_user: str
    pg_password: str
    pg_db: str
    alembic_image: str

    @property
    def conninfo(self) -> str:
        return (f"dbname={self.pg_db} user={self.pg_user} host={self.pg_host} "
                f"password={self.pg_password} port={self.pg_port}")


def load_cfg() -> Cfg:
    cfg = Cfg(
        arango_host=_env("SG_E2E_ARANGO_HOST", "127.0.0.1"),
        arango_port=int(_env("SG_E2E_ARANGO_PORT", "28529")),
        arango_user="root",
        arango_password=_env("SG_E2E_ARANGO_PASSWORD", "sg-e2e-arango"),
        arango_db=_env("SG_E2E_ARANGO_DB", "sg_e2e_topomap"),
        minio_host=_env("SG_E2E_MINIO_HOST", "127.0.0.1"),
        minio_port=int(_env("SG_E2E_MINIO_PORT", "29000")),
        minio_user=_env("SG_E2E_MINIO_USER", "sge2eminio"),
        minio_password=_env("SG_E2E_MINIO_PASSWORD", "sge2eminio-secret"),
        pg_host=_env("SG_E2E_POSTGRES_HOST", "127.0.0.1"),
        pg_port=int(_env("SG_E2E_POSTGRES_PORT", "25432")),
        pg_user="postgres",
        pg_password=_env("SG_E2E_POSTGRES_PASSWORD", "sg-e2e-postgres"),
        pg_db=_env("SG_E2E_POSTGRES_DB", "mission"),
        alembic_image=_env("SG_E2E_ALEMBIC_IMAGE", "wp6-test:py310"),
    )
    for name, port in (("ArangoDB", cfg.arango_port), ("MinIO", cfg.minio_port),
                       ("Postgres", cfg.pg_port)):
        if port in PRODUCTION_PORTS and os.environ.get("SG_E2E_ALLOW_DEFAULT_PORTS") != "1":
            raise RuntimeError(f"{name} port {port} is a production default: this harness "
                               "creates and drops data, use the compose stack's own ports")
    return cfg


def run_id() -> str:
    return uuid.uuid4().hex[:6]


# ------------------------------------------------------------------------------ async side

class AsyncRuntime:
    """An event loop in a thread: the Postgres layer of the product is async, the worker is not."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._main, name="sg-e2e-loop", daemon=True)
        self._thread.start()

    def _main(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro: Any, timeout: float = 120.0) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5)


class RecordingPublisher:
    """Stands in for graph-builder's UpdatePublisher: keeps the websocket messages."""

    def __init__(self) -> None:
        self.messages: List[Dict[str, Any]] = []

    async def publish(self, map_id: str, message: Dict[str, Any]) -> None:
        self.messages.append(dict(message, _map=map_id))


# ------------------------------------------------------------------------------ readiness

def wait_until(predicate, what: str, timeout: float = 60.0, every: float = 0.5) -> Any:
    deadline = time.monotonic() + timeout
    last: Optional[BaseException] = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(every)
    raise TimeoutError(f"{what} not ready within {timeout:.0f}s" + (f" ({last!r})" if last else ""))


def wait_for_stack(cfg: Cfg, timeout: float = 90.0) -> None:
    import urllib.error
    import urllib.request

    import psycopg

    def arango() -> bool:
        req = urllib.request.Request(
            f"http://{cfg.arango_host}:{cfg.arango_port}/_api/version")
        try:
            urllib.request.urlopen(req, timeout=2)
        except urllib.error.HTTPError as exc:   # 401: up, auth enforced
            return exc.code in (200, 401)
        return True

    def minio() -> bool:
        urllib.request.urlopen(
            f"http://{cfg.minio_host}:{cfg.minio_port}/minio/health/live", timeout=2)
        return True

    def pg() -> bool:
        with psycopg.connect(cfg.conninfo, connect_timeout=3) as conn:
            return conn.execute("SELECT 1").fetchone() == (1,)

    wait_until(arango, "ArangoDB", timeout)
    wait_until(minio, "MinIO", timeout)
    wait_until(pg, "Postgres", timeout)


# ------------------------------------------------------------------------------ migrations

def migration_chain() -> List[Path]:
    """The migration files in revision order (down_revision chain), without importing alembic."""
    info: Dict[str, Dict[str, Any]] = {}
    for path in MIGRATIONS.glob("*.py"):
        text = path.read_text()
        rev = re.search(r'^revision\s*=\s*"([^"]+)"', text, re.M)
        down = re.search(r'^down_revision\s*=\s*(?:"([^"]+)"|None)', text, re.M)
        if rev:
            info[rev.group(1)] = {"path": path, "down": down.group(1) if down else None}
    by_down = {v["down"]: k for k, v in info.items()}
    chain: List[Path] = []
    cur = by_down.get(None)
    while cur is not None:
        chain.append(info[cur]["path"])
        cur = by_down.get(cur)
    if len(chain) != len(info):
        raise RuntimeError(f"migration chain is not linear: {len(chain)} of {len(info)} revisions")
    return chain


def head_revision() -> str:
    return re.search(r'^revision\s*=\s*"([^"]+)"', migration_chain()[-1].read_text(), re.M).group(1)


def _migration_env(cfg: Cfg) -> Dict[str, str]:
    return {"POSTGRES_DATABASE_HOST": cfg.pg_host, "POSTGRES_DATABASE_PORT": str(cfg.pg_port),
            "POSTGRES_DATABASE_NAME": cfg.pg_db, "POSTGRES_DATABASE_USERNAME": cfg.pg_user,
            "POSTGRES_DATABASE_PASSWORD": cfg.pg_password, "POSTGRES_PASSWORD": cfg.pg_password,
            "ARANGO_PASSWORD": "unused", "MINIO_ACCESS_KEY": "unused", "MINIO_SECRET_KEY": "unused"}


def _alembic_local(cfg: Cfg) -> Optional[str]:
    if importlib.util.find_spec("alembic") is None or importlib.util.find_spec("sqlalchemy") is None:
        return None
    env = dict(os.environ, **_migration_env(cfg), PYTHONPATH=str(REPO))
    done = subprocess.run([sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), "upgrade", "head"],
                          cwd=REPO, env=env, capture_output=True, text=True, timeout=600)
    if done.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed:\n{done.stdout}\n{done.stderr}")
    return "alembic (local)"


def _alembic_docker(cfg: Cfg) -> Optional[str]:
    probe = subprocess.run(["docker", "image", "inspect", cfg.alembic_image],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        return None
    args = ["docker", "run", "--rm", "--network", "host", "-v", f"{REPO}:/src:ro", "-w", "/src",
            "-e", "PYTHONPATH=/src", "-e", "PYTHONDONTWRITEBYTECODE=1"]
    for k, v in _migration_env(cfg).items():
        args += ["-e", f"{k}={v}"]
    args += [cfg.alembic_image, "alembic", "-c", "packages/api/alembic.ini", "upgrade", "head"]
    done = subprocess.run(args, capture_output=True, text=True, timeout=600)
    if done.returncode != 0:
        raise RuntimeError(f"alembic upgrade head ({cfg.alembic_image}) failed:\n"
                           f"{done.stdout}\n{done.stderr}")
    return f"alembic (docker {cfg.alembic_image})"


def _alembic_sql_replay(cfg: Cfg) -> str:
    """No alembic anywhere: run every revision's `upgrade()` against a stand-in `op` that
    collects its `op.execute` SQL, each revision in its own transaction with the version bump
    (what `transaction_per_migration` does). Only raw-SQL migrations work, which is the rule of
    packages/api/alembic.ini. A revision's Python data step (maps M1 classifies existing
    `mapobjectv1` rows through `op.get_bind()`) is skipped, as in alembic's offline `--sql` mode
    (`get_context().as_sql`): the harness database has no rows to classify."""
    import types
    import psycopg

    collected: List[str] = []
    fake_op = types.SimpleNamespace(
        execute=lambda sql, *a, **k: collected.append(str(sql)),
        get_context=lambda: types.SimpleNamespace(as_sql=True))
    fake = types.ModuleType("alembic")
    fake.op = fake_op
    saved = sys.modules.get("alembic")
    sys.modules["alembic"] = fake
    try:
        with psycopg.connect(cfg.conninfo, autocommit=True) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS alembic_version "
                         "(version_num varchar(255) NOT NULL PRIMARY KEY)")
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            current = row[0] if row else None
            started = current is None
            for path in migration_chain():
                spec = importlib.util.spec_from_file_location(f"_sg_e2e_{path.stem}", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if not started:
                    started = mod.revision == current
                    continue
                collected.clear()
                mod.upgrade()
                with conn.transaction():
                    for sql in collected:
                        conn.execute(sql)
                    conn.execute("DELETE FROM alembic_version")
                    conn.execute("INSERT INTO alembic_version VALUES (%s)", (mod.revision,))
    finally:
        if saved is None:
            sys.modules.pop("alembic", None)
        else:
            sys.modules["alembic"] = saved
    return "raw SQL replay of the revisions"


def widen_version_table(cfg: Cfg) -> None:
    """Create alembic's version table with a 255-char column, as alembic itself would create it
    with 32. Needed only because a shipped revision id is longer than 32 characters (see
    `long_revision_ids`): without it `upgrade head` cannot finish on ANY database, and nothing
    after the migration step could be exercised. The defect itself is asserted separately."""
    import psycopg
    with psycopg.connect(cfg.conninfo, autocommit=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(255) NOT NULL, "
                     "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))")


def long_revision_ids(limit: int = 32) -> List[str]:
    """Revision ids that do not fit alembic's `alembic_version.version_num varchar(32)`."""
    out = []
    for path in migration_chain():
        rev = re.search(r'^revision\s*=\s*"([^"]+)"', path.read_text(), re.M).group(1)
        if len(rev) > limit:
            out.append(rev)
    return out


def migrate(cfg: Cfg, mode: Optional[str] = None) -> str:
    """`alembic upgrade head` on the stack's Postgres: local alembic if importable, else the
    docker image `SG_E2E_ALEMBIC_IMAGE`, else the SQL of the revisions. `mode` (or
    SG_E2E_MIGRATE) = local | docker | sql forces one. Returns what ran."""
    mode = mode or os.environ.get("SG_E2E_MIGRATE") or "auto"
    order = {"auto": (_alembic_local, _alembic_docker, _alembic_sql_replay),
             "local": (_alembic_local,), "docker": (_alembic_docker,),
             "sql": (_alembic_sql_replay,)}[mode]
    for step in order:
        out = step(cfg)
        if out:
            return out
    raise RuntimeError(f"no migration method available for mode {mode!r}")

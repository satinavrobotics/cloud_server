"""Structural checks for the Alembic setup (v2 WP1.5): raw SQL, date-prefixed ids, linear
history, and a stable advisory-lock key for the API entrypoint. No database needed."""
import re
from pathlib import Path

import pytest

alembic = pytest.importorskip("alembic")
from alembic.config import Config  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402

from packages.api.entrypoint import (  # noqa: E402
    ALEMBIC_INI, MIGRATION_LOCK_KEY, advisory_lock_key)

pytestmark = pytest.mark.unit

REV_ID = re.compile(r"^\d{8}_\d{2}_[a-z0-9_]+$")
BASELINE = "20260924_00_baseline"


@pytest.fixture(scope="module")
def script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))


def test_lock_key_is_stable():
    # Changing this breaks mutual exclusion between old and new API images during a rollout.
    assert MIGRATION_LOCK_KEY == advisory_lock_key("migrations") == -3058229681751119483
    assert -(2 ** 63) <= MIGRATION_LOCK_KEY < 2 ** 63


def test_single_linear_history_from_baseline(script):
    assert len(script.get_heads()) == 1
    revs = list(script.walk_revisions())  # head -> base
    assert revs[-1].revision == BASELINE
    assert revs[-1].down_revision is None
    for newer, older in zip(revs, revs[1:]):
        assert newer.down_revision == older.revision


def test_revision_ids_are_date_prefixed_and_fit_version_table(script):
    for rev in script.walk_revisions():
        assert REV_ID.match(rev.revision), rev.revision
        assert len(rev.revision) <= 32, rev.revision
        assert Path(rev.path).stem == rev.revision


def test_migrations_are_raw_sql(script):
    for rev in script.walk_revisions():
        src = Path(rev.path).read_text()
        assert "sqlalchemy" not in src, rev.path
        assert not re.search(r"op\.(create|drop|add|alter)_", src), rev.path


def _load_revision(script, rev_id):
    import importlib.util
    path = script.get_revision(rev_id).path
    spec = importlib.util.spec_from_file_location(rev_id, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_phase0_core_constants(script):
    from cloud_common.objects.mission import MissionStateV1
    from packages.database.postgres import DB_INIT_LOCK_KEY
    core = _load_revision(script, "20260924_01_phase0_core")
    # Serializes with initialize_database's mission_trajectory CREATE.
    assert core.DB_INIT_LOCK_KEY == DB_INIT_LOCK_KEY
    # Decided 2026-09-24: COMPLETED (MissionStateV1), not v2's SUCCEEDED.
    assert "COMPLETED" in core.RUN_TERMINAL_STATES and "SUCCEEDED" not in core.RUN_STATES
    assert core.RUN_ACTIVE_STATE == MissionStateV1.RUNNING.value
    codes = [row[0] for row in core.CAUSE_CODES]
    assert len(codes) == len(set(codes)) == 20
    assert "UNKNOWN" in codes
    for code, category, _title, _desc in core.CAUSE_CODES:
        assert category == code.split(".", 1)[0]
    assert core.EVENT_SEVERITIES == ("info", "warning", "error", "critical")
    assert core.EVENT_SOURCES == ("dispatch", "api")

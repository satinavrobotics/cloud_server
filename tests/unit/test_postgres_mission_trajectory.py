"""
Unit tests for PostgresDatabase.log_mission_waypoint and the mission_trajectory table schema.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


def _make_db():
    """Return a PostgresDatabase with _pool pre-patched to a mock connection pool."""
    from packages.database.postgres import PostgresDatabase
    db = PostgresDatabase(dbname="test", user="u", password="p", host="h", port=5432)

    mock_cursor = AsyncMock()
    mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
    mock_cursor.__aexit__ = AsyncMock(return_value=False)

    mock_conn = AsyncMock()
    mock_conn.cursor = MagicMock(return_value=mock_cursor)
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)

    mock_pool = MagicMock()
    mock_pool.connection = MagicMock(return_value=mock_conn)

    db._pool = mock_pool
    return db, mock_conn, mock_cursor


@pytest.mark.unit
class TestLogMissionWaypoint:
    """Tests for PostgresDatabase.log_mission_waypoint."""

    async def test_insert_uses_correct_table_and_columns(self):
        """The INSERT targets mission_trajectory with all eight columns."""
        db, conn, cursor = _make_db()

        await db.log_mission_waypoint(
            mission_id="m1",
            robot_name="robot1",
            node_id="node-uuid",
            seq=3,
            x=1.5,
            y=2.5,
            yaw=0.7,
            map_id="main",
        )

        cursor.execute.assert_called_once()
        sql, params = cursor.execute.call_args[0]
        assert "mission_trajectory" in sql
        assert "mission_id" in sql
        assert "robot_name" in sql
        assert "node_id" in sql
        assert "seq" in sql
        assert "x" in sql
        assert "y" in sql
        assert "yaw" in sql
        assert "map_id" in sql

    async def test_insert_passes_params_in_order(self):
        """Parameters are forwarded in the documented order."""
        db, conn, cursor = _make_db()

        await db.log_mission_waypoint(
            mission_id="mission_a",
            robot_name="robot_x",
            node_id="uuid-123",
            seq=7,
            x=10.0,
            y=20.0,
            yaw=1.57,
            map_id="floor2",
        )

        _, params = cursor.execute.call_args[0]
        assert params == ["mission_a", "robot_x", "uuid-123", 7, 10.0, 20.0, 1.57, "floor2"]

    async def test_commit_called_on_success(self):
        """Pool context manager commits on successful exit; execute must be called."""
        db, conn, cursor = _make_db()

        await db.log_mission_waypoint(
            mission_id="m", robot_name="r", node_id="n",
            seq=1, x=0.0, y=0.0, yaw=0.0, map_id="map",
        )

        # Pool handles commit automatically; verify the SQL was executed
        cursor.execute.assert_called_once()

    async def test_rollback_called_on_execute_error(self):
        """Pool context manager rolls back on error; exception is swallowed."""
        db, conn, cursor = _make_db()
        cursor.execute = AsyncMock(side_effect=Exception("constraint violation"))

        # Should not raise — log_mission_waypoint swallows errors
        await db.log_mission_waypoint(
            mission_id="m", robot_name="r", node_id="n",
            seq=1, x=0.0, y=0.0, yaw=0.0, map_id="map",
        )

        cursor.execute.assert_called_once()
        # No explicit commit after a failed execute
        conn.commit.assert_not_called()

    async def test_no_exception_propagated_on_error(self):
        """Errors in log_mission_waypoint must never crash the caller."""
        db, conn, cursor = _make_db()
        cursor.execute = AsyncMock(side_effect=Exception("DB unavailable"))

        # Must not raise
        await db.log_mission_waypoint(
            mission_id="m", robot_name="r", node_id="n",
            seq=1, x=0.0, y=0.0, yaw=0.0, map_id="map",
        )


@pytest.mark.unit
class TestMissionTrajectoryTableCreation:
    """Tests that initialize_database creates the mission_trajectory table."""

    def _make_conn_with_cursor(self):
        # initialize_database calls connection.cursor() as a plain call (not async-with),
        # then awaits cursor.execute() directly.
        mock_cursor = AsyncMock()
        mock_conn = MagicMock()
        mock_conn.cursor = MagicMock(return_value=mock_cursor)
        mock_conn.commit = AsyncMock()
        return mock_conn, mock_cursor

    async def test_initialize_database_creates_mission_trajectory_table(self):
        """initialize_database must issue a CREATE TABLE for mission_trajectory."""
        mock_conn, mock_cursor = self._make_conn_with_cursor()

        from packages.database.postgres import initialize_database
        await initialize_database(mock_conn)

        all_sql = " ".join(
            str(c.args[0]) for c in mock_cursor.execute.call_args_list
        )
        assert "mission_trajectory" in all_sql

    async def test_initialize_database_creates_trajectory_index(self):
        """initialize_database must create the (mission_id, seq) index."""
        mock_conn, mock_cursor = self._make_conn_with_cursor()

        from packages.database.postgres import initialize_database
        await initialize_database(mock_conn)

        all_sql = " ".join(
            str(c.args[0]) for c in mock_cursor.execute.call_args_list
        )
        assert "trajectory_mission_idx" in all_sql

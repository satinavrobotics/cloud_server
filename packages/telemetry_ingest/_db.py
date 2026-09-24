"""Shared helper: accept either a psycopg AsyncConnection or an AsyncConnectionPool."""

from typing import Any

class connection_scope:
    """`async with connection_scope(x) as conn`: x is a connection (used as is) or a pool."""

    def __init__(self, pool_or_conn: Any):
        self._target = pool_or_conn
        self._cm = None

    async def __aenter__(self):
        if hasattr(self._target, "cursor"):
            return self._target
        self._cm = self._target.connection()
        return await self._cm.__aenter__()

    async def __aexit__(self, *exc):
        if self._cm is not None:
            return await self._cm.__aexit__(*exc)
        return False

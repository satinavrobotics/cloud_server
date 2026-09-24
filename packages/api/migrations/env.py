"""Alembic environment for the Phase 0 schema.

Raw-SQL migrations only. There is no SQLAlchemy model metadata, and autogenerate is refused
outright: the *objectv1 tables are created at runtime by
packages/database/postgres.py::initialize_database, and TimescaleDB objects (hypertables,
chunks, continuous aggregates) would show up as noise in any diff.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import URL

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

cmd_opts = getattr(config, "cmd_opts", None)
if cmd_opts is not None and getattr(cmd_opts, "autogenerate", False):
    raise SystemExit(
        "alembic autogenerate is disabled for this project: write the migration as raw SQL "
        "(op.execute) with a date-prefixed --rev-id. See packages/api/alembic.ini.")

# No metadata on purpose (see module docstring).
target_metadata = None


def include_object(obj, name, type_, reflected, compare_to):
    """Keep the runtime-managed object tables (robotobjectv1, missionobjectv1, ...) out of
    anything Alembic compares. Only relevant to comparison features such as `alembic check`,
    since autogenerate is disabled above."""
    if type_ == "table" and name is not None and name.endswith("objectv1"):
        return False
    return True


def database_url() -> URL:
    # Imported here so `alembic --help`/`history` work without the service environment.
    from packages.config import (
        POSTGRES_DATABASE_HOST, POSTGRES_DATABASE_NAME, POSTGRES_DATABASE_PASSWORD,
        POSTGRES_DATABASE_PORT, POSTGRES_DATABASE_USERNAME)
    return URL.create(
        "postgresql+psycopg",
        username=POSTGRES_DATABASE_USERNAME,
        password=POSTGRES_DATABASE_PASSWORD,
        host=POSTGRES_DATABASE_HOST,
        port=POSTGRES_DATABASE_PORT,
        database=POSTGRES_DATABASE_NAME,
    )


_CONFIGURE_KW = dict(
    target_metadata=target_metadata,
    include_object=include_object,
    # One transaction per revision: a failing migration rolls back completely, together
    # with its alembic_version bump.
    transaction_per_migration=True,
)


def run_migrations_offline() -> None:
    """`alembic upgrade --sql`: render the SQL for review without connecting."""
    context.configure(url=database_url(), literal_binds=True, **_CONFIGURE_KW)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, **_CONFIGURE_KW)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

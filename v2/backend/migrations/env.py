"""Alembic environment shared by the startup runner and administration CLI."""
from alembic import context
from app.database import Base
from app import models  # noqa: F401

config = context.config
connection = config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Run migrations through scripts/manage-accounts.py migrate; no credentials belong in alembic.ini")
context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True,
                  render_as_batch=connection.dialect.name == "sqlite")
with context.begin_transaction():
    context.run_migrations()

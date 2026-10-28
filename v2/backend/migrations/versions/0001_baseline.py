"""Baseline the three original application tables without dropping legacy rows."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT

revision = "0001_baseline"
down_revision = None
branch_labels = depends_on = None


def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "characters" not in tables:
        op.create_table("characters", sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("slug", sa.String(64), nullable=False), sa.Column("name", sa.String(64), nullable=False),
            sa.Column("display_name", sa.String(64), nullable=False), sa.Column("version", sa.String(32), nullable=False),
            sa.Column("avatar", sa.String(255), nullable=False), sa.Column("greeting", sa.Text(), nullable=False),
            sa.Column("system_prompt", sa.Text(), nullable=False), sa.Column("personality", sa.Text(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), mysql_charset="utf8mb4")
        op.create_index("ix_characters_slug", "characters", ["slug"], unique=True)
    if "chat_sessions" not in tables:
        op.create_table("chat_sessions", sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("character_id", sa.Integer(), sa.ForeignKey("characters.id"), nullable=False),
            sa.Column("title", sa.String(120), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False), mysql_charset="utf8mb4")
    if "messages" not in tables:
        op.create_table("messages", sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("session_id", sa.String(32), sa.ForeignKey("chat_sessions.id"), nullable=False),
            sa.Column("role", sa.String(16), nullable=False), sa.Column("content", sa.Text().with_variant(LONGTEXT(), "mysql"), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False), mysql_charset="utf8mb4")
        op.create_index("ix_messages_session_id", "messages", ["session_id"])


def downgrade():
    raise RuntimeError("Refusing to erase legacy chat tables. Restore a verified backup instead.")

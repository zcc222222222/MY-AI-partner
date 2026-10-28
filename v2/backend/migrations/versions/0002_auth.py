"""Accounts and nullable ownership: old conversations remain unassigned."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT

revision = "0002_auth"
down_revision = "0001_baseline"
branch_labels = depends_on = None


def upgrade():
    op.create_table("users", sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(32), nullable=False, unique=True), sa.Column("display_name", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False), sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False), mysql_charset="utf8mb4")
    op.create_table("auth_sessions", sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("csrf_token", sa.String(64), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False), mysql_charset="utf8mb4")
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])
    op.create_table("auth_throttles", sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("window_start", sa.DateTime(), nullable=False), sa.Column("attempts", sa.Integer(), nullable=False), mysql_charset="utf8mb4")
    if op.get_bind().dialect.name == "sqlite":
        # SQLite accepts an inline nullable REFERENCES column without copying the
        # parent table and disturbing messages' foreign key.
        op.execute("ALTER TABLE chat_sessions ADD COLUMN owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL")
    else:
        op.add_column("chat_sessions", sa.Column("owner_id", sa.Integer(), nullable=True))
        op.create_foreign_key("fk_chat_sessions_owner_id_users", "chat_sessions", "users", ["owner_id"], ["id"], ondelete="SET NULL")
        op.alter_column("messages", "content", existing_type=sa.Text(), type_=LONGTEXT(), existing_nullable=False)
    op.create_index("ix_chat_sessions_owner_id", "chat_sessions", ["owner_id"])


def downgrade():
    # This drops credentials and ownership; the explicit CLI requires acknowledgement.
    op.drop_index("ix_chat_sessions_owner_id", table_name="chat_sessions")
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("fk_chat_sessions_owner_id_users", "chat_sessions", type_="foreignkey")
    op.drop_column("chat_sessions", "owner_id")
    op.drop_table("auth_throttles")
    op.drop_table("auth_sessions")
    op.drop_table("users")

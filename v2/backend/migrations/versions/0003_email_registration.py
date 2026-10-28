"""Verified email registration and persistent empty-chat state."""
from alembic import op
import sqlalchemy as sa

revision = "0003_email"
down_revision = "0002_auth"
branch_labels = depends_on = None


def upgrade():
    op.add_column("users", sa.Column("email", sa.String(254), nullable=True))
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(), nullable=True))
    op.create_index("uq_users_email", "users", ["email"], unique=True)
    op.add_column("chat_sessions", sa.Column("has_user_message", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("UPDATE chat_sessions SET has_user_message = 1 WHERE EXISTS (SELECT 1 FROM messages WHERE messages.session_id = chat_sessions.id AND messages.role = 'user')")
    op.create_table("email_verifications",
        sa.Column("email", sa.String(254), primary_key=True),
        sa.Column("nonce", sa.String(32), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("resend_after", sa.DateTime(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("delivered", sa.Boolean(), nullable=False), mysql_charset="utf8mb4")


def downgrade():
    op.drop_table("email_verifications")
    op.drop_column("chat_sessions", "has_user_message")
    op.drop_index("uq_users_email", table_name="users")
    op.drop_column("users", "email_verified_at")
    op.drop_column("users", "email")

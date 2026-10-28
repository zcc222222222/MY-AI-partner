"""Private, rebuildable session context; original messages are unchanged."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT

revision = "0004_context"
down_revision = "0003_email"
branch_labels = depends_on = None


def upgrade():
    op.create_table("chat_context_summaries",
        sa.Column("session_id", sa.String(32), sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("covered_through", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text().with_variant(LONGTEXT(), "mysql"), nullable=False),
        sa.Column("format_version", sa.String(32), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False), mysql_charset="utf8mb4")
    op.create_index("ix_chat_context_summaries_owner_id", "chat_context_summaries", ["owner_id"])


def downgrade():
    op.drop_table("chat_context_summaries")

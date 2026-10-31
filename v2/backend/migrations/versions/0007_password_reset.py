"""One-use password reset challenges and password change timestamp."""
from alembic import op
import sqlalchemy as sa

revision = "0007_password_reset"
down_revision = "0006_usage_turns"
branch_labels = depends_on = None


def upgrade():
    op.add_column("users", sa.Column("password_changed_at", sa.DateTime(), nullable=True))
    op.create_table("password_reset_challenges",
        sa.Column("email", sa.String(254), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("credential_digest", sa.String(64), nullable=True),
        sa.Column("nonce", sa.String(32), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("resend_after", sa.DateTime(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("delivered", sa.Boolean(), nullable=False), mysql_charset="utf8mb4")


def downgrade():
    op.drop_table("password_reset_challenges")
    op.drop_column("users", "password_changed_at")

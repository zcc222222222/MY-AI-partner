"""Expiring isolated guest identities; shared IP quota uses the existing ledger."""
from alembic import op
import sqlalchemy as sa

revision = "0008_guest_access"
down_revision = "0007_password_reset"
branch_labels = depends_on = None


def upgrade():
    op.add_column("users", sa.Column("is_guest", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("guest_expires_at", sa.DateTime(), nullable=True))
    op.add_column("users", sa.Column("guest_ip_hash", sa.String(64), nullable=True))
    op.create_index("ix_users_guest_ip_hash", "users", ["guest_ip_hash"])


def downgrade():
    op.drop_index("ix_users_guest_ip_hash", table_name="users")
    op.drop_column("users", "guest_ip_hash")
    op.drop_column("users", "guest_expires_at")
    op.drop_column("users", "is_guest")

"""Associate failed requests with their user message without changing chat history."""
from alembic import op
import sqlalchemy as sa

revision='0006_usage_turns'
down_revision='0005_usage'
branch_labels=depends_on=None


def upgrade():
    if 'user_message_id' not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns('generation_requests')}:
        op.add_column('generation_requests',sa.Column('user_message_id',sa.Integer(),nullable=True))


def downgrade():
    op.drop_column('generation_requests','user_message_id')

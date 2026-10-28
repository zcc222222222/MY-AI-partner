"""Durable request admission and conservative provider usage estimates."""
from alembic import op
from sqlalchemy import Table, MetaData, Column, String, Integer, BigInteger, DateTime, ForeignKey, Text, UniqueConstraint

revision = '0005_usage'
down_revision = '0004_context'
branch_labels = depends_on = None


def upgrade():
    op.create_table('quota_buckets',Column('key',String(100),primary_key=True),
        Column('questions',Integer,nullable=False),Column('spent_micros',BigInteger,nullable=False),
        Column('reserved_micros',BigInteger,nullable=False),Column('updated_at',DateTime,nullable=False),mysql_charset='utf8mb4')
    op.execute("INSERT INTO quota_buckets (`key`,questions,spent_micros,reserved_micros,updated_at) VALUES ('system-lock',0,0,0,CURRENT_TIMESTAMP)")
    op.create_table('generation_requests',Column('id',String(32),primary_key=True),
        Column('owner_id',Integer,ForeignKey('users.id'),nullable=False),Column('session_id',String(32),nullable=False),
        Column('client_request_id',String(64),nullable=False),Column('input_digest',String(64),nullable=False),
        Column('status',String(24),nullable=False),Column('question_buckets',Text,nullable=False),
        Column('created_at',DateTime,nullable=False),Column('lease_until',DateTime,nullable=False),
        Column('finished_at',DateTime),Column('assistant_message_id',Integer),Column('error_code',String(32)),
        UniqueConstraint('owner_id','client_request_id',name='uq_generation_owner_client'),mysql_charset='utf8mb4')
    for field in ('owner_id','session_id','status','created_at'):
        op.create_index('ix_generation_requests_'+field,'generation_requests',[field])
    op.create_table('llm_usage',Column('id',String(32),primary_key=True),
        Column('request_id',String(32),ForeignKey('generation_requests.id'),nullable=False),
        Column('purpose',String(24),nullable=False),Column('model',String(100),nullable=False),
        Column('status',String(24),nullable=False),Column('buckets',Text,nullable=False),
        Column('reserved_micros',BigInteger,nullable=False),Column('charged_micros',BigInteger,nullable=False),
        Column('prompt_tokens',Integer),Column('completion_tokens',Integer),Column('cache_hit_tokens',Integer),
        Column('price_version',String(100),nullable=False),Column('input_rate',BigInteger,nullable=False),
        Column('cached_rate',BigInteger,nullable=False),Column('output_rate',BigInteger,nullable=False),
        Column('created_at',DateTime,nullable=False),mysql_charset='utf8mb4')
    op.create_index('ix_llm_usage_request_id','llm_usage',['request_id'])


def downgrade():
    op.drop_table('llm_usage')
    op.drop_table('generation_requests')
    op.drop_table('quota_buckets')

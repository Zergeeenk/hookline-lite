"""credenciales de Instagram por inicio de sesión unificado

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('credenciales_ig', schema=None) as b:
        b.add_column(sa.Column('via', sa.String(length=20), nullable=False, server_default='instagram'))


def downgrade():
    with op.batch_alter_table('credenciales_ig', schema=None) as b:
        b.drop_column('via')

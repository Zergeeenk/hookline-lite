"""cuentas atadas a modelos de Pyro

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('cuentas', schema=None) as b:
        b.add_column(sa.Column('pyro_modelo_id', sa.Integer(), nullable=True))
        b.add_column(sa.Column('pyro_modelo', sa.String(length=200), nullable=False, server_default=''))
        b.create_index(b.f('ix_cuentas_pyro_modelo_id'), ['pyro_modelo_id'], unique=False)


def downgrade():
    with op.batch_alter_table('cuentas', schema=None) as b:
        b.drop_index(b.f('ix_cuentas_pyro_modelo_id'))
        b.drop_column('pyro_modelo')
        b.drop_column('pyro_modelo_id')

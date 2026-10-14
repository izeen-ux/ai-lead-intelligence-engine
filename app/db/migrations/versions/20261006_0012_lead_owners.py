"""lead owners (the team member whose pipeline run produced a lead)

Additive: one new table, one row per company. The first owner is kept: later runs never
re-assign a company that already has one.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-06 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0012'
down_revision: str | None = '0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'lead_owners',
        sa.Column('company_id', sa.Uuid(), nullable=False),
        sa.Column('owner', sa.String(length=120), nullable=False),
        sa.Column('pipeline_run_id', sa.Uuid(), nullable=True),
        sa.Column(
            'assigned_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ['company_id'],
            ['companies.id'],
            name=op.f('fk_lead_owners_company_id_companies'),
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['pipeline_run_id'],
            ['pipeline_runs.id'],
            name=op.f('fk_lead_owners_pipeline_run_id_pipeline_runs'),
            ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('company_id', name=op.f('pk_lead_owners')),
    )
    op.create_index(op.f('ix_lead_owners_owner'), 'lead_owners', ['owner'])


def downgrade() -> None:
    op.drop_index(op.f('ix_lead_owners_owner'), table_name='lead_owners')
    op.drop_table('lead_owners')

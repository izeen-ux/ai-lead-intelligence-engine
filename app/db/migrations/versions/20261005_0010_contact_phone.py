"""contact phone (optional capability) on enrichment contacts and cache

Additive only: two nullable/defaulted columns per table. Existing rows become
``phone = NULL, phone_status = 'not_requested'`` - true for every contact created before the
phone capability existed. The append-only trigger on ``enrichment_contacts`` guards UPDATE and
DELETE of rows, not ALTER TABLE.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-05 18:20:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0010'
down_revision: str | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ('enrichment_contacts', 'enrichment_cache')
_STATUSES = "phone_status IN ('not_requested','found','not_found','budget_exhausted')"


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column('phone', sa.String(length=40), nullable=True))
        op.add_column(
            table,
            sa.Column(
                'phone_status', sa.String(length=20), server_default='not_requested', nullable=False
            ),
        )
        op.create_check_constraint(op.f(f'ck_{table}_phone_status_valid'), table, _STATUSES)
        op.create_check_constraint(
            op.f(f'ck_{table}_phone_matches_status'),
            table,
            "(phone IS NOT NULL) = (phone_status = 'found')",
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_constraint(op.f(f'ck_{table}_phone_matches_status'), table, type_='check')
        op.drop_constraint(op.f(f'ck_{table}_phone_status_valid'), table, type_='check')
        op.drop_column(table, 'phone_status')
        op.drop_column(table, 'phone')

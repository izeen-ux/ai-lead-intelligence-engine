"""prescreen decisions (scrape / skip before paid website intelligence)

Additive: one new insert-only table. Existing companies have no row, so they are simply
pre-screened by the next pipeline run.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-06 09:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0011'
down_revision: str | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'prescreen_decisions',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('company_id', sa.Uuid(), nullable=False),
        sa.Column('decision', sa.String(length=16), nullable=False),
        sa.Column(
            'reasons',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column('laya_status', sa.String(length=16), nullable=False),
        sa.Column('laya_fit', sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column('policy_version', sa.String(length=32), nullable=False),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False
        ),
        sa.CheckConstraint(
            "decision IN ('scrape','skip')", name=op.f('ck_prescreen_decisions_decision_valid')
        ),
        sa.CheckConstraint(
            "laya_status IN ('ok','skipped','error')",
            name=op.f('ck_prescreen_decisions_laya_status_valid'),
        ),
        sa.ForeignKeyConstraint(
            ['company_id'],
            ['companies.id'],
            name=op.f('fk_prescreen_decisions_company_id_companies'),
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_prescreen_decisions')),
    )
    op.create_index(
        op.f('ix_prescreen_decisions_company_id'), 'prescreen_decisions', ['company_id']
    )
    op.execute(
        """
        CREATE FUNCTION prescreen_decisions_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'prescreen_decisions is append-only (id=%)', OLD.id
                USING ERRCODE = 'check_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_prescreen_decisions_immutable BEFORE UPDATE ON prescreen_decisions "
        "FOR EACH ROW EXECUTE FUNCTION prescreen_decisions_immutable()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_prescreen_decisions_immutable ON prescreen_decisions")
    op.execute("DROP FUNCTION IF EXISTS prescreen_decisions_immutable()")
    op.drop_index(op.f('ix_prescreen_decisions_company_id'), table_name='prescreen_decisions')
    op.drop_table('prescreen_decisions')

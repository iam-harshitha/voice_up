"""policy auto dial

Opt-in automatic voice calls: approval_policies.auto_dial (NOT NULL, default false).
When true, creating a request under the policy schedules the approvers' calls in the
same transaction. Existing policies get false, so behaviour is unchanged until a policy
is explicitly switched on. Additive; downgrade drops the column.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-29 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'approval_policies',
        sa.Column('auto_dial', sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column('approval_policies', 'auto_dial')

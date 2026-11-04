"""voice fallback range

approval_policies.voice_fallback_after_seconds: 5-600 seconds (was 10-3600), for
'text_then_voice' policies (text first, then the call). No policy used the value before
(the channel was refused by the API), so any value above 600 is clamped to 600.

Downgrade refuses while a policy uses a value below 10 (the old minimum).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-04 15:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME = 'ck_approval_policies_voice_fallback_range'


def _replace_check(condition: str) -> None:
    op.drop_constraint(op.f(NAME), 'approval_policies', type_='check')
    op.create_check_constraint(op.f(NAME), 'approval_policies', condition)


def upgrade() -> None:
    op.execute(
        'UPDATE approval_policies SET voice_fallback_after_seconds = 600 '
        'WHERE voice_fallback_after_seconds > 600'
    )
    _replace_check('voice_fallback_after_seconds BETWEEN 5 AND 600')


GUARD = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM approval_policies WHERE voice_fallback_after_seconds < 10) THEN
        RAISE EXCEPTION 'refusing to downgrade 0008: a policy uses a fallback below 10 s';
    END IF;
END;
$$;
"""


def downgrade() -> None:
    op.execute(GUARD)
    _replace_check('voice_fallback_after_seconds BETWEEN 10 AND 3600')

"""text approvals channel

Telegram text approvals (additive; off by default):
- approval_policies.approval_channel ('voice' | 'text_then_voice' | 'text', default
  'voice') and voice_fallback_after_seconds (default 60). Existing policies get 'voice',
  so behaviour is unchanged until a policy is explicitly switched.
- approver_channel_links: verified Telegram accounts per approver (one-time pairing).
- text_prompts: one approval message per approver per request, with hashed single-use
  button tokens.
- audit_events may describe an approver (approver_id), for channel-link events.

Downgrade refuses while text data exists (prompts, approver audit events, or a policy
not on 'voice'): dropping the column would silently turn a text-only policy back into
phone calls, and audit history is never discarded.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-04 12:12:42.850943
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_='check')
    op.create_check_constraint(op.f(name), table, condition)


def upgrade() -> None:
    op.create_table('approver_channel_links',
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('approver_id', sa.Uuid(), nullable=False),
    sa.Column('channel', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('pairing_code_hash', sa.String(length=64), nullable=True),
    sa.Column('pairing_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('external_user_id', sa.String(length=32), nullable=True),
    sa.Column('external_chat_id', sa.String(length=32), nullable=True),
    sa.Column('created_by_api_key_id', sa.Uuid(), nullable=True),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status = 'revoked') = (revoked_at IS NOT NULL)", name=op.f('ck_approver_channel_links_revoked_at_matches')),
    sa.CheckConstraint("channel IN ('telegram')", name=op.f('ck_approver_channel_links_channel_valid')),
    sa.CheckConstraint("status <> 'pending' OR (pairing_code_hash IS NOT NULL AND pairing_expires_at IS NOT NULL)", name=op.f('ck_approver_channel_links_pending_has_code')),
    sa.CheckConstraint("status <> 'verified' OR (external_user_id IS NOT NULL AND external_chat_id IS NOT NULL AND verified_at IS NOT NULL)", name=op.f('ck_approver_channel_links_verified_has_account')),
    sa.CheckConstraint("status IN ('pending', 'verified', 'revoked')", name=op.f('ck_approver_channel_links_status_valid')),
    sa.ForeignKeyConstraint(['approver_id', 'organization_id'], ['approvers.id', 'approvers.organization_id'], name=op.f('fk_approver_channel_links_approver_id_organization_id_approvers'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by_api_key_id'], ['api_keys.id'], name=op.f('fk_approver_channel_links_created_by_api_key_id_api_keys'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_approver_channel_links_organization_id_organizations'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_approver_channel_links')),
    sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_approver_channel_links_id_organization_id')),
    sa.UniqueConstraint('pairing_code_hash', name=op.f('uq_approver_channel_links_pairing_code_hash'))
    )
    op.create_index(op.f('ix_approver_channel_links_approver_id'), 'approver_channel_links', ['approver_id'], unique=False)
    op.create_index(op.f('ix_approver_channel_links_organization_id'), 'approver_channel_links', ['organization_id'], unique=False)
    op.create_index('uq_approver_channel_links_verified_per_approver', 'approver_channel_links', ['approver_id', 'channel'], unique=True, postgresql_where=sa.text("status = 'verified'"))
    op.create_index('uq_approver_channel_links_verified_per_user', 'approver_channel_links', ['channel', 'external_user_id'], unique=True, postgresql_where=sa.text("status = 'verified'"))
    op.create_table('text_prompts',
    sa.Column('organization_id', sa.Uuid(), nullable=False),
    sa.Column('request_id', sa.Uuid(), nullable=False),
    sa.Column('approver_id', sa.Uuid(), nullable=False),
    sa.Column('link_id', sa.Uuid(), nullable=False),
    sa.Column('provider', sa.String(length=16), nullable=False),
    sa.Column('reason', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=True),
    sa.Column('confirm_token_hash', sa.String(length=64), nullable=True),
    sa.Column('decision', sa.String(length=16), nullable=True),
    sa.Column('outcome', sa.String(length=48), nullable=True),
    sa.Column('vote_id', sa.Uuid(), nullable=True),
    sa.Column('chat_id', sa.String(length=32), nullable=True),
    sa.Column('provider_message_id', sa.String(length=32), nullable=True),
    sa.Column('attempts', sa.SmallInteger(), server_default='0', nullable=False),
    sa.Column('run_after', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.String(length=64), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('answered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status = 'answered') = (answered_at IS NOT NULL)", name=op.f('ck_text_prompts_answered_at_matches')),
    sa.CheckConstraint("(status = 'sending') = (lease_expires_at IS NOT NULL)", name=op.f('ck_text_prompts_lease_matches_status')),
    sa.CheckConstraint("decision IS NULL OR decision IN ('approve', 'reject')", name=op.f('ck_text_prompts_decision_valid')),
    sa.CheckConstraint("provider IN ('telegram', 'mock')", name=op.f('ck_text_prompts_provider_valid')),
    sa.CheckConstraint("reason IN ('auto_dial', 'escalation', 'reapproval')", name=op.f('ck_text_prompts_reason_valid')),
    sa.CheckConstraint("status <> 'confirming' OR confirm_token_hash IS NOT NULL", name=op.f('ck_text_prompts_confirming_has_token')),
    sa.CheckConstraint("status IN ('queued', 'sending', 'sent', 'confirming', 'answered', 'closed', 'failed')", name=op.f('ck_text_prompts_status_valid')),
    sa.CheckConstraint("status NOT IN ('sent', 'confirming') OR (token_hash IS NOT NULL AND provider_message_id IS NOT NULL AND chat_id IS NOT NULL)", name=op.f('ck_text_prompts_sent_has_message')),
    sa.CheckConstraint('attempts >= 0', name=op.f('ck_text_prompts_attempts_non_negative')),
    sa.ForeignKeyConstraint(['link_id', 'organization_id'], ['approver_channel_links.id', 'approver_channel_links.organization_id'], name=op.f('fk_text_prompts_link_id_organization_id_approver_channel_links'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_text_prompts_organization_id_organizations'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['request_id', 'approver_id'], ['request_approvers.request_id', 'request_approvers.approver_id'], name=op.f('fk_text_prompts_request_id_approver_id_request_approvers'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['request_id', 'organization_id'], ['approval_requests.id', 'approval_requests.organization_id'], name=op.f('fk_text_prompts_request_id_organization_id_approval_requests'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['vote_id'], ['approval_votes.id'], name=op.f('fk_text_prompts_vote_id_approval_votes'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_text_prompts')),
    sa.UniqueConstraint('confirm_token_hash', name=op.f('uq_text_prompts_confirm_token_hash')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_text_prompts_token_hash'))
    )
    op.create_index(op.f('ix_text_prompts_link_id'), 'text_prompts', ['link_id'], unique=False)
    op.create_index(op.f('ix_text_prompts_organization_id'), 'text_prompts', ['organization_id'], unique=False)
    op.create_index(op.f('ix_text_prompts_request_id'), 'text_prompts', ['request_id'], unique=False)
    op.create_index('ix_text_prompts_status_run_after', 'text_prompts', ['status', 'run_after'], unique=False)
    op.create_index('uq_text_prompts_open_per_approver', 'text_prompts', ['request_id', 'approver_id'], unique=True, postgresql_where=sa.text("status IN ('queued', 'sending', 'sent', 'confirming')"))
    op.add_column('approval_policies', sa.Column('approval_channel', sa.String(length=16), server_default='voice', nullable=False))
    op.add_column('approval_policies', sa.Column('voice_fallback_after_seconds', sa.Integer(), server_default='60', nullable=False))
    op.add_column('audit_events', sa.Column('approver_id', sa.Uuid(), nullable=True))
    op.create_index('ix_audit_events_approver_id_sequence', 'audit_events', ['approver_id', 'sequence'], unique=False)
    op.create_foreign_key(op.f('fk_audit_events_approver_id_organization_id_approvers'), 'audit_events', 'approvers', ['approver_id', 'organization_id'], ['id', 'organization_id'], ondelete='RESTRICT')
    # Hand-written: alembic check does not see CHECK constraints.
    op.create_check_constraint(op.f('ck_approval_policies_approval_channel_valid'), 'approval_policies', "approval_channel IN ('voice', 'text_then_voice', 'text')")
    op.create_check_constraint(op.f('ck_approval_policies_voice_fallback_range'), 'approval_policies', 'voice_fallback_after_seconds BETWEEN 10 AND 3600')
    _replace_check('audit_events', 'ck_audit_events_has_subject', 'request_id IS NOT NULL OR policy_id IS NOT NULL OR approver_id IS NOT NULL')


GUARD = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM text_prompts)
       OR EXISTS (SELECT 1 FROM audit_events WHERE approver_id IS NOT NULL)
       OR EXISTS (SELECT 1 FROM approval_policies WHERE approval_channel <> 'voice')
       OR EXISTS (SELECT 1 FROM approval_requests WHERE policy_snapshot ? 'approval_channel') THEN
        RAISE EXCEPTION 'refusing to downgrade 0007: text-approval data exists';
    END IF;
END;
$$;
"""


def downgrade() -> None:
    op.execute(GUARD)
    _replace_check('audit_events', 'ck_audit_events_has_subject', 'request_id IS NOT NULL OR policy_id IS NOT NULL')
    op.drop_constraint(op.f('ck_approval_policies_voice_fallback_range'), 'approval_policies', type_='check')
    op.drop_constraint(op.f('ck_approval_policies_approval_channel_valid'), 'approval_policies', type_='check')
    op.drop_constraint(op.f('fk_audit_events_approver_id_organization_id_approvers'), 'audit_events', type_='foreignkey')
    op.drop_index('ix_audit_events_approver_id_sequence', table_name='audit_events')
    op.drop_column('audit_events', 'approver_id')
    op.drop_column('approval_policies', 'voice_fallback_after_seconds')
    op.drop_column('approval_policies', 'approval_channel')
    op.drop_index('uq_text_prompts_open_per_approver', table_name='text_prompts', postgresql_where=sa.text("status IN ('queued', 'sending', 'sent', 'confirming')"))
    op.drop_index('ix_text_prompts_status_run_after', table_name='text_prompts')
    op.drop_index(op.f('ix_text_prompts_request_id'), table_name='text_prompts')
    op.drop_index(op.f('ix_text_prompts_organization_id'), table_name='text_prompts')
    op.drop_index(op.f('ix_text_prompts_link_id'), table_name='text_prompts')
    op.drop_table('text_prompts')
    op.drop_index('uq_approver_channel_links_verified_per_user', table_name='approver_channel_links', postgresql_where=sa.text("status = 'verified'"))
    op.drop_index('uq_approver_channel_links_verified_per_approver', table_name='approver_channel_links', postgresql_where=sa.text("status = 'verified'"))
    op.drop_index(op.f('ix_approver_channel_links_organization_id'), table_name='approver_channel_links')
    op.drop_index(op.f('ix_approver_channel_links_approver_id'), table_name='approver_channel_links')
    op.drop_table('approver_channel_links')

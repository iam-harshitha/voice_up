"""consensus conditions escalation deadlines

Chunk 3 schema:
- policies: approval_mode, veto_mode, decision deadline, escalation timing/limit;
  policy_approvers gain role (primary/backup) and can_veto.
- approval_requests: 'expired' status and a snapshotted expires_at.
- request_approvers: normalised, immutable copy of who may decide each request
  (backfilled from existing snapshots). Votes reference it and gain seat_approver_id
  with UNIQUE(request_id, seat_approver_id).
- escalation_steps (immutable) and approval_conditions (frozen once closed).
- audit_events may describe a policy (policy_id) instead of a request.
- voice_jobs carries request-level jobs: expire_request and escalate_request.

Downgrade is supported only while no Chunk 3 data exists (it refuses otherwise rather
than silently discarding authorization history).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-24 17:52:44.425827
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMMUTABLE_FUNCTION = """
CREATE FUNCTION approval_line_immutable_row() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% rows are immutable; % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;
"""

CONDITION_FREEZE_FUNCTION = """
CREATE FUNCTION approval_conditions_freeze_closed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'approval_conditions rows cannot be deleted'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.status <> 'open' THEN
        RAISE EXCEPTION 'approval_conditions row is closed and cannot change'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.request_id IS DISTINCT FROM OLD.request_id
       OR NEW.approver_id IS DISTINCT FROM OLD.approver_id
       OR NEW.seat_approver_id IS DISTINCT FROM OLD.seat_approver_id
       OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
       OR NEW.description IS DISTINCT FROM OLD.description THEN
        RAISE EXCEPTION 'approval_conditions binding and description are immutable'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;
"""


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, condition)


def upgrade() -> None:
    op.execute(IMMUTABLE_FUNCTION)
    op.execute(CONDITION_FREEZE_FUNCTION)

    # --- policies ----------------------------------------------------------------------
    op.add_column("approval_policies", sa.Column("approval_mode", sa.String(length=16), server_default="unanimous", nullable=False))
    op.add_column("approval_policies", sa.Column("veto_mode", sa.String(length=16), server_default="any_rejection", nullable=False))
    op.add_column("approval_policies", sa.Column("decision_deadline_seconds", sa.Integer(), nullable=True))
    op.add_column("approval_policies", sa.Column("escalation_after_seconds", sa.Integer(), nullable=True))
    op.add_column("approval_policies", sa.Column("max_escalations", sa.SmallInteger(), server_default="0", nullable=False))
    for name, condition in (
        ("ck_approval_policies_approval_mode_valid", "approval_mode IN ('unanimous', 'quorum')"),
        ("ck_approval_policies_veto_mode_valid", "veto_mode IN ('any_rejection', 'designated', 'none')"),
        ("ck_approval_policies_unanimous_uses_any_rejection", "approval_mode = 'quorum' OR veto_mode = 'any_rejection'"),
        ("ck_approval_policies_deadline_range", "decision_deadline_seconds IS NULL OR decision_deadline_seconds BETWEEN 60 AND 2592000"),
        ("ck_approval_policies_escalation_configured_consistently", "(escalation_after_seconds IS NULL) = (max_escalations = 0)"),
        ("ck_approval_policies_escalation_after_range", "escalation_after_seconds IS NULL OR escalation_after_seconds BETWEEN 60 AND 604800"),
        ("ck_approval_policies_max_escalations_range", "max_escalations BETWEEN 0 AND 3"),
        ("ck_approval_policies_escalation_before_deadline", "escalation_after_seconds IS NULL OR decision_deadline_seconds IS NULL OR escalation_after_seconds < decision_deadline_seconds"),
    ):
        op.create_check_constraint(op.f(name), "approval_policies", condition)

    op.add_column("policy_approvers", sa.Column("role", sa.String(length=16), server_default="primary", nullable=False))
    op.add_column("policy_approvers", sa.Column("can_veto", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.create_check_constraint(op.f("ck_policy_approvers_role_valid"), "policy_approvers", "role IN ('primary', 'backup')")
    op.create_check_constraint(op.f("ck_policy_approvers_only_primaries_veto"), "policy_approvers", "role = 'primary' OR NOT can_veto")

    # --- requests ----------------------------------------------------------------------
    op.add_column("approval_requests", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_approval_requests_status_expires_at", "approval_requests", ["status", "expires_at"], unique=False)
    _replace_check("approval_requests", "ck_approval_requests_status_valid", "status IN ('pending', 'approved', 'rejected', 'expired')")

    op.create_table(
        "request_approvers",
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("approver_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("can_veto", sa.Boolean(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.CheckConstraint("role = 'primary' OR NOT can_veto", name=op.f("ck_request_approvers_only_primaries_veto")),
        sa.CheckConstraint("role IN ('primary', 'backup')", name=op.f("ck_request_approvers_role_valid")),
        sa.CheckConstraint("position >= 0", name=op.f("ck_request_approvers_position_non_negative")),
        sa.ForeignKeyConstraint(["approver_id", "organization_id"], ["approvers.id", "approvers.organization_id"], name=op.f("fk_request_approvers_approver_id_organization_id_approvers"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["request_id", "organization_id"], ["approval_requests.id", "approval_requests.organization_id"], name=op.f("fk_request_approvers_request_id_organization_id_approval_requests"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("request_id", "approver_id", name=op.f("pk_request_approvers")),
        sa.UniqueConstraint("request_id", "position", name=op.f("uq_request_approvers_request_id_position")),
    )
    op.create_index(op.f("ix_request_approvers_approver_id"), "request_approvers", ["approver_id"], unique=False)
    op.create_index(op.f("ix_request_approvers_organization_id"), "request_approvers", ["organization_id"], unique=False)
    # Backfill existing (v1) requests: their snapshot approver_ids are the primaries.
    op.execute(
        """
        INSERT INTO request_approvers
            (request_id, approver_id, organization_id, role, can_veto, position)
        SELECT r.id, a.value::uuid, r.organization_id, 'primary', false, a.ordinality - 1
        FROM approval_requests r
        CROSS JOIN LATERAL jsonb_array_elements_text(r.policy_snapshot -> 'approver_ids')
            WITH ORDINALITY AS a(value, ordinality)
        """
    )
    op.execute(
        "CREATE TRIGGER request_approvers_immutable BEFORE UPDATE OR DELETE ON request_approvers "
        "FOR EACH ROW EXECUTE FUNCTION approval_line_immutable_row()"
    )

    # --- votes: seat + snapshot membership ------------------------------------------------
    op.add_column("approval_votes", sa.Column("seat_approver_id", sa.Uuid(), nullable=True))
    op.execute("UPDATE approval_votes SET seat_approver_id = approver_id")
    op.alter_column("approval_votes", "seat_approver_id", nullable=False)
    op.create_unique_constraint(op.f("uq_approval_votes_request_id_seat_approver_id"), "approval_votes", ["request_id", "seat_approver_id"])
    op.create_foreign_key(op.f("fk_approval_votes_request_id_approver_id_request_approvers"), "approval_votes", "request_approvers", ["request_id", "approver_id"], ["request_id", "approver_id"], ondelete="RESTRICT")
    op.create_foreign_key(op.f("fk_approval_votes_request_id_seat_approver_id_request_approvers"), "approval_votes", "request_approvers", ["request_id", "seat_approver_id"], ["request_id", "approver_id"], ondelete="RESTRICT")

    # --- conditions ------------------------------------------------------------------------
    op.create_table(
        "approval_conditions",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("approver_id", sa.Uuid(), nullable=False),
        sa.Column("seat_approver_id", sa.Uuid(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("voice_call_id", sa.Uuid(), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by_api_key_id", sa.Uuid(), nullable=True),
        sa.Column("resolution_note", sa.String(length=1000), nullable=True),
        sa.Column("evidence_reference", sa.String(length=500), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("(status = 'open') = (closed_at IS NULL)", name=op.f("ck_approval_conditions_closed_at_matches")),
        sa.CheckConstraint("source IN ('voice', 'api')", name=op.f("ck_approval_conditions_source_valid")),
        sa.CheckConstraint("status IN ('open', 'satisfied', 'not_satisfied', 'superseded', 'lapsed')", name=op.f("ck_approval_conditions_status_valid")),
        sa.CheckConstraint("status NOT IN ('satisfied', 'not_satisfied') OR resolved_by_api_key_id IS NOT NULL", name=op.f("ck_approval_conditions_resolution_has_actor")),
        sa.CheckConstraint("length(btrim(description)) > 0", name=op.f("ck_approval_conditions_description_not_blank")),
        sa.ForeignKeyConstraint(["request_id", "approver_id"], ["request_approvers.request_id", "request_approvers.approver_id"], name=op.f("fk_approval_conditions_request_id_approver_id_request_approvers"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["request_id", "organization_id"], ["approval_requests.id", "approval_requests.organization_id"], name=op.f("fk_approval_conditions_request_id_organization_id_approval_requests"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["request_id", "seat_approver_id"], ["request_approvers.request_id", "request_approvers.approver_id"], name=op.f("fk_approval_conditions_request_id_seat_approver_id_request_approvers"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["resolved_by_api_key_id"], ["api_keys.id"], name=op.f("fk_approval_conditions_resolved_by_api_key_id_api_keys"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["voice_call_id"], ["voice_calls.id"], name=op.f("fk_approval_conditions_voice_call_id_voice_calls"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_conditions")),
    )
    op.create_index(op.f("ix_approval_conditions_organization_id"), "approval_conditions", ["organization_id"], unique=False)
    op.create_index(op.f("ix_approval_conditions_request_id"), "approval_conditions", ["request_id"], unique=False)
    op.create_index("uq_approval_conditions_open_per_approver", "approval_conditions", ["request_id", "approver_id"], unique=True, postgresql_where=sa.text("status = 'open'"))
    op.execute(
        "CREATE TRIGGER approval_conditions_freeze_closed BEFORE UPDATE OR DELETE "
        "ON approval_conditions FOR EACH ROW EXECUTE FUNCTION approval_conditions_freeze_closed()"
    )

    # --- escalation steps ----------------------------------------------------------------
    op.create_table(
        "escalation_steps",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("seat_approver_id", sa.Uuid(), nullable=False),
        sa.Column("backup_approver_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.SmallInteger(), nullable=False),
        sa.Column("round", sa.SmallInteger(), nullable=False),
        sa.Column("voice_call_id", sa.Uuid(), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("round >= 1", name=op.f("ck_escalation_steps_round_positive")),
        sa.CheckConstraint("seat_approver_id <> backup_approver_id", name=op.f("ck_escalation_steps_backup_not_seat")),
        sa.CheckConstraint("sequence BETWEEN 1 AND 3", name=op.f("ck_escalation_steps_sequence_range")),
        sa.ForeignKeyConstraint(["request_id", "backup_approver_id"], ["request_approvers.request_id", "request_approvers.approver_id"], name=op.f("fk_escalation_steps_request_id_backup_approver_id_request_approvers"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["request_id", "organization_id"], ["approval_requests.id", "approval_requests.organization_id"], name=op.f("fk_escalation_steps_request_id_organization_id_approval_requests"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["request_id", "seat_approver_id"], ["request_approvers.request_id", "request_approvers.approver_id"], name=op.f("fk_escalation_steps_request_id_seat_approver_id_request_approvers"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["voice_call_id"], ["voice_calls.id"], name=op.f("fk_escalation_steps_voice_call_id_voice_calls"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_escalation_steps")),
        sa.UniqueConstraint("request_id", "backup_approver_id", name=op.f("uq_escalation_steps_request_id_backup_approver_id")),
        sa.UniqueConstraint("request_id", "sequence", name=op.f("uq_escalation_steps_request_id_sequence")),
    )
    op.create_index(op.f("ix_escalation_steps_organization_id"), "escalation_steps", ["organization_id"], unique=False)
    op.create_index(op.f("ix_escalation_steps_request_id"), "escalation_steps", ["request_id"], unique=False)
    op.execute(
        "CREATE TRIGGER escalation_steps_immutable BEFORE UPDATE OR DELETE ON escalation_steps "
        "FOR EACH ROW EXECUTE FUNCTION approval_line_immutable_row()"
    )

    # --- audit events about policies --------------------------------------------------------
    op.add_column("audit_events", sa.Column("policy_id", sa.Uuid(), nullable=True))
    op.alter_column("audit_events", "request_id", existing_type=sa.UUID(), nullable=True)
    op.create_foreign_key(op.f("fk_audit_events_policy_id_organization_id_approval_policies"), "audit_events", "approval_policies", ["policy_id", "organization_id"], ["id", "organization_id"], ondelete="RESTRICT")
    op.create_check_constraint(op.f("ck_audit_events_has_subject"), "audit_events", "request_id IS NOT NULL OR policy_id IS NOT NULL")
    op.create_index("ix_audit_events_policy_id_sequence", "audit_events", ["policy_id", "sequence"], unique=False)

    # --- voice calls / jobs ------------------------------------------------------------------
    _replace_check("voice_calls", "ck_voice_calls_decision_status_valid", "decision_status IN ('awaiting', 'accepted', 'no_decision', 'refused', 'conditional')")
    op.add_column("voice_jobs", sa.Column("request_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_voice_jobs_request_id"), "voice_jobs", ["request_id"], unique=False)
    op.create_index("uq_voice_jobs_request_job", "voice_jobs", ["request_id", "job_type"], unique=True, postgresql_where=sa.text("request_id IS NOT NULL"))
    op.create_foreign_key(op.f("fk_voice_jobs_request_id_approval_requests"), "voice_jobs", "approval_requests", ["request_id"], ["id"], ondelete="RESTRICT")
    _replace_check("voice_jobs", "ck_voice_jobs_job_type_valid", "job_type IN ('initiate_call', 'process_event', 'reconcile_call', 'expire_request', 'escalate_request')")
    _replace_check("voice_jobs", "ck_voice_jobs_call_job_has_call", "job_type NOT IN ('initiate_call', 'reconcile_call') OR voice_call_id IS NOT NULL")
    op.create_check_constraint(op.f("ck_voice_jobs_request_job_has_request"), "voice_jobs", "(job_type IN ('expire_request', 'escalate_request')) = (request_id IS NOT NULL)")


GUARD = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM approval_requests WHERE status = 'expired')
       OR EXISTS (SELECT 1 FROM audit_events WHERE request_id IS NULL)
       OR EXISTS (SELECT 1 FROM approval_conditions)
       OR EXISTS (SELECT 1 FROM escalation_steps)
       OR EXISTS (SELECT 1 FROM approval_votes WHERE seat_approver_id <> approver_id)
       OR EXISTS (SELECT 1 FROM voice_calls WHERE decision_status = 'conditional')
       OR EXISTS (SELECT 1 FROM request_approvers WHERE role = 'backup' OR can_veto)
       OR EXISTS (SELECT 1 FROM approval_policies
                  WHERE approval_mode <> 'unanimous' OR decision_deadline_seconds IS NOT NULL
                     OR max_escalations > 0)
       OR EXISTS (SELECT 1 FROM policy_approvers WHERE role <> 'primary' OR can_veto) THEN
        RAISE EXCEPTION 'refusing to downgrade 0003: Chunk 3 authorization data exists';
    END IF;
END;
$$;
"""


def downgrade() -> None:
    op.execute(GUARD)
    # Request-level jobs carry no history worth keeping.
    op.execute("DELETE FROM voice_jobs WHERE request_id IS NOT NULL")
    op.drop_constraint(op.f("ck_voice_jobs_request_job_has_request"), "voice_jobs", type_="check")
    _replace_check("voice_jobs", "ck_voice_jobs_call_job_has_call", "job_type = 'process_event' OR voice_call_id IS NOT NULL")
    _replace_check("voice_jobs", "ck_voice_jobs_job_type_valid", "job_type IN ('initiate_call', 'process_event', 'reconcile_call')")
    op.drop_constraint(op.f("fk_voice_jobs_request_id_approval_requests"), "voice_jobs", type_="foreignkey")
    op.drop_index("uq_voice_jobs_request_job", table_name="voice_jobs", postgresql_where=sa.text("request_id IS NOT NULL"))
    op.drop_index(op.f("ix_voice_jobs_request_id"), table_name="voice_jobs")
    op.drop_column("voice_jobs", "request_id")
    _replace_check("voice_calls", "ck_voice_calls_decision_status_valid", "decision_status IN ('awaiting', 'accepted', 'no_decision', 'refused')")

    op.drop_index("ix_audit_events_policy_id_sequence", table_name="audit_events")
    op.drop_constraint(op.f("ck_audit_events_has_subject"), "audit_events", type_="check")
    op.drop_constraint(op.f("fk_audit_events_policy_id_organization_id_approval_policies"), "audit_events", type_="foreignkey")
    op.alter_column("audit_events", "request_id", existing_type=sa.UUID(), nullable=False)
    op.drop_column("audit_events", "policy_id")

    op.execute("DROP TRIGGER IF EXISTS escalation_steps_immutable ON escalation_steps")
    op.drop_index(op.f("ix_escalation_steps_request_id"), table_name="escalation_steps")
    op.drop_index(op.f("ix_escalation_steps_organization_id"), table_name="escalation_steps")
    op.drop_table("escalation_steps")
    op.execute("DROP TRIGGER IF EXISTS approval_conditions_freeze_closed ON approval_conditions")
    op.drop_index("uq_approval_conditions_open_per_approver", table_name="approval_conditions", postgresql_where=sa.text("status = 'open'"))
    op.drop_index(op.f("ix_approval_conditions_request_id"), table_name="approval_conditions")
    op.drop_index(op.f("ix_approval_conditions_organization_id"), table_name="approval_conditions")
    op.drop_table("approval_conditions")

    op.drop_constraint(op.f("fk_approval_votes_request_id_seat_approver_id_request_approvers"), "approval_votes", type_="foreignkey")
    op.drop_constraint(op.f("fk_approval_votes_request_id_approver_id_request_approvers"), "approval_votes", type_="foreignkey")
    op.drop_constraint(op.f("uq_approval_votes_request_id_seat_approver_id"), "approval_votes", type_="unique")
    op.drop_column("approval_votes", "seat_approver_id")

    op.execute("DROP TRIGGER IF EXISTS request_approvers_immutable ON request_approvers")
    op.drop_index(op.f("ix_request_approvers_organization_id"), table_name="request_approvers")
    op.drop_index(op.f("ix_request_approvers_approver_id"), table_name="request_approvers")
    op.drop_table("request_approvers")

    _replace_check("approval_requests", "ck_approval_requests_status_valid", "status IN ('pending', 'approved', 'rejected')")
    op.drop_index("ix_approval_requests_status_expires_at", table_name="approval_requests")
    op.drop_column("approval_requests", "expires_at")

    op.drop_constraint(op.f("ck_policy_approvers_only_primaries_veto"), "policy_approvers", type_="check")
    op.drop_constraint(op.f("ck_policy_approvers_role_valid"), "policy_approvers", type_="check")
    op.drop_column("policy_approvers", "can_veto")
    op.drop_column("policy_approvers", "role")
    for name in (
        "ck_approval_policies_escalation_before_deadline",
        "ck_approval_policies_max_escalations_range",
        "ck_approval_policies_escalation_after_range",
        "ck_approval_policies_escalation_configured_consistently",
        "ck_approval_policies_deadline_range",
        "ck_approval_policies_unanimous_uses_any_rejection",
        "ck_approval_policies_veto_mode_valid",
        "ck_approval_policies_approval_mode_valid",
    ):
        op.drop_constraint(op.f(name), "approval_policies", type_="check")
    op.drop_column("approval_policies", "max_escalations")
    op.drop_column("approval_policies", "escalation_after_seconds")
    op.drop_column("approval_policies", "decision_deadline_seconds")
    op.drop_column("approval_policies", "veto_mode")
    op.drop_column("approval_policies", "approval_mode")

    op.execute("DROP FUNCTION IF EXISTS approval_conditions_freeze_closed()")
    op.execute("DROP FUNCTION IF EXISTS approval_line_immutable_row()")

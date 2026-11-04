"""Telegram pairing: one-time links, verified accounts, revocation, and the audit trail.

Everything runs through the real API, the real text worker loop and the mock provider
(Telegram-shaped updates parsed by the real adapter). Telegram IDs are fictional.
"""

from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import select

from app.cli import main as cli_main
from app.db.models import ApproverChannelLink, AuditEvent
from app.db.models.text import LinkStatus
from tests.helpers import API, create_approver

pytestmark = pytest.mark.integration

ALICE_TG, BOB_TG, MALLORY_TG = "7000001", "7000002", "7000666"


def start_pairing(client, tenant, approver_id: str):
    return client.post(
        f"{API}/approvers/{approver_id}/channel-links",
        json={"channel": "telegram"},
        headers=tenant.admin,
    )


def code_of(url: str) -> str:
    return url.split("?start=", 1)[1]


def press_start(text_worker, url: str, user: str, **kwargs) -> str:
    """The user presses Start on the link; returns the bot's reply."""
    before = len(text_worker.provider.outbox())
    text_worker.provider.send_text(f"/start {code_of(url)}", sender_id=user, **kwargs)
    text_worker.run()
    replies = [m for m in text_worker.provider.outbox()[before:] if m["kind"] == "message"]
    return replies[-1]["text"] if replies else ""


def links(session, approver_id: str) -> list[ApproverChannelLink]:
    session.expire_all()
    return list(
        session.scalars(
            select(ApproverChannelLink)
            .where(ApproverChannelLink.approver_id == UUID(approver_id))
            .order_by(ApproverChannelLink.created_at)
        )
    )


def approver_events(session, approver_id: str) -> list[tuple[str, dict]]:
    session.expire_all()
    rows = session.scalars(
        select(AuditEvent)
        .where(AuditEvent.approver_id == UUID(approver_id))
        .order_by(AuditEvent.sequence)
    )
    return [(e.event_type, e.event_data) for e in rows]


@pytest.fixture
def alice(text_client, tenant):
    return create_approver(text_client, tenant, "Alice", phone_number="+14155550101")


def test_pairing_links_the_account_and_the_bot_replies_with_the_name(
    text_client, tenant, alice, text_worker, session
):
    created = start_pairing(text_client, tenant, alice["id"])
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "pending"
    assert body["pairing_url"].startswith("https://t.me/approval_line_mock_bot?start=")
    assert "external_user_id" not in body and "pairing_code_hash" not in body

    assert press_start(text_worker, body["pairing_url"], ALICE_TG) == (
        "Linked to Alice. Approval requests will arrive here."
    )
    (link,) = links(session, alice["id"])
    assert (link.status, link.external_user_id, link.external_chat_id) == (
        LinkStatus.VERIFIED,
        ALICE_TG,
        ALICE_TG,
    )
    assert code_of(body["pairing_url"]) != link.pairing_code_hash  # only the hash is stored

    listed = text_client.get(
        f"{API}/approvers/{alice['id']}/channel-links", headers=tenant.admin
    ).json()
    assert [i["status"] for i in listed["items"]] == ["verified"]
    assert ALICE_TG not in str(listed) and "pairing_url" not in str(listed)

    ((event_type, data),) = approver_events(session, alice["id"])
    assert event_type == "channel.link_created"
    assert ALICE_TG not in str(data)  # no Telegram IDs in audit data


def test_a_used_link_cannot_be_used_by_a_second_telegram_user(
    text_client, tenant, alice, text_worker, session
):
    url = start_pairing(text_client, tenant, alice["id"]).json()["pairing_url"]
    press_start(text_worker, url, ALICE_TG)
    reply = press_start(text_worker, url, MALLORY_TG)
    assert reply == "This pairing link has already been used. Ask for a new one."
    (link,) = links(session, alice["id"])
    assert link.external_user_id == ALICE_TG  # unchanged
    assert approver_events(session, alice["id"])[-1] == (
        "channel.link_rejected",
        {"link_id": str(link.id), "channel": "telegram", "reason": "code_used"},
    )
    # The legitimate user pressing Start again is harmless.
    assert press_start(text_worker, url, ALICE_TG) == "This chat is already linked."


def test_an_expired_or_superseded_link_does_not_work(
    text_client, tenant, alice, text_worker, session, session_factory
):
    first = start_pairing(text_client, tenant, alice["id"]).json()["pairing_url"]
    second = start_pairing(text_client, tenant, alice["id"]).json()["pairing_url"]
    assert press_start(text_worker, first, ALICE_TG).startswith(
        "This pairing link is no longer valid"
    )

    with session_factory() as db:
        db.execute(
            ApproverChannelLink.__table__.update()
            .where(ApproverChannelLink.status == LinkStatus.PENDING)
            .values(
                pairing_expires_at=ApproverChannelLink.pairing_expires_at - timedelta(minutes=11)
            )
        )
        db.commit()
    assert (
        press_start(text_worker, second, ALICE_TG)
        == "This pairing link has expired. Ask for a new one."
    )
    assert all(link.status != LinkStatus.VERIFIED for link in links(session, alice["id"]))
    listed = text_client.get(
        f"{API}/approvers/{alice['id']}/channel-links", headers=tenant.admin
    ).json()
    assert sorted(i["status"] for i in listed["items"]) == ["expired", "revoked"]


def test_one_approver_per_telegram_account(text_client, tenant, alice, text_worker, session):
    bob = create_approver(text_client, tenant, "Bob", phone_number="+14155550102")
    press_start(
        text_worker, start_pairing(text_client, tenant, alice["id"]).json()["pairing_url"], ALICE_TG
    )
    reply = press_start(
        text_worker, start_pairing(text_client, tenant, bob["id"]).json()["pairing_url"], ALICE_TG
    )
    assert reply.startswith("This Telegram account is already linked to another approver")
    assert [link.status for link in links(session, bob["id"])] == [LinkStatus.PENDING]


def test_a_telegram_account_cannot_be_linked_across_organizations(
    text_client, tenant, make_tenant, alice, text_worker
):
    other = make_tenant("Globex")
    eve = create_approver(text_client, other, "Eve", phone_number="+14155550109")
    press_start(
        text_worker, start_pairing(text_client, tenant, alice["id"]).json()["pairing_url"], ALICE_TG
    )
    reply = press_start(
        text_worker, start_pairing(text_client, other, eve["id"]).json()["pairing_url"], ALICE_TG
    )
    assert reply.startswith("This Telegram account is already linked")


def test_re_pairing_replaces_the_old_link(text_client, tenant, alice, text_worker, session):
    press_start(
        text_worker, start_pairing(text_client, tenant, alice["id"]).json()["pairing_url"], ALICE_TG
    )
    press_start(
        text_worker, start_pairing(text_client, tenant, alice["id"]).json()["pairing_url"], BOB_TG
    )
    old, new = links(session, alice["id"])
    assert (old.status, new.status, new.external_user_id) == (
        LinkStatus.REVOKED,
        LinkStatus.VERIFIED,
        BOB_TG,
    )
    types = [t for t, _ in approver_events(session, alice["id"])]
    assert types == ["channel.link_created", "channel.link_revoked", "channel.link_created"]


def test_pairing_only_in_a_private_chat(text_client, tenant, alice, text_worker, session):
    url = start_pairing(text_client, tenant, alice["id"]).json()["pairing_url"]
    text_worker.provider.send_text(
        f"/start {code_of(url)}", sender_id=ALICE_TG, chat_id="-100123", chat_type="group"
    )
    text_worker.run()
    assert [link.status for link in links(session, alice["id"])] == [LinkStatus.PENDING]


def test_revoking_links_is_audited(text_client, tenant, alice, text_worker, session):
    press_start(
        text_worker, start_pairing(text_client, tenant, alice["id"]).json()["pairing_url"], ALICE_TG
    )
    response = text_client.delete(
        f"{API}/approvers/{alice['id']}/channel-links", headers=tenant.admin
    )
    assert response.json() == {"revoked": 1}
    assert [link.status for link in links(session, alice["id"])] == [LinkStatus.REVOKED]
    event_type, data = approver_events(session, alice["id"])[-1]
    assert event_type == "channel.link_revoked" and data["reason"] == "revoked"
    assert data["actor_api_key_id"] == str(tenant.key_ids["admin"])


def test_channel_links_need_approver_scopes_and_tenant_isolation(
    text_client, tenant, make_tenant, alice
):
    url = f"{API}/approvers/{alice['id']}/channel-links"
    assert text_client.post(url, json={}, headers=tenant.agent).status_code == 403
    assert text_client.get(url, headers=tenant.agent).status_code == 403
    assert text_client.delete(url, headers=tenant.agent).status_code == 403
    other = make_tenant("Globex")
    assert text_client.post(url, json={}, headers=other.admin).status_code == 404
    assert text_client.post(url, json={"channel": "sms"}, headers=tenant.admin).status_code == 422
    assert text_client.post(url, json={"extra": 1}, headers=tenant.admin).status_code == 422


def test_pairing_is_refused_while_text_is_disabled(voice_client, tenant):
    alice = create_approver(voice_client, tenant, "Alice", phone_number="+14155550101")
    response = start_pairing(voice_client, tenant, alice["id"])
    assert response.status_code == 503 and response.json()["error"]["code"] == "text_unavailable"


def test_pair_telegram_cli_prints_a_one_time_link(
    text_client, tenant, alice, text_settings, monkeypatch, capsys
):
    monkeypatch.setattr("app.cli.get_settings", lambda: text_settings)
    assert cli_main(["pair-telegram", "--approver-id", alice["id"]]) == 0
    out = capsys.readouterr().out
    assert "https://t.me/approval_line_mock_bot?start=" in out and "Alice" in out

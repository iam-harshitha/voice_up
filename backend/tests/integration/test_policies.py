import pytest

from tests.helpers import API, create_approver, create_policy

pytestmark = pytest.mark.integration


def post_policy(client, tenant, approver_ids, required):
    return client.post(
        f"{API}/policies",
        json={"name": "Deploys", "required_approvals": required, "approver_ids": approver_ids},
        headers=tenant.admin,
    )


def assert_policy_invalid(response, fragment):
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "approval_policy_invalid"
    assert fragment in error["message"]


class TestApprovers:
    def test_create_and_list(self, client, tenant):
        created = create_approver(client, tenant, "Alice", phone_number="+14155550101")
        assert created["phone_number"] == "+14155550101"
        assert created["is_active"] is True
        listed = client.get(f"{API}/approvers", headers=tenant.admin).json()
        assert [item["id"] for item in listed["items"]] == [created["id"]]

    @pytest.mark.parametrize(
        "payload",
        [
            {"name": "NoContact"},
            {"name": "BadPhone", "phone_number": "4155550101"},
            {"name": "BadPhone", "phone_number": "+0123"},
            {"name": "BadPhone", "phone_number": "+1234567890123456"},
            {"name": "BadEmail", "email": "not-an-email"},
            {"name": "   ", "email": "a@example.com"},
        ],
    )
    def test_invalid_approver_rejected(self, client, tenant, payload):
        response = client.post(f"{API}/approvers", json=payload, headers=tenant.admin)
        assert response.status_code == 422

    def test_cannot_remove_last_contact_method(self, client, tenant):
        approver = create_approver(client, tenant, "Alice")
        response = client.patch(
            f"{API}/approvers/{approver['id']}", json={"email": None}, headers=tenant.admin
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "approver_contact_required"


class TestPolicyCreation:
    def test_one_approver_policy(self, client, tenant):
        alice = create_approver(client, tenant, "Alice")
        policy = create_policy(client, tenant, [alice["id"]])
        assert policy["required_approvals"] == 1
        assert policy["version"] == 1 and policy["is_active"] is True
        assert [a["id"] for a in policy["approvers"]] == [alice["id"]]

        fetched = client.get(f"{API}/policies/{policy['id']}", headers=tenant.admin)
        assert fetched.status_code == 200
        assert fetched.json() == policy

    def test_two_approver_policy_preserves_order(self, client, tenant):
        alice = create_approver(client, tenant, "Alice")
        bob = create_approver(client, tenant, "Bob")
        policy = create_policy(client, tenant, [bob["id"], alice["id"]])
        assert policy["required_approvals"] == 2
        assert [a["id"] for a in policy["approvers"]] == [bob["id"], alice["id"]]
        assert "phone_number" not in policy["approvers"][0]

    def test_list_policies(self, client, tenant):
        alice = create_approver(client, tenant, "Alice")
        first = create_policy(client, tenant, [alice["id"]], name="First")
        second = create_policy(client, tenant, [alice["id"]], name="Second")
        page = client.get(f"{API}/policies", headers=tenant.admin).json()
        assert page["total"] == 2
        assert [p["id"] for p in page["items"]] == [second["id"], first["id"]]

    def test_duplicate_approvers_rejected(self, client, tenant):
        alice = create_approver(client, tenant, "Alice")
        response = post_policy(client, tenant, [alice["id"], alice["id"]], 2)
        assert_policy_invalid(response, "distinct")

    @pytest.mark.parametrize(("count", "required"), [(1, 2), (2, 1)])
    def test_approver_count_must_match(self, client, tenant, count, required):
        ids = [create_approver(client, tenant, f"A{i}")["id"] for i in range(count)]
        expected = "exactly two" if required == 2 else "exactly one"
        assert_policy_invalid(post_policy(client, tenant, ids, required), expected)

    @pytest.mark.parametrize("required", [0, 3, "two", None])
    def test_unsupported_required_approvals_is_422(self, client, tenant, required):
        alice = create_approver(client, tenant, "Alice")
        assert post_policy(client, tenant, [alice["id"]], required).status_code == 422

    def test_inactive_approver_rejected(self, client, tenant):
        alice = create_approver(client, tenant, "Alice")
        bob = create_approver(client, tenant, "Bob")
        client.patch(
            f"{API}/approvers/{bob['id']}", json={"is_active": False}, headers=tenant.admin
        )
        assert_policy_invalid(post_policy(client, tenant, [alice["id"], bob["id"]], 2), "inactive")

    def test_cross_organization_approver_rejected(self, client, tenant, make_tenant):
        other = make_tenant("Globex")
        alice = create_approver(client, tenant, "Alice")
        outsider = create_approver(client, other, "Mallory")
        response = post_policy(client, tenant, [alice["id"], outsider["id"]], 2)
        assert_policy_invalid(response, "not found")
        assert client.get(f"{API}/policies", headers=tenant.admin).json()["total"] == 0

    def test_unknown_policy_is_404(self, client, tenant):
        response = client.get(
            f"{API}/policies/00000000-0000-0000-0000-000000000000", headers=tenant.admin
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "policy_not_found"


class TestPolicyUpdate:
    def test_version_increments_only_on_change(self, client, tenant):
        alice = create_approver(client, tenant, "Alice")
        policy = create_policy(client, tenant, [alice["id"]])
        url = f"{API}/policies/{policy['id']}"

        same = client.patch(url, json={"name": policy["name"]}, headers=tenant.admin).json()
        assert same["version"] == 1
        renamed = client.patch(url, json={"name": "Renamed"}, headers=tenant.admin).json()
        assert (renamed["name"], renamed["version"]) == ("Renamed", 2)

    def test_change_approvers_and_count_together(self, client, tenant):
        alice = create_approver(client, tenant, "Alice")
        bob = create_approver(client, tenant, "Bob")
        policy = create_policy(client, tenant, [alice["id"]])
        url = f"{API}/policies/{policy['id']}"

        only_count = client.patch(url, json={"required_approvals": 2}, headers=tenant.admin)
        assert_policy_invalid(only_count, "exactly two")

        updated = client.patch(
            url,
            json={"required_approvals": 2, "approver_ids": [bob["id"], alice["id"]]},
            headers=tenant.admin,
        ).json()
        assert updated["required_approvals"] == 2 and updated["version"] == 2
        assert [a["id"] for a in updated["approvers"]] == [bob["id"], alice["id"]]

        swapped = client.patch(
            url, json={"approver_ids": [alice["id"], bob["id"]]}, headers=tenant.admin
        ).json()
        assert [a["id"] for a in swapped["approvers"]] == [alice["id"], bob["id"]]
        assert swapped["version"] == 3

    def test_failed_update_changes_nothing(self, client, tenant):
        alice = create_approver(client, tenant, "Alice")
        policy = create_policy(client, tenant, [alice["id"]])
        url = f"{API}/policies/{policy['id']}"
        response = client.patch(
            url,
            json={"name": "New", "approver_ids": [alice["id"], alice["id"]]},
            headers=tenant.admin,
        )
        assert response.status_code == 400
        assert client.get(url, headers=tenant.admin).json() == policy

    def test_null_for_required_field_is_422(self, client, tenant):
        alice = create_approver(client, tenant, "Alice")
        policy = create_policy(client, tenant, [alice["id"]])
        url = f"{API}/policies/{policy['id']}"
        for body in ({"name": None}, {"approver_ids": None}, {}):
            assert client.patch(url, json=body, headers=tenant.admin).status_code == 422

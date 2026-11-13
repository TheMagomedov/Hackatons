"""Signed Mini App roles against a temporary database and fake providers only."""

from datetime import timedelta

import pytest

from tests.test_miniapp import OWNER, signed_data
from tests.test_miniapp_controls import mini as mini
from tests.test_miniapp_controls import prepared_post

REVIEWER = 3003
OPERATOR = 2002
OUTSIDER = 4004


def invite(service, actor, role):
    item = service.repo.create_access_invite(
        OWNER, role, 1, service.now() + timedelta(hours=1), service.now(),
    )
    assert service.claim_invite(actor, item["token"]) == role


def headers(service, actor):
    return {"X-Telegram-Init-Data": signed_data(service.now(), actor=actor)}


def read_routes(mini, post):
    job = mini.service.list_jobs(OWNER, mini.business.id)[0]
    return [
        "/mini/api/state",
        f"/mini/api/state?business={mini.business.id}",
        mini.prefix + "/posts",
        mini.prefix + "/calendar?month=" + mini.service.now().strftime("%Y-%m"),
        mini.prefix + "/control",
        mini.prefix + "/jobs",
        mini.prefix + "/history",
        mini.prefix + "/report",
        f"/mini/api/job/{job.id}",
        f"/mini/api/post/{post.id}",
        f"/mini/api/asset/{post.image_asset_id}",
    ]


async def test_reviewer_reads_workspace_posts_calendar_history_reports_and_png(mini):
    post = await prepared_post(mini)
    invite(mini.service, REVIEWER, "reviewer")
    auth = headers(mini.service, REVIEWER)
    before = mini.service.ai_usage(OWNER)
    for url in read_routes(mini, post):
        response = await mini.client.get(url, headers=auth)
        assert response.status == 200, (url, await response.text())
        assert response.headers["Cache-Control"] == "no-store"
        if "/asset/" in url:
            assert response.content_type == "image/png"
            assert (await response.read()).startswith(b"\x89PNG\r\n\x1a\n")
        else:
            await response.json()
    state = await (await mini.client.get("/mini/api/state", headers=auth)).json()
    assert state["access"] == {"role": "reviewer", "can_write": False}
    assert state["business"]["id"] == mini.business.id
    assert state["sources"]
    assert mini.service.ai_usage(OWNER) == before
    assert mini.service.publisher.sent == []
    page = await (await mini.client.get("/mini/")).text()
    assert 'src="./access.js"' in page
    assert (await mini.client.get("/mini/access.js")).status == 200


async def test_every_mutation_rejects_reviewer_before_parsing_body_or_spending(mini):
    post = await prepared_post(mini)
    invite(mini.service, REVIEWER, "reviewer")
    auth = headers(mini.service, REVIEWER)
    job = mini.service.list_jobs(OWNER, mini.business.id)[0]
    routes = [
        "/mini/api/business",
        *[mini.prefix + suffix for suffix in (
            "/source", "/pdf", "/image", "/profile/manual", "/draft", "/profile/analyze",
            "/profile/confirm", "/channel", "/prepare", "/pause", "/automation", "/chat",
        )],
        f"/mini/api/job/{job.id}/cancel",
        *[f"/mini/api/post/{post.id}/" + action for action in (
            "edit", "approve", "reject", "revise-text", "revise-image", "refresh-context",
        )],
        "/mini/api/future-write-route",
    ]
    before = mini.service.ai_usage(OWNER)
    original = mini.service.get_post(OWNER, post.id)
    for url in routes:
        # Invalid bodies demonstrate rejection by the access guard, before handlers.
        response = await mini.client.post(url, headers=auth, data=b"not-json")
        assert response.status == 403, (url, await response.text())
        assert (await response.json())["error"]
    assert mini.service.ai_usage(OWNER) == before
    assert mini.service.get_post(OWNER, post.id) == original
    assert mini.service.publisher.sent == []


async def test_revocation_applies_to_existing_signed_session_and_image_requests(mini):
    post = await prepared_post(mini)
    invite(mini.service, REVIEWER, "reviewer")
    auth = headers(mini.service, REVIEWER)
    assert (await mini.client.get("/mini/api/state", headers=auth)).status == 200
    mini.service.repo.revoke_access_member(OWNER, REVIEWER, mini.service.now())
    for url in read_routes(mini, post):
        assert (await mini.client.get(url, headers=auth)).status == 403, url
    assert (await mini.client.post(mini.prefix + "/prepare", headers=auth, json={
        "request": "Forbidden", "request_id": "revoked-session",
    })).status == 403
    assert mini.service.publisher.sent == []


@pytest.mark.parametrize("actor", [REVIEWER, OPERATOR])
async def test_workspace_scope_excludes_businesses_owned_elsewhere(mini, actor):
    invite(mini.service, actor, "reviewer" if actor == REVIEWER else "operator")
    foreign = mini.service.repo.create_business(9999, "Foreign workspace", "UTC", mini.service.now())
    auth = headers(mini.service, actor)
    state = await (await mini.client.get("/mini/api/state", headers=auth)).json()
    assert {item["id"] for item in state["businesses"]} == {mini.business.id}
    for url in [
        f"/mini/api/state?business={foreign.id}",
        *[f"/mini/api/business/{foreign.id}/" + action for action in (
            "posts", "control", "jobs", "history", "report",
        )],
    ]:
        assert (await mini.client.get(url, headers=auth)).status == 403, url


async def test_operator_can_write_with_real_actor_and_owner_is_unchanged(mini):
    post = await prepared_post(mini)
    invite(mini.service, OPERATOR, "operator")
    auth = headers(mini.service, OPERATOR)
    state = await (await mini.client.get("/mini/api/state", headers=auth)).json()
    assert state["access"] == {"role": "operator", "can_write": True}
    created = await mini.client.post("/mini/api/business", headers=auth, json={"name": "Operator workspace"})
    assert created.status == 201
    business_id = (await created.json())["id"]
    assert mini.service.get_business(OPERATOR, business_id).owner_id == OWNER
    edited = await mini.client.post(f"/mini/api/post/{post.id}/edit", headers=auth, json={
        "revision": post.revision, "scheduled_at": (mini.service.now() + timedelta(hours=1)).isoformat(),
    })
    assert edited.status == 200
    approved = await mini.client.post(f"/mini/api/post/{post.id}/approve", headers=auth, json={
        "revision": (await edited.json())["revision"],
    })
    assert approved.status == 200
    assert (await approved.json())["approved_by"] == OPERATOR
    audit = mini.service.repo.audit_events("post", post.id)
    assert audit[-1]["details"]["actor_id"] == OPERATOR
    assert mini.service.publisher.sent == []


async def test_outsider_and_missing_or_forged_telegram_sessions_remain_forbidden(mini):
    post = await prepared_post(mini)
    auth = headers(mini.service, OUTSIDER)
    for url in read_routes(mini, post):
        assert (await mini.client.get(url, headers=auth)).status == 403, url
    assert (await mini.client.post("/mini/api/business", headers=auth, json={"name": "Forbidden"})).status == 403
    for value in ["", signed_data(mini.service.now(), actor=REVIEWER).replace("test-only", "tampered")]:
        assert (await mini.client.get("/mini/api/state", headers={"X-Telegram-Init-Data": value})).status == 403

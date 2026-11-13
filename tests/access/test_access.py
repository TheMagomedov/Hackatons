import hashlib
import inspect
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from aiogram.methods import SendPhoto, SetChatMenuButton

from app.shared.errors import Forbidden
from app.shared.models import PostStatus
from app.storage import Repository
from tests.bot.test_product_workflow import OWNER as BOT_OWNER
from tests.core.test_product_controls import prepare
from tests.core.test_service import OWNER

OPERATOR, VIEWER = 2002, 3003


def grant(service, actor, role, *, uses=1):
    invite = service.repo.create_access_invite(
        service.settings.owner_id, role, uses, service.now() + timedelta(days=1), service.now(),
    )
    service.claim_invite(actor, invite["token"])
    return invite


async def test_invites_are_hashed_capped_idempotent_expiring_and_revocable(setup):
    s = setup.service
    invitation = grant(s, VIEWER, "reviewer")
    assert s.claim_invite(VIEWER, invitation["token"]) == "reviewer"
    assert s.repo.list_access(OWNER)["invites"][0]["uses"] == 1
    with s.repo._reader() as conn:
        row = dict(conn.execute("SELECT * FROM access_invites").fetchone())
        assert row["token_hash"] == hashlib.sha256(invitation["token"].encode()).hexdigest()
        assert invitation["token"] not in json.dumps(row)
        assert invitation["token"] not in json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit")])
    with pytest.raises(Forbidden):
        s.claim_invite(OPERATOR, invitation["token"])
    s.repo.revoke_access_member(OWNER, VIEWER, s.now())
    assert s.access_role(VIEWER) is None
    reopened = Repository(s.repo.db_path)
    assert reopened.access_role(OWNER, VIEWER) is None
    reopened.close()
    with pytest.raises(Forbidden):
        s.claim_invite(VIEWER, invitation["token"])
    setup.clock.advance(seconds=1)
    fresh = grant(s, VIEWER, "reviewer", uses=2)
    s.repo.revoke_access_invite(OWNER, fresh["id"], s.now())
    with pytest.raises(Forbidden):
        s.claim_invite(OPERATOR, fresh["token"])
    expiring = s.repo.create_access_invite(OWNER, "operator", 1, s.now() + timedelta(seconds=1), s.now())
    setup.clock.advance(seconds=2)
    with pytest.raises(Forbidden):
        s.claim_invite(OPERATOR, expiring["token"])


async def test_invite_limit_is_atomic_under_concurrent_claims(setup):
    s = setup.service
    invite = s.repo.create_access_invite(OWNER, "reviewer", 2, s.now() + timedelta(days=1), s.now())

    def claim(actor):
        try:
            s.claim_invite(actor, invite["token"])
            return True
        except Forbidden:
            return False

    with ThreadPoolExecutor(max_workers=8) as workers:
        assert sum(workers.map(claim, range(4000, 4008))) == 2
    assert s.repo.list_access(OWNER)["invites"][0]["uses"] == 2


async def test_local_cli_does_not_load_env_and_never_rewrites_owner(setup, monkeypatch, capsys):
    from app.access import main
    s = setup.service
    s.repo.bind_runtime("demo", OWNER, adopt_legacy=True)
    monkeypatch.setenv("AI_PROVIDER_FACTORY", "does.not.exist:do_not_load")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "do-not-use")
    monkeypatch.setattr("sys.argv", ["access", "--data-dir", str(s.settings.data_dir),
                                   "create-invite", "--role", "reviewer", "--bot-username", "test_bot"])
    assert main() == 0
    invitation = json.loads(capsys.readouterr().out)
    assert len(invitation["start_parameter"]) == 48
    assert s.repo.runtime_binding() == {"mode": "demo", "owner_id": OWNER}
    monkeypatch.setattr("sys.argv", ["access", "--data-dir", str(s.settings.data_dir), "list"])
    assert main() == 0
    listing = capsys.readouterr().out
    assert invitation["start_parameter"] not in listing and "token_hash" not in listing


async def test_reviewer_reads_workspace_but_never_mutates_or_spends(setup):
    s, business = setup.service, setup.business
    grant(s, VIEWER, "reviewer")
    post = await prepare(setup)
    assert s.get_business(VIEWER, business.id).id == business.id
    assert s.list_posts(VIEWER, business.id)[0].id == post.id
    assert s.read_asset(VIEWER, post.image_asset_id)[1]
    assert s.conversation_history(VIEWER, business.id) == []
    before = s.ai_usage(OWNER)
    for method_name in (
        "create_business", "connect_channel", "add_text_source", "add_pdf_source", "add_url_source",
        "analyze_business", "save_profile", "confirm_profile", "save_manual_profile", "interpret_request",
        "request_preparation", "cancel_job", "set_publications_paused", "retry_notification",
        "upload_post_image", "create_manual_draft", "edit_post", "refresh_post_context", "revise_text",
        "revise_image", "approve_post", "reject_post", "configure_automation", "resolve_unknown_delivery",
    ):
        method = getattr(s, method_name)
        with pytest.raises(Forbidden):
            if inspect.iscoroutinefunction(method):
                await method(VIEWER, business.id)
            else:
                method(VIEWER, business.id)
    assert s.ai_usage(OWNER) == before and not setup.publisher.calls
    other = s.repo.create_business(9999, "Other owner", "UTC", s.now())
    assert other.id not in {b.id for b in s.list_businesses(VIEWER)}
    with pytest.raises(Forbidden):
        s.get_business(VIEWER, other.id)
    s.repo.revoke_access_member(OWNER, VIEWER, s.now())
    with pytest.raises(Forbidden):
        s.read_asset(VIEWER, post.image_asset_id)


async def test_operator_keeps_real_actor_and_canonical_owner(setup):
    s = setup.service
    grant(s, OPERATOR, "operator")
    business = s.create_business(OPERATOR, "Team business")
    assert business.owner_id == OWNER
    post = await prepare(setup)
    approved = s.approve_post(OPERATOR, post.id, post.revision)
    assert approved.approved_by == OPERATOR
    event = s.repo.audit_events("post", post.id)[-1]
    assert event["action"] == "approved" and event["details"]["actor_id"] == OPERATOR
    actions = s.repo.audit_events("workspace_action", "Team business")
    assert actions[-1]["details"]["actor_id"] == OPERATOR
    setup.clock.advance(minutes=2)
    assert await s.publish_due_once()
    assert len(setup.publisher.calls) == 1


async def test_old_operator_invite_cannot_regain_rights_after_fresh_reviewer_grant(setup):
    s = setup.service
    old = grant(s, OPERATOR, "operator")
    post = await prepare(setup)
    s.approve_post(OPERATOR, post.id, post.revision)
    s.repo.revoke_access_member(OWNER, OPERATOR, s.now())
    # Deliberately use the SAME clock: audit sequence must remain decisive.
    grant(s, OPERATOR, "reviewer")
    assert s.access_role(OPERATOR) == "reviewer"
    with pytest.raises(Forbidden):
        s.claim_invite(OPERATOR, old["token"])
    assert s.access_role(OPERATOR) == "reviewer"
    grant(s, OPERATOR, "operator")
    assert not s.repo.approval_allowed(OWNER, s.repo.get_post(post.id))
    setup.clock.advance(minutes=2)
    assert not await s.publish_due_once()
    assert not setup.publisher.calls


@pytest.mark.parametrize("when", ["before_claim", "during_safety", "after_regrant"])
async def test_revoked_operator_approval_never_sends(setup, when):
    s = setup.service
    grant(s, OPERATOR, "operator")
    post = await prepare(setup)
    s.approve_post(OPERATOR, post.id, post.revision)
    setup.clock.advance(minutes=2)
    if when == "during_safety":
        class RevokeDuringCheck:
            def check_text(self, _texts):
                s.repo.revoke_access_member(OWNER, OPERATOR, s.now())

            def check_image(self, *_args):
                pass
        s.safety = RevokeDuringCheck()
    else:
        s.repo.revoke_access_member(OWNER, OPERATOR, s.now())
        if when == "after_regrant":
            setup.clock.advance(seconds=1)
            grant(s, OPERATOR, "operator")
    await s.publish_due_once()
    assert not setup.publisher.calls
    assert s.repo.get_post(post.id).status in {PostStatus.REVIEW, PostStatus.FAILED}


async def test_bot_two_operators_keep_independent_pending_and_selected_state(harness):
    h = harness
    grant(h.service, OPERATOR, "operator")
    first = h.service.create_business(BOT_OWNER, "First")
    second = h.service.create_business(BOT_OWNER, "Second")
    await h.send(button=f"business:{first.id}", actor=BOT_OWNER)
    await h.send(button=f"input:notes:{first.id}", actor=BOT_OWNER)
    await h.send(button=f"business:{second.id}", actor=OPERATOR)
    await h.send(button=f"input:notes:{second.id}", actor=OPERATOR)
    await h.send(text="Second operator note", actor=OPERATOR)
    await h.send(text="First owner note", actor=BOT_OWNER)
    assert [s.text for s in h.service.list_sources(BOT_OWNER, first.id)] == ["First owner note"]
    assert [s.text for s in h.service.list_sources(OPERATOR, second.id)] == ["Second operator note"]


async def test_bot_invited_reviewer_sees_images_and_cannot_forge_write_callbacks(harness):
    h = harness
    business = await h.ready_business()
    post, _ = await h.prepare(business)
    invite = h.service.repo.create_access_invite(
        BOT_OWNER, "reviewer", 1, h.service.now() + timedelta(days=1), h.service.now(),
    )
    await h.send(text="/start join_" + invite["token"], actor=VIEWER)
    assert h.service.access_role(VIEWER) == "reviewer"
    assert "input:name" not in h.buttons and "Наблюдатель" in h.last_text
    await h.send(button=f"business:{business.id}", actor=VIEWER)
    assert all(b.split(":", 1)[0] in {"posts", "jobs", "report", "history", "home"} for b in h.buttons)
    await h.send(button=f"post:{post.id}", actor=VIEWER)
    assert any(isinstance(r, SendPhoto) and r.chat_id == VIEWER for r in h.session.requests)
    calls = h.ai.calls.copy()
    for button in (f"approve:{post.id}:{post.revision}", f"analyze:{business.id}", f"input:channel:{business.id}"):
        await h.send(button=button, actor=VIEWER)
    await h.send(text="Prepare 5 posts", actor=VIEWER)
    assert h.ai.calls == calls and h.service.get_post(BOT_OWNER, post.id).status == PostStatus.REVIEW
    h.service.repo.revoke_access_member(BOT_OWNER, VIEWER, h.service.now())
    before = len([r for r in h.session.requests if isinstance(r, SendPhoto)])
    await h.send(button=f"post:{post.id}", actor=VIEWER)
    assert len([r for r in h.session.requests if isinstance(r, SendPhoto)]) == before


async def test_invited_reviewer_gets_current_inline_and_personal_miniapp_menu(studio):
    from tests.bot.test_miniapp_launch import URL, launch_button

    h = studio
    h.session.failures = 0
    invite = h.service.repo.create_access_invite(
        BOT_OWNER, "reviewer", 1, h.service.now() + timedelta(days=1), h.service.now(),
    )
    await h.send(text="/start join_" + invite["token"], actor=VIEWER)
    assert launch_button(h.messages[-1])[0].web_app.url == URL
    installed = next(r for r in h.session.requests if isinstance(r, SetChatMenuButton))
    assert installed.chat_id == VIEWER and installed.menu_button.web_app.url == URL

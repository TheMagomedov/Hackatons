"""Revocation during a slow external call must prevent its later content writes."""

import asyncio
import threading
from datetime import timedelta

import pytest

from app.shared.errors import Conflict, Forbidden, SafetyUnavailable
from app.shared.models import PostStatus
from tests.core.test_product_controls import prepare
from tests.core.test_product_controls import setup as setup
from tests.core.test_service import OWNER, Gate
from tests.safety.test_boundaries import valid_png

OPERATOR = 2002


def grant(service, role="operator"):
    invitation = service.repo.create_access_invite(
        OWNER, role, 1, service.now() + timedelta(days=1), service.now(),
    )
    service.claim_invite(OPERATOR, invitation["token"])


class SafetyGate:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def wait(self):
        self.entered.set()
        if not self.release.wait(5):
            raise SafetyUnavailable()

    def check_text(self, _texts):
        self.wait()

    def check_image(self, _content, _mime):
        self.wait()


@pytest.mark.parametrize("operation", ["upload", "draft", "edit"])
@pytest.mark.parametrize("new_role", [None, "reviewer"])
async def test_manual_write_revoked_during_safety_keeps_saved_state(setup, operation, new_role):
    service, business = setup.service, setup.business
    grant(service)
    post = await prepare(setup)
    before_posts = service.list_posts(OWNER, business.id)
    before_files = set(service.files.root.iterdir())
    gate = SafetyGate()
    service.safety = gate
    calls = {
        "upload": lambda: service.upload_post_image(OPERATOR, business.id, "new.png", valid_png()),
        "draft": lambda: service.create_manual_draft(
            OPERATOR, business.id, "New manual caption", post.image_asset_id, "revoked-manual",
        ),
        "edit": lambda: service.edit_post(OPERATOR, post.id, post.revision, text="Changed caption"),
    }
    worker = asyncio.create_task(asyncio.to_thread(calls[operation]))
    try:
        assert await asyncio.to_thread(gate.entered.wait, 2)
        service.repo.revoke_access_member(OWNER, OPERATOR, service.now())
        if new_role:
            grant(service, new_role)
    finally:
        gate.release.set()
    with pytest.raises(Forbidden) as error:
        await worker
    assert type(error.value) is Forbidden
    assert service.list_posts(OWNER, business.id) == before_posts
    assert set(service.files.root.iterdir()) == before_files
    assert not setup.publisher.calls


@pytest.mark.parametrize("operation", ["analyze", "revise_text", "revise_image", "interpret"])
async def test_revoked_ai_result_fails_reservation_without_committing_result(setup, operation):
    service, business = setup.service, setup.business
    grant(service)
    post = await prepare(setup)
    gate = Gate()
    if operation == "analyze":
        service.add_text_source(OWNER, business.id, "Business source")
        setup.ai.gates["profile"] = gate
        def execute():
            return service.analyze_business(OPERATOR, business.id, request_id="revoke-operation")
    elif operation == "revise_text":
        setup.ai.gates["revision"] = gate
        def execute():
            return service.revise_text(
                OPERATOR, post.id, post.revision, "Change text", request_id="revoke-operation",
            )
    elif operation == "revise_image":
        setup.images.gate = gate
        def execute():
            return service.revise_image(
                OPERATOR, post.id, post.revision, "Change image", request_id="revoke-operation",
            )
    else:
        async def interpret(_text, _context):
            await gate.wait()
            return setup.ai.intent
        setup.ai.interpret_request = interpret
        def execute():
            return service.interpret_request(
                OPERATOR, business.id, "Create post", request_id="revoke-operation",
            )
    before_business = service.get_business(OWNER, business.id)
    before_files = set(service.files.root.iterdir())
    before_operations = {item["id"] for item in service.list_ai_operations(OWNER, business.id)}
    worker = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(gate.entered.wait(), 2)
        service.repo.revoke_access_member(OWNER, OPERATOR, service.now())
    finally:
        gate.release.set()
    with pytest.raises(Forbidden) as error:
        await worker
    assert type(error.value) is Forbidden
    assert service.get_business(OWNER, business.id) == before_business
    assert set(service.files.root.iterdir()) == before_files
    saved = service.get_post(OWNER, post.id)
    if operation in ("revise_text", "revise_image"):
        assert saved.status == PostStatus.FAILED
        assert saved.revision == post.revision + 1  # Failed reservation, not a successful edit.
        assert saved.approved_revision is None and saved.approved_by is None
        assert saved.text == post.text and saved.image_asset_id == post.image_asset_id
    else:
        assert saved == post
    operations = [item for item in service.list_ai_operations(OWNER, business.id)
                  if item["id"] not in before_operations]
    assert len(operations) == 1
    assert (operations[0]["status"], operations[0]["error_code"]) == ("failed", "forbidden")
    assert service.ai_usage(OWNER)["running"] == 0
    assert not service.conversation_history(OWNER, business.id)
    # Even after a new grant the failed request is not silently retried or reported done.
    grant(service)
    with pytest.raises(Conflict):
        await execute()
    assert service.ai_usage(OWNER)["running"] == 0
    assert not setup.publisher.calls

import asyncio
import io
import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from app.mcp_tools import MCPServer, ToolDispatcher
from app.mcp_tools.__main__ import build_dispatcher
from app.mcp_tools.server import MAX_MESSAGE_BYTES, PROTOCOL_VERSION, parse_message, serve_stdio
from app.shared.errors import Conflict, Forbidden, InvalidInput
from tests.core.test_product_controls import setup  # noqa: F401
from tests.core.test_service import OWNER


def dispatcher(fixture, actor=OWNER, **kwargs):
    return ToolDispatcher(fixture.service, actor, business_ids={fixture.business.id}, **kwargs)


def grant(fixture, actor, role):
    service = fixture.service
    invitation = service.repo.create_access_invite(
        OWNER, role, 1, service.now() + timedelta(days=1), service.now(),
    )
    service.claim_invite(actor, invitation["token"])


def request(method, params=None, identifier=1):
    return {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params or {}}


async def initialized(server):
    result = await server.handle(request("initialize", {
        "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1"},
    }))
    assert result["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert result["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert await server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


async def test_real_queue_is_durable_idempotent_and_has_no_publication_tools(setup):  # noqa: F811
    tools = dispatcher(setup, allow_prepare=True)
    arguments = {"business_id": setup.business.id, "request": "One post", "request_id": "mcp-stable-1"}
    result = await tools.call("studio_request_preparation", arguments)
    assert result["requires_human_approval"] is True
    assert result["job"]["status"] == "pending"
    assert await tools.call("studio_request_preparation", arguments) == result
    assert len(setup.service.list_jobs(OWNER, setup.business.id)) == 1
    assert setup.service.list_posts(OWNER, setup.business.id) == []
    assert not any(any(word in t["name"] for word in ["approve", "publish", "schedule", "shell"])
                   for t in tools.list_tools())
    with pytest.raises(Conflict):
        await tools.call("studio_request_preparation", {**arguments, "request": "Different"})
    with pytest.raises(InvalidInput):
        await tools.call("studio_request_preparation", {**arguments, "actor_id": OWNER})
    with setup.repo._reader() as connection:
        audit = json.dumps([dict(row) for row in connection.execute("SELECT * FROM audit")])
    assert "mcp:studio_request_preparation:completed" in audit
    assert "One post" not in audit


async def test_role_scope_and_revocation_apply_on_every_call(setup):  # noqa: F811
    second = setup.service.create_business(OWNER, "Other company secret")
    grant(setup, 2002, "operator")
    grant(setup, 3003, "reviewer")
    reader = dispatcher(setup, 3003, allow_prepare=True)
    operator = dispatcher(setup, 2002, allow_prepare=True)
    assert "studio_request_preparation" not in {tool["name"] for tool in reader.list_tools()}
    assert [b["id"] for b in (await operator.call("studio_list_businesses", {}))["businesses"]] == [setup.business.id]
    with pytest.raises(Forbidden):
        await operator.call("studio_list_jobs", {"business_id": second.id})
    arguments = {"business_id": setup.business.id, "request": "One post", "request_id": "role-check"}
    with pytest.raises(Forbidden):
        await reader.call("studio_request_preparation", arguments)
    with pytest.raises(Forbidden):
        await dispatcher(setup).call("studio_request_preparation", arguments)
    setup.repo.revoke_access_member(OWNER, 2002, setup.service.now())
    with pytest.raises(Forbidden):
        operator.list_tools()
    with pytest.raises(Forbidden):
        await operator.call("studio_list_businesses", {})
    assert setup.service.list_jobs(OWNER, setup.business.id) == []


async def test_search_rechecks_revoked_access_after_await_and_bounds_results(setup, monkeypatch):  # noqa: F811
    grant(setup, 2002, "operator")
    operator = dispatcher(setup, 2002)

    async def search(actor, business, query, *, limit):
        assert (actor, business, query, limit) == (2002, setup.business.id, "Payment", 4)
        setup.repo.revoke_access_member(OWNER, 2002, setup.service.now())
        return [{"text": "Private contract"}]

    monkeypatch.setattr(setup.service, "search_knowledge", search, raising=False)
    with pytest.raises(Forbidden):
        await operator.call("studio_search_knowledge", {"business_id": setup.business.id, "query": "Payment"})
    with pytest.raises(InvalidInput):
        await dispatcher(setup).call("studio_search_knowledge", {
            "business_id": setup.business.id, "query": "Payment", "limit": True,
        })


async def test_protocol_lifecycle_and_errors_never_echo_private_exception(setup, monkeypatch):  # noqa: F811
    server = MCPServer(dispatcher(setup))
    assert (await server.handle(request("tools/list")))["error"]["code"] == -32002
    await initialized(server)
    tools = (await server.handle(request("tools/list")))["result"]["tools"]
    assert all(t["inputSchema"]["additionalProperties"] is False for t in tools)
    assert (await server.handle(request("publish", {})))["error"]["code"] == -32601
    assert (await server.handle(request("tools/call", {"name": "approve", "arguments": {}})))["error"]["code"] == -32602
    invalid = await server.handle(request("tools/call", {
        "name": "studio_list_posts", "arguments": {"business_id": setup.business.id, "limit": "20"},
    }))
    assert invalid["result"]["isError"] is True

    async def broken(*_args, **_kwargs):
        raise RuntimeError("private password must never be returned")

    monkeypatch.setattr(server.dispatcher, "call", broken)
    error = await server.handle(request("tools/call", {"name": "studio_list_businesses"}))
    assert error["result"]["isError"] is True
    assert "password" not in json.dumps(error)
    assert (await server.handle(request("initialize", {})))["error"]["code"] == -32602
    assert await server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}}) is None


async def test_stdio_masks_sensitive_text_preserves_identifiers_and_outputs_only_json(setup, monkeypatch):  # noqa: F811
    uid = "4de48c2c-1027-4699-b24a-a9506ff95b60"
    tools = dispatcher(setup)

    async def answer(*_args):
        return {"matches": [{"source_id": uid, "text": "Contact person@example.org"}]}

    monkeypatch.setattr(tools, "call", answer)
    messages = [request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                      "clientInfo": {"name": "test", "version": "1"}}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                request("tools/call", {"name": "studio_search_knowledge", "arguments": {}}, 2)]
    source = io.BytesIO("\n".join(json.dumps(m) for m in messages).encode())
    output = io.BytesIO()
    await serve_stdio(MCPServer(tools), source, output)
    lines = output.getvalue().decode("utf-8").splitlines()
    assert len(lines) == 2
    result = json.loads(lines[1])["result"]
    assert "person@example.org" not in lines[1]
    assert "[EMAIL]" in lines[1] and uid in lines[1]
    assert result["structuredContent"]["matches"][0]["source_id"] == uid


@pytest.mark.parametrize("payload", [b'{"method":"one","method":"two"}', b'{"x":NaN}', b'\xff'])
def test_wire_parser_rejects_ambiguous_or_non_utf8_json(payload):
    with pytest.raises((ValueError, UnicodeError)):
        parse_message(payload)


async def test_oversized_frame_terminates_instead_of_executing_its_tail(setup):  # noqa: F811
    wire = b" " * (MAX_MESSAGE_BYTES + 2) + b'\n{"jsonrpc":"2.0","id":99,"method":"ping"}\n'
    output = io.BytesIO()
    await serve_stdio(MCPServer(dispatcher(setup)), io.BytesIO(wire), output)
    results = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(results) == 1 and results[0]["error"]["code"] == -32700


@pytest.mark.parametrize("message", [[], None, {}, {"jsonrpc": "2.0", "id": None, "method": "ping"},
                                     {"jsonrpc": "2.0", "id": True, "method": "ping"},
                                     {"jsonrpc": "2.0", "id": "\ud800", "method": "ping"}])
async def test_bad_wire_envelopes_have_protocol_errors(setup, message):  # noqa: F811
    result = await MCPServer(dispatcher(setup)).handle(message)
    assert result["error"]["code"] == -32600


async def test_local_cli_reuses_bound_database_without_providers_or_recovery(setup, monkeypatch):  # noqa: F811
    setup.repo.bind_runtime("demo", OWNER, adopt_legacy=True)
    monkeypatch.setattr("app.core.runtime.build_runtime", lambda *_: pytest.fail("Must not build live runtime"))
    companion = build_dispatcher(setup.service.settings, business_ids={setup.business.id}, allow_prepare=True)
    try:
        response = await companion.call("studio_request_preparation", {
            "business_id": setup.business.id, "request": "Persisted request", "request_id": "cross-process",
        })
        assert setup.repo.get_job(response["job"]["id"]).status == "pending"
        assert len(setup.service.list_jobs(OWNER, setup.business.id)) == 1
        assert setup.repo.runtime_binding() == {"mode": "demo", "owner_id": OWNER}
        source = setup.service.add_text_source(OWNER, setup.business.id, "Payment is due within 17 days.")
        result = await companion.call("studio_search_knowledge", {
            "business_id": setup.business.id, "query": "Payment deadline",
        })
        assert result["matches"] and result["matches"][0]["source_id"] == source.id
        assert "17" in result["matches"][0]["text"]
        companion.service.telemetry.trace({"business_id": setup.business.id, "operation_id": "operation-test",
                                           "kind": "interpret", "profile_version": 1}, "completed")
        traces = await companion.call("studio_traces", {"business_id": setup.business.id})
        assert traces["traces"][0]["event"] == "completed"
        assert source.text not in json.dumps(traces)
    finally:
        companion.service.telemetry.close()
        companion.service.knowledge.close()
        companion.service.repo.close()


def test_module_is_real_stdio_process_and_has_no_publication_capability(tmp_path):
    from app.config import Settings
    from app.core.runtime import build_runtime

    settings = Settings(owner_id=1001, data_dir=tmp_path / "data")

    async def create():
        runtime = await build_runtime(settings)
        business = runtime.service.create_business(1001, "MCP test company")
        await runtime.close()
        return business.id

    business = asyncio.run(create())
    env_file = tmp_path / "fixture.env"
    env_file.write_text(f"APP_MODE=demo\nOWNER_TELEGRAM_ID=1001\nDATA_DIR={settings.data_dir.as_posix()}\n",
                        encoding="utf-8")
    messages = [request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                      "clientInfo": {"name": "client", "version": "1"}}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                request("tools/call", {"name": "studio_list_businesses", "arguments": {}}, 2)]
    env = {k: v for k, v in os.environ.items() if k.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOME",
    }}
    env["PYTHONUTF8"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "app.mcp_tools", "--env-file", str(env_file), "--business-id", business],
        cwd=Path(__file__).parents[2], env=env, input="\n".join(json.dumps(m) for m in messages).encode(),
        capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(replies) == 2 and replies[1]["result"]["isError"] is False
    assert replies[1]["result"]["structuredContent"]["businesses"][0]["id"] == business

"""Qualify native CLI flags against a loopback receiver, with a dummy key only.

Does not load .env, contact AgentRouter or open the application database.
Usage: python scripts/check_agentrouter_cli.py --codex /path/to/codex.exe
"""

import argparse
import asyncio
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai.agentrouter import BASE_URL, AgentRouterClient, AgentRouterSettings  # noqa: E402
from app.ai.client import SYSTEM  # noqa: E402
from app.shared.errors import ProviderUnavailable  # noqa: E402


async def check(executable):
    key = "dummy-cli-contract-check-not-a-real-key"
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(size)) if 0 < size < 1_000_000 else {}
            captured.append({
                "path": self.path, "authorization_matches": self.headers.get("Authorization") == "Bearer " + key,
                "model": data.get("model"), "reasoning": data.get("reasoning", {}).get("effort"),
                "store": data.get("store"), "tools": data.get("tools", []),
                "schema": data.get("text", {}).get("format", {}).get("type"),
                "system_policy_present": SYSTEM in json.dumps(data, ensure_ascii=False),
            })
            body = b'{"error":{"message":"LOCAL_CONTRACT_CHECK_COMPLETE"}}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = AgentRouterClient(executable, AgentRouterSettings(
        api_key=key, model="gpt-6.1-sol", base_url=BASE_URL, wire_api="responses", reasoning_effort="low",
    ))
    try:
        with tempfile.TemporaryDirectory(prefix="studio-cli-contract-") as directory:
            root = Path(directory)
            env = client._environment(root)
            work = root / "work"
            work.mkdir()
            shape = root / "schema.json"
            shape.write_text(json.dumps({"type": "object", "properties": {"ok": {"type": "boolean"}},
                                         "required": ["ok"], "additionalProperties": False}), encoding="utf-8")
            shape.with_name("instructions.txt").write_text(SYSTEM, encoding="utf-8")
            try:
                async with asyncio.timeout(25):
                    await client._run(client._arguments(shape, base_url=f"http://127.0.0.1:{server.server_port}/v1"),
                                      cwd=work, env=env, prompt='Return JSON: {"ok":true}',
                                      capture=True, max_output_bytes=100_000)
            except ProviderUnavailable:
                pass  # The local receiver deliberately refuses generation.
        ok = len(captured) == 1 and all(
            r["path"] == "/v1/responses" and r["authorization_matches"] and not r["tools"]
            and r["model"] == "gpt-6.1-sol" and r["reasoning"] == "low" and r["store"] is False
            and r["schema"] == "json_schema" and r["system_policy_present"] for r in captured
        )
        print(json.dumps({"ok": ok, "requests": captured}, ensure_ascii=False))
        return 0 if ok else 1
    finally:
        await client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", required=True)
    raise SystemExit(asyncio.run(check(parser.parse_args().codex)))

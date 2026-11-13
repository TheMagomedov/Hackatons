"""Read-only local health probe: database plus HTTP event loop when enabled."""

import json
import os
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

from app.storage import Repository


def check() -> None:
    path = Path(os.environ.get("DATA_DIR", "var")) / "backend.sqlite3"
    if not path.is_file():
        raise RuntimeError("Database is not initialized")
    repo = Repository(path)
    try:
        if not repo.diagnostics().get("binding"):
            raise RuntimeError("Database has no runtime binding")
    finally:
        repo.close()
    if os.environ.get("MINI_APP_URL", "").strip():
        port = int(os.environ.get("MINI_APP_PORT", "8766"))
        # Never route a local probe through proxy credentials from the environment.
        with build_opener(ProxyHandler({})).open(
            f"http://127.0.0.1:{port}/mini/health", timeout=5
        ) as response:
            body = json.loads(response.read(1024))
            if response.status != 200 or body.get("status") != "ok":
                raise RuntimeError("Mini App is not healthy")


if __name__ == "__main__":
    try:
        check()
    except Exception:
        print("Local service health check failed")
        raise SystemExit(1) from None
    print("Local service health check passed; external providers are not probed")

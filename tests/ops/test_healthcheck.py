import pytest

from app.ops.healthcheck import check
from app.storage import Repository


def test_healthcheck_reads_database_without_creating_or_migrating(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MINI_APP_URL", raising=False)
    with pytest.raises(RuntimeError, match="not initialized"):
        check()
    assert not (tmp_path / "backend.sqlite3").exists()
    repo = Repository(tmp_path / "backend.sqlite3")
    repo.initialize()
    repo.bind_runtime("demo", 1)
    repo.close()
    check()


def test_healthcheck_does_not_call_external_providers(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MINI_APP_URL", "https://example.test/mini/")
    monkeypatch.setenv("MINI_APP_PORT", "8766")
    repo = Repository(tmp_path / "backend.sqlite3")
    repo.initialize()
    repo.bind_runtime("demo", 1)
    repo.close()

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            assert size == 1024
            return b'{"status":"ok"}'

    class Opener:
        def open(self, url, timeout):
            assert url == "http://127.0.0.1:8766/mini/health" and timeout == 5
            return Response()

    monkeypatch.setattr("app.ops.healthcheck.build_opener", lambda *args: Opener())
    check()

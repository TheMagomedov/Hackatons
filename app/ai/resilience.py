"""Private inference cache, explicit provider routing and metadata-only traces.

No publication or image request is retried here. Cached values are private,
bounded, scoped to a company and validated again before returning them.
"""

import hashlib
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from app.shared.errors import AppError, ProviderUnavailable
from app.storage.files import FileStore

CALL_CONTEXT = ContextVar("ai_call_context", default=None)
log = logging.getLogger(__name__)


@contextmanager
def inference_scope(business_id, operation_id, kind, profile_version):
    token = CALL_CONTEXT.set({"business_id": business_id, "operation_id": operation_id,
                              "kind": kind, "profile_version": profile_version})
    try:
        yield
    finally:
        CALL_CONTEXT.reset(token)


class InferenceStore:
    def __init__(self, path, *, ttl_seconds=900, max_entries=256, max_traces=10000, clock=time.time):
        if (type(ttl_seconds) is not int or not 0 <= ttl_seconds <= 86400
                or type(max_entries) is not int or not 1 <= max_entries <= 10000
                or type(max_traces) is not int or not 1 <= max_traces <= 100000):
            raise ValueError("Inference cache and trace retention require bounded integer limits")
        self.path = Path(path)
        FileStore._check_components(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self.max_traces = max_traces
        self.clock = clock
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS inference_cache (
                    cache_key TEXT PRIMARY KEY, business_id TEXT NOT NULL,
                    result TEXT NOT NULL, model TEXT NOT NULL, expires REAL NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inference_traces (
                    id TEXT PRIMARY KEY, business_id TEXT NOT NULL, operation_id TEXT NOT NULL,
                    kind TEXT NOT NULL, event TEXT NOT NULL, model TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL, created REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS trace_company ON inference_traces(business_id, created);
            """)

    @contextmanager
    def connection(self):
        FileStore._check_components(self.path)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, key, business_id):
        if self.ttl <= 0:
            return None
        with self.connection() as db:
            row = db.execute("SELECT result,model FROM inference_cache WHERE cache_key=? "
                             "AND business_id=? AND expires>?", (key, business_id, self.clock())).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["result"]), row["model"]
        except (ValueError, TypeError):
            return None

    def put(self, key, business_id, result, model):
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        if self.ttl <= 0 or len(encoded.encode("utf-8")) > 100_000:
            return
        now = self.clock()
        with self.connection() as db:
            db.execute("DELETE FROM inference_cache WHERE expires<=?", (now,))
            db.execute("INSERT OR REPLACE INTO inference_cache VALUES(?,?,?,?,?,?)",
                       (key, business_id, encoded, model, now + self.ttl, now))
            db.execute("DELETE FROM inference_cache WHERE cache_key IN "
                       "(SELECT cache_key FROM inference_cache ORDER BY created DESC LIMIT -1 OFFSET ?)",
                       (self.max_entries,))

    def trace(self, scope, event, model="", duration_ms=0):
        # Closed vocabulary: no exception bodies, prompt text, headers or user-supplied metadata.
        if event not in {"started", "completed", "failed", "cache_hit", "model_ok", "model_failed", "fallback"}:
            raise ValueError("Unknown trace event")
        if scope is None:
            return
        with self.connection() as db:
            db.execute("INSERT INTO inference_traces VALUES(?,?,?,?,?,?,?,?)",
                       (uuid4().hex, scope["business_id"], scope["operation_id"], scope["kind"],
                        event, model, max(0, int(duration_ms)), self.clock()))
            db.execute("DELETE FROM inference_traces WHERE id IN "
                       "(SELECT id FROM inference_traces ORDER BY created DESC LIMIT -1 OFFSET ?)",
                       (self.max_traces,))

    def recent(self, business_id, *, limit=30):
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM inference_traces WHERE business_id=? ORDER BY created DESC LIMIT ?",
                (business_id, min(100, max(1, limit))),
            )]

    def close(self):
        pass  # Connections are short lived; nothing is held across model calls.


class InferenceRouter:
    def __init__(self, clients, store):
        if not 1 <= len(clients) <= 2:
            raise ValueError("Use one primary and at most one explicitly configured fallback")
        self.clients = clients
        self.store = store

    async def close(self):
        for client in self.clients:
            await client.close()

    def _optional_store(self, operation, *args, **kwargs):
        """Derived metadata must not turn a successful inference into a failed task.

        Domain/security errors and cancellation still propagate. Only optional
        cache/trace persistence is contained; model calls and validators are not.
        """
        try:
            return getattr(self.store, operation)(*args, **kwargs)
        except AppError:
            raise
        except Exception:
            log.warning("Optional AI cache/trace storage unavailable")
            return None

    async def complete(self, schema, instruction, data, validate=None):
        from app.ai.client import SYSTEM
        from app.ai.local import _COMPACT, output_schema
        from app.safety.input_guard import guard_payload

        guard_payload(data)  # A cache hit must not bypass the current input policy.

        scope = CALL_CONTEXT.get()
        identity = [(c.settings.model, c.settings.base_url, c.settings.model_dump(mode="json"))
                    for c in self.clients]
        encoded = json.dumps({"policy": SYSTEM, "local_policy": _COMPACT.get(schema.__name__, ""),
                              "models": identity, "schema": output_schema(schema),
                              "instruction": instruction, "data": data,
                              "company": scope["business_id"] if scope else None,
                              "profile_version": scope["profile_version"] if scope else None},
                             sort_keys=True, ensure_ascii=False, allow_nan=False)
        key = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if scope:
            cached = self._optional_store("get", key, scope["business_id"])
            if cached:
                try:
                    result = schema.model_validate(cached[0])
                    if validate is None or validate(result):
                        self._optional_store("trace", scope, "cache_hit", cached[1])
                        return result
                except (ValidationError, ValueError, TypeError):
                    pass
        for index, client in enumerate(self.clients):
            started = time.monotonic()
            if index:
                self._optional_store("trace", scope, "fallback", client.settings.model)
            try:
                result = await client.complete(schema, instruction, data, validate)
            except ProviderUnavailable:
                self._optional_store("trace", scope, "model_failed", client.settings.model,
                                     (time.monotonic() - started) * 1000)
                if index == len(self.clients) - 1:
                    raise
            else:
                self._optional_store("trace", scope, "model_ok", client.settings.model,
                                     (time.monotonic() - started) * 1000)
                if scope:
                    self._optional_store("put", key, scope["business_id"], result.model_dump(mode="json"),
                                         client.settings.model)
                return result
        raise ProviderUnavailable("Настроенные модели недоступны. Задача сохранена.")


# Preserve the existing local integration import; runtime chooses fallback policy.
LocalInferenceRouter = InferenceRouter

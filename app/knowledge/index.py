"""Persistent local chunks and bounded, business-scoped BM25 retrieval.

No embeddings, model calls or network requests occur during search. Ingestion
uses the existing bounded source extractor; only explicitly supplied URL
sources can cause a network read. Authentication belongs to the service layer.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import threading
import unicodedata
from collections import Counter
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.ai.sources import ExtractedSource, extract_source
from app.shared.errors import InvalidInput
from app.shared.models import SourceInput

Extractor = Callable[[SourceInput], Awaitable[ExtractedSource]]
INDEX_VERSION = "local-bm25-v1"
MAX_SOURCES = 10
MAX_SOURCE_CHARS = 20_000
MAX_TOTAL_CHARS = 120_000
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 180
MAX_QUERY_CHARS = 10_000
_STOP = frozenset("и в во на к ко от до по из с со за о об для а но или это как какой какие какая сколько "
                  "ли что кто где когда чем при без у не если то the a an is are of in on to for and or "
                  "what which how many please мне нам нас ваш наши наш про расскажи скажи".split())
# Conservative inflection reduction, not a morphological model. Exact numbers
# and short tokens are kept; callers should not treat a match as factual proof.
_RU_ENDINGS = tuple(sorted(("иями", "ями", "ами", "ого", "ему", "ому", "ыми", "ими", "ией", "иям",
                           "иях", "иях", "ение", "ения", "ению", "ений", "ением", "ии", "ий", "ый", "ой",
                           "ая", "яя", "ое", "ее", "ые", "ие", "ов", "ев", "ам", "ям", "ах", "ях",
                           "ом", "ем", "ей", "ия", "ию", "а", "я", "ы", "и", "у", "ю", "е", "о"),
                          key=len, reverse=True))


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or any(ord(c) < 32 for c in value):
        raise InvalidInput("Некорректный идентификатор базы знаний.")
    return value


def _terms(text: str) -> list[str]:
    tokens = re.findall(r"[\w]+", unicodedata.normalize("NFKC", text).casefold().replace("ё", "е"))
    result = []
    for token in tokens:
        if token in _STOP or len(token) < 2 or len(token) > 80:
            continue
        if re.fullmatch(r"[а-я]+", token):
            for ending in _RU_ENDINGS:
                if token.endswith(ending) and len(token) - len(ending) >= 4:
                    token = token[:-len(ending)]
                    break
        result.append(token)
    return result


def _chunks(text: str) -> list[str]:
    result, start = [], 0
    while start < len(text):
        end = min(len(text), start + CHUNK_CHARS)
        if end < len(text):
            boundary = max(text.rfind("\n", start + CHUNK_CHARS // 2, end),
                           text.rfind(". ", start + CHUNK_CHARS // 2, end))
            if boundary > start:
                end = boundary + 1
        part = text[start:end].strip()
        if part:
            result.append(part)
        if end == len(text):
            break
        start = max(start + 1, end - CHUNK_OVERLAP)
    return result


def _input_fingerprint(item: SourceInput) -> str:
    metadata = item.source.model_dump(mode="json", exclude={"created_at"})
    digest = hashlib.sha256(json.dumps(metadata, sort_keys=True, ensure_ascii=False).encode())
    digest.update(b"\0")
    digest.update(item.content or b"")
    digest.update(INDEX_VERSION.encode())
    return digest.hexdigest()


@dataclass(frozen=True)
class KnowledgeHit:
    source_id: str
    source_name: str
    chunk_id: str
    text: str
    score: float

    def as_context(self) -> dict:
        return {"source_id": self.source_id, "source_name": self.source_name, "chunk_id": self.chunk_id,
                "text": self.text, "untrusted": True}


@dataclass(frozen=True)
class IndexStats:
    sources: int
    chunks: int
    indexed: int
    fingerprint: str


class KnowledgeIndex:
    def __init__(self, path: str | Path, *, extractor: Extractor = extract_source):
        self.path = Path(os.path.abspath(path))
        self._check_path()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._check_path()
        self.extractor = extractor
        self._lock = threading.RLock()
        self._index_lock = asyncio.Lock()
        self._db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS rag_sources (
                business_id TEXT NOT NULL, source_id TEXT NOT NULL,
                source_name TEXT NOT NULL, kind TEXT NOT NULL,
                input_hash TEXT NOT NULL, content_hash TEXT NOT NULL,
                text_length INTEGER NOT NULL, indexed_at TEXT NOT NULL,
                PRIMARY KEY (business_id, source_id)
            );
            CREATE TABLE IF NOT EXISTS rag_chunks (
                business_id TEXT NOT NULL, source_id TEXT NOT NULL,
                chunk_id TEXT NOT NULL, ordinal INTEGER NOT NULL, text TEXT NOT NULL,
                PRIMARY KEY (business_id, chunk_id),
                FOREIGN KEY (business_id, source_id) REFERENCES rag_sources(business_id, source_id)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS rag_chunk_source ON rag_chunks(business_id, source_id);
        """)

    def _check_path(self) -> None:
        for path in [*reversed(self.path.parents), self.path]:
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise InvalidInput("Ссылки и точки перенаправления в пути базы знаний запрещены.")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _allowed(values: Collection[str] | None) -> set[str] | None:
        if values is None:
            return None
        if not isinstance(values, Collection) or isinstance(values, (str, bytes)) or len(values) > MAX_SOURCES:
            raise InvalidInput("Неверный список разрешённых источников.")
        return {_identifier(value) for value in values}

    def _source_rows(self, business_id: str, allowed_source_ids=None) -> list[sqlite3.Row]:
        business_id = _identifier(business_id)
        allowed = self._allowed(allowed_source_ids)
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM rag_sources WHERE business_id=? ORDER BY source_id", (business_id,),
            ).fetchall()
        return [row for row in rows if allowed is None or row["source_id"] in allowed]

    def fingerprint(self, business_id: str, *, allowed_source_ids: Collection[str] | None = None) -> str:
        rows = self._source_rows(business_id, allowed_source_ids)
        material = [INDEX_VERSION, business_id, [(row["source_id"], row["input_hash"], row["content_hash"])
                                               for row in rows]]
        return hashlib.sha256(json.dumps(material, ensure_ascii=False).encode()).hexdigest()

    def status(self, business_id: str, *, allowed_source_ids: Collection[str] | None = None) -> IndexStats:
        rows = self._source_rows(business_id, allowed_source_ids)
        ids = {row["source_id"] for row in rows}
        with self._lock:
            count = sum(row["count"] for row in self._db.execute(
                "SELECT source_id,count(*) AS count FROM rag_chunks WHERE business_id=? GROUP BY source_id",
                (business_id,),
            ) if row["source_id"] in ids)
        return IndexStats(len(rows), count, 0, self.fingerprint(business_id, allowed_source_ids=ids))

    async def index_sources(
        self, business_id: str, sources: list[SourceInput], *, force: bool = False,
    ) -> IndexStats:
        """Atomically replace a complete business snapshot after all extraction succeeds.

        Removed sources disappear. URL content is cached until ``force=True``
        or source metadata changes. A failed refresh leaves the old snapshot
        intact and raises; callers must not silently answer from it as current.
        """
        _identifier(business_id)
        if not isinstance(sources, list) or len(sources) > MAX_SOURCES or type(force) is not bool:
            raise InvalidInput("База знаний принимает не более 10 источников за один снимок.")
        for item in sources:
            if not isinstance(item, SourceInput) or item.source.business_id != business_id:
                raise InvalidInput("Источники базы знаний должны принадлежать одной выбранной компании.")
            _identifier(item.source.id)
            if not 1 <= len(item.source.name) <= 255:
                raise InvalidInput("Некорректное название источника базы знаний.")
        if len({item.source.id for item in sources}) != len(sources):
            raise InvalidInput("Идентификаторы источников базы знаний должны быть уникальными.")
        async with self._index_lock:
            old = {row["source_id"]: row for row in self._source_rows(business_id)}
            updates, total = [], 0
            for item in sources:
                source_id, input_hash = item.source.id, _input_fingerprint(item)
                previous = old.get(source_id)
                if not force and previous is not None and previous["input_hash"] == input_hash:
                    total += previous["text_length"]
                else:
                    extracted = await self.extractor(item)
                    if (extracted.id != source_id or not isinstance(extracted.text, str)
                            or not extracted.text.strip() or len(extracted.text) > MAX_SOURCE_CHARS):
                        raise InvalidInput("Извлечённый документ пуст, слишком велик или имеет неверный ID.")
                    text = extracted.text.strip()
                    total += len(text)
                    updates.append((item, input_hash, hashlib.sha256(text.encode()).hexdigest(), text))
                if total > MAX_TOTAL_CHARS:
                    raise InvalidInput("Общий текст базы знаний превышает 120000 символов.")
            wanted = {item.source.id for item in sources}
            now = datetime.now(UTC).isoformat()
            with self._lock, self._db:
                for source_id in old.keys() - wanted:
                    self._db.execute("DELETE FROM rag_sources WHERE business_id=? AND source_id=?",
                                     (business_id, source_id))
                for item, input_hash, content_hash, text in updates:
                    source = item.source
                    self._db.execute("DELETE FROM rag_sources WHERE business_id=? AND source_id=?",
                                     (business_id, source.id))
                    self._db.execute("INSERT INTO rag_sources VALUES (?,?,?,?,?,?,?,?)",
                                     (business_id, source.id, source.name, source.kind, input_hash, content_hash,
                                      len(text), now))
                    for ordinal, chunk in enumerate(_chunks(text), 1):
                        chunk_id = f"{source.id}:{content_hash[:16]}:{ordinal}"
                        self._db.execute("INSERT INTO rag_chunks VALUES (?,?,?,?,?)",
                                         (business_id, source.id, chunk_id, ordinal, chunk))
            state = self.status(business_id)
            return IndexStats(state.sources, state.chunks, len(updates), state.fingerprint)

    def search(
        self, business_id: str, query: str, *, limit: int = 4,
        allowed_source_ids: Collection[str] | None = None, max_context_chars: int = 6000,
    ) -> list[KnowledgeHit]:
        _identifier(business_id)
        allowed = self._allowed(allowed_source_ids)
        if (not isinstance(query, str) or len(query) > MAX_QUERY_CHARS
                or type(limit) is not int or not 1 <= limit <= 8
                or type(max_context_chars) is not int or not 1 <= max_context_chars <= 10_000):
            raise InvalidInput("Неверные параметры поиска по базе знаний.")
        query_terms = list(dict.fromkeys(_terms(query)))[:32]
        if not query_terms or allowed == set():
            return []
        with self._lock:
            rows = self._db.execute("""
                SELECT c.source_id,c.chunk_id,c.text,c.ordinal,s.source_name
                FROM rag_chunks c JOIN rag_sources s
                  ON s.business_id=c.business_id AND s.source_id=c.source_id
                WHERE c.business_id=? ORDER BY c.source_id,c.ordinal
            """, (business_id,)).fetchall()
        rows = [row for row in rows if allowed is None or row["source_id"] in allowed]
        if not rows:
            return []
        documents = [Counter(_terms(row["text"])) for row in rows]
        lengths = [sum(document.values()) for document in documents]
        average = sum(lengths) / len(lengths) or 1
        frequencies = {term: sum(term in document for document in documents) for term in query_terms}
        ranked = []
        for row, counts, length in zip(rows, documents, lengths, strict=True):
            score = 0.0
            for term in query_terms:
                frequency = counts[term]
                if frequency:
                    inverse = math.log(1 + (len(rows) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
                    score += inverse * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * length / average))
            if score > 0:
                ranked.append((score, row))
        ranked.sort(key=lambda item: (-item[0], item[1]["source_id"], item[1]["ordinal"]))
        hits, used = [], 0
        for score, row in ranked:
            if len(hits) >= limit or used >= max_context_chars:
                break
            text = row["text"][:max_context_chars - used]
            used += len(text)
            hits.append(KnowledgeHit(row["source_id"], row["source_name"], row["chunk_id"], text, round(score, 6)))
        return hits

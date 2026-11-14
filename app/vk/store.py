"""Durable VK receipts and explicit cross-platform account linking.

Raw link/launch/session secrets are never stored. A VK ID alone is never an
application actor. Removing a link invalidates every associated web session.
"""

import hashlib
import secrets
import sqlite3
import time
from pathlib import Path

from app.shared.errors import Conflict, Forbidden


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class VKStore:
    def __init__(self, path: Path, group_id: int):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS binding(group_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS cursor(value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS links(
                user_id INTEGER PRIMARY KEY, actor INTEGER NOT NULL UNIQUE, business TEXT);
            CREATE TABLE IF NOT EXISTS tickets(
                hash TEXT PRIMARY KEY, kind TEXT NOT NULL, actor INTEGER NOT NULL,
                user_id INTEGER, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS inbox(
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE NOT NULL,
                user_id INTEGER NOT NULL, text TEXT NOT NULL, actor INTEGER,
                kind TEXT NOT NULL DEFAULT 'message', state TEXT NOT NULL DEFAULT 'pending',
                created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS inbox_pending ON inbox(state, id);
        """)
        row = self.db.execute("SELECT group_id FROM binding").fetchone()
        if row and row[0] != group_id:
            self.db.close()
            raise Conflict("База ВК уже привязана к другому сообществу.")
        with self.db:
            if not row:
                self.db.execute("INSERT INTO binding VALUES (?)", (group_id,))
            # Do not repeat a possibly sent message or an interrupted mutation.
            self.db.execute("UPDATE inbox SET state='interrupted',text='' WHERE state='running'")

    def close(self):
        self.db.close()

    def cursor(self, value: str | None = None) -> str | None:
        if value is not None:
            if not value.isdigit() or len(value) > 30:
                raise ValueError("Invalid polling cursor")
            with self.db:
                self.db.execute("DELETE FROM cursor")
                self.db.execute("INSERT INTO cursor VALUES (?)", (value,))
        row = self.db.execute("SELECT value FROM cursor").fetchone()
        return row[0] if row else None

    def actor(self, user_id: int) -> int | None:
        row = self.db.execute("SELECT actor FROM links WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row else None

    def seen(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM inbox WHERE event_key=?", (key,)).fetchone() is not None

    def user(self, actor: int) -> int | None:
        row = self.db.execute("SELECT user_id FROM links WHERE actor=?", (actor,)).fetchone()
        return row[0] if row else None

    def issue(self, kind: str, actor: int, user_id: int | None = None) -> str:
        if kind not in {"link", "launch", "session"}:
            raise ValueError("Unknown ticket kind")
        now, token = time.time(), secrets.token_urlsafe(32)
        lifetime = {"link": 600, "launch": 300, "session": 3600}[kind]
        with self.db:
            self.db.execute("DELETE FROM tickets WHERE expires<?", (now,))
            if kind == "link":
                self.db.execute("DELETE FROM tickets WHERE actor=? AND kind='link'", (actor,))
            self.db.execute("INSERT INTO tickets VALUES (?,?,?,?,?)",
                            (digest(token), kind, actor, user_id, now + lifetime))
        return token

    def claim_link(self, user_id: int, token: str, authorize) -> int:
        with self.db:
            row = self.db.execute("SELECT * FROM tickets WHERE hash=? AND kind='link' AND expires>?",
                                  (digest(token), time.time())).fetchone()
            if row is None:
                raise Forbidden("Код недействителен или истёк. Получите новый через /link_vk в Telegram.")
            authorize(row["actor"])
            current_actor = self.actor(user_id)
            current_user = self.user(row["actor"])
            if (current_actor is not None and current_actor != row["actor"]) or (
                current_user is not None and current_user != user_id
            ):
                raise Conflict("Аккаунт уже связан. Сначала выполните /unlink_vk в Telegram.")
            self.db.execute("INSERT OR IGNORE INTO links(user_id,actor) VALUES (?,?)", (user_id, row["actor"]))
            self.db.execute("DELETE FROM tickets WHERE hash=?", (digest(token),))
            return row["actor"]

    def unlink(self, actor: int):
        with self.db:
            self.db.execute("DELETE FROM links WHERE actor=?", (actor,))
            self.db.execute("DELETE FROM tickets WHERE actor=?", (actor,))

    def authenticate(self, kind: str, token: str, authorize, *, consume=False) -> tuple[int, int]:
        if not isinstance(token, str) or len(token) != 43:
            raise Forbidden("Откройте новую ссылку «Открыть студию» в боте ВК.")
        with self.db:
            row = self.db.execute("""SELECT t.actor,t.user_id FROM tickets t JOIN links l
                ON l.actor=t.actor AND l.user_id=t.user_id
                WHERE t.hash=? AND t.kind=? AND t.expires>?""", (digest(token), kind, time.time())).fetchone()
            if row is None:
                raise Forbidden("Ссылка или сеанс истекли. Откройте студию заново из бота ВК.")
            authorize(row["actor"])
            if consume:
                self.db.execute("DELETE FROM tickets WHERE hash=?", (digest(token),))
            return row["actor"], row["user_id"]

    def selected(self, user_id: int, business: str | None = None) -> str | None:
        if business is not None:
            with self.db:
                self.db.execute("UPDATE links SET business=? WHERE user_id=?", (business, user_id))
        row = self.db.execute("SELECT business FROM links WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row else None

    def enqueue(self, key: str, user_id: int, text: str, *, actor=None, kind="message") -> bool:
        now = time.time()
        with self.db:
            # Retain IDs for replay protection; erase processed message content immediately.
            self.db.execute("DELETE FROM inbox WHERE created<? AND state NOT IN ('pending','running')",
                            (now - 7 * 86400,))
            if self.db.execute("SELECT 1 FROM inbox WHERE event_key=?", (key,)).fetchone():
                return False
            if self.db.execute("SELECT COUNT(*) FROM inbox WHERE state='pending'").fetchone()[0] >= 1000:
                raise Conflict("Очередь ВК заполнена.")
            count = self.db.execute("SELECT COUNT(*) FROM inbox WHERE user_id=? AND created>?",
                                    (user_id, now - 60)).fetchone()[0]
            if count >= 15:
                return False
            self.db.execute("INSERT INTO inbox(event_key,user_id,text,actor,kind,created) VALUES (?,?,?,?,?,?)",
                            (key, user_id, text, actor, kind, now))
            return True

    def take(self) -> dict | None:
        with self.db:
            row = self.db.execute("SELECT * FROM inbox WHERE state='pending' ORDER BY id LIMIT 1").fetchone()
            if row:
                self.db.execute("UPDATE inbox SET state='running' WHERE id=?", (row["id"],))
                return dict(row)
        return None

    def finish(self, item_id: int, state="done"):
        with self.db:
            self.db.execute("UPDATE inbox SET state=?,text='' WHERE id=?", (state, item_id))

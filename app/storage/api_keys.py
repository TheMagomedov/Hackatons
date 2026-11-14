"""Company-scoped, expiring, hash-only integration credentials."""

import hashlib
import json
import secrets
from datetime import timedelta
from uuid import uuid4

from app.shared.errors import Conflict, Forbidden, InvalidInput, RateLimited


class APIKeyRepository:
    def company_ai_usage(self, business_id, now):
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with self._reader() as conn:
            business = self._business(conn, business_id)
            used = conn.execute('SELECT COUNT(*) FROM ai_operations WHERE business_id=? AND created_at>=? AND created_at<?',
                (business_id, start.isoformat(timespec='microseconds'),
                 (start+timedelta(days=1)).isoformat(timespec='microseconds'))).fetchone()[0]
            return {'used_today': used, 'daily_limit': business.ai_daily_limit, 'period': 'UTC'}

    def set_company_ai_limit(self, business_id, actor_id, limit, now):
        if limit is not None and (type(limit) is not int or not 0 <= limit <= 10000):
            raise InvalidInput('Лимит: целое число от 0 до 10000 или общий лимит.')
        with self._transaction() as conn:
            business = self._business(conn, business_id)
            if business.owner_id != actor_id or business.archived:
                raise Forbidden('Лимит задаёт владелец действующей компании.')
            self._save_business(conn, business.model_copy(update={'ai_daily_limit': limit}))
            self._audit(conn, 'business', business_id, 'ai_limit_changed', now, limit=limit)

    @staticmethod
    def initialize_api_keys(conn):
        conn.execute('CREATE TABLE IF NOT EXISTS api_keys (id TEXT PRIMARY KEY, business_id TEXT NOT NULL REFERENCES businesses(id), '
                     'actor_id INTEGER NOT NULL, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, scopes TEXT NOT NULL, '
                     'created_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT, last_used_at TEXT)')
        conn.execute('CREATE TABLE IF NOT EXISTS api_key_rate (key_id TEXT NOT NULL REFERENCES api_keys(id), '
                     'minute TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY(key_id,minute))')

    @staticmethod
    def _key_view(row):
        return {k: json.loads(row[k]) if k == 'scopes' else row[k] for k in
                ('id', 'business_id', 'name', 'scopes', 'created_at', 'expires_at', 'revoked_at', 'last_used_at')}

    def issue_api_key(self, business_id, actor_id, name, scopes, days, now):
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 80
                or not isinstance(scopes, list) or not scopes or set(scopes) - {'read', 'generate'}
                or type(days) is not int or not 1 <= days <= 90):
            raise InvalidInput('Укажите название, права read/generate и срок от 1 до 90 дней.')
        with self._transaction() as conn:
            business = self._business(conn, business_id)
            if business.owner_id != actor_id or business.archived:
                raise Forbidden('API-ключи создаёт владелец действующей компании.')
            if conn.execute('SELECT COUNT(*) FROM api_keys WHERE business_id=? AND revoked_at IS NULL '
                            'AND expires_at>?', (business_id, now.isoformat())).fetchone()[0] >= 10:
                raise Conflict('Не больше 10 действующих ключей на компанию. Отзовите ненужные.')
            token = 'ss_' + secrets.token_urlsafe(32)
            key_id = str(uuid4())
            conn.execute('INSERT INTO api_keys VALUES(?,?,?,?,?,?,?,?,NULL,NULL)',
                         (key_id, business_id, actor_id, name.strip(), hashlib.sha256(token.encode()).hexdigest(),
                          json.dumps(sorted(set(scopes))), now.isoformat(), (now + timedelta(days=days)).isoformat()))
            self._audit(conn, 'api_key', key_id, 'issued', now, business_id=business_id, scopes=scopes)
            row = conn.execute('SELECT * FROM api_keys WHERE id=?', (key_id,)).fetchone()
            return {**self._key_view(row), 'token': token}

    def list_api_keys(self, business_id, actor_id):
        with self._reader() as conn:
            if self._business(conn, business_id).owner_id != actor_id:
                raise Forbidden('Ключи доступны только владельцу.')
            return [self._key_view(row) for row in conn.execute(
                'SELECT * FROM api_keys WHERE business_id=? ORDER BY created_at DESC LIMIT 100', (business_id,))]

    def revoke_api_key(self, business_id, actor_id, key_id, now):
        with self._transaction() as conn:
            if self._business(conn, business_id).owner_id != actor_id:
                raise Forbidden('Ключи отзывает только владелец.')
            conn.execute('UPDATE api_keys SET revoked_at=? WHERE business_id=? AND id=? AND revoked_at IS NULL',
                         (now.isoformat(), business_id, key_id))
            self._audit(conn, 'api_key', key_id, 'revoked', now, business_id=business_id)

    def authenticate_api_key(self, token, scope, now):
        if not isinstance(token, str) or not token.startswith('ss_') or len(token) != 46:
            raise Forbidden('API-ключ недействителен.')
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._transaction() as conn:
            row = conn.execute('SELECT * FROM api_keys WHERE token_hash=?', (digest,)).fetchone()
            if (row is None or row['revoked_at'] or row['expires_at'] <= now.isoformat()
                    or scope not in json.loads(row['scopes'])):
                raise Forbidden('API-ключ недействителен, истёк или не имеет нужных прав.')
            business = self._business(conn, row['business_id'])
            if business.archived or not self._may_operate(conn, business.owner_id, row['actor_id'], business.id):
                raise Forbidden('Доступ API к этой компании закрыт.')
            minute = now.replace(second=0, microsecond=0).isoformat()
            conn.execute('DELETE FROM api_key_rate WHERE minute<?', ((now-timedelta(minutes=2)).isoformat(),))
            conn.execute('INSERT INTO api_key_rate VALUES(?,?,1) ON CONFLICT(key_id,minute) '
                         'DO UPDATE SET count=count+1', (row['id'], minute))
            count = conn.execute('SELECT count FROM api_key_rate WHERE key_id=? AND minute=?',
                                 (row['id'], minute)).fetchone()[0]
            if count > 60:
                raise RateLimited('Лимит API: 60 запросов в минуту на ключ.')
            conn.execute('UPDATE api_keys SET last_used_at=? WHERE id=?', (now.isoformat(), row['id']))
            return {'id': row['id'], 'business_id': row['business_id'], 'actor_id': row['actor_id']}

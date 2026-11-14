"""Single-workspace memberships and hash-only capped invitations."""

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from uuid import uuid4

from app.shared.errors import Forbidden, InvalidInput


def stamp(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidInput("Требуется дата с часовым поясом.")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


class AccessRepository:
    @staticmethod
    def _migrate_v5_to_v6(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS invite_companies (invite_id TEXT PRIMARY KEY REFERENCES access_invites(id), "
                     "business_id TEXT NOT NULL REFERENCES businesses(id))")
        conn.execute("CREATE TABLE IF NOT EXISTS form_drafts (business_id TEXT NOT NULL REFERENCES businesses(id), "
                     "actor_id INTEGER NOT NULL, kind TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT, "
                     "updated_at TEXT NOT NULL, PRIMARY KEY(business_id,actor_id,kind))")
        conn.execute("CREATE TABLE IF NOT EXISTS company_scopes (owner_id INTEGER NOT NULL, actor_id INTEGER NOT NULL, "
                     "scope TEXT NOT NULL CHECK(scope IN ('all','selected')), PRIMARY KEY(owner_id,actor_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS company_members (business_id TEXT NOT NULL REFERENCES businesses(id), "
                     "actor_id INTEGER NOT NULL, role TEXT NOT NULL CHECK(role IN ('operator','reviewer','none')), "
                     "PRIMARY KEY(business_id,actor_id))")

    @classmethod
    def _business_role(cls, conn, owner_id, actor_id, business_id):
        global_role = cls._access_role(conn, owner_id, actor_id)
        if global_role in {None, 'owner'}:
            return global_role
        grant = conn.execute('SELECT role FROM company_members WHERE business_id=? AND actor_id=?',
                             (business_id, actor_id)).fetchone()
        if grant:
            if grant['role'] == 'none':
                return None
            return 'reviewer' if global_role == 'reviewer' else grant['role']
        scope = conn.execute('SELECT scope FROM company_scopes WHERE owner_id=? AND actor_id=?',
                             (owner_id, actor_id)).fetchone()
        return None if scope and scope['scope'] == 'selected' else global_role

    def business_role(self, owner_id, actor_id, business_id):
        with self._reader() as conn:
            return self._business_role(conn, owner_id, actor_id, business_id)

    def set_company_access(self, owner_id, actor_id, business_id, role, scope, now):
        if role not in {'operator', 'reviewer', 'none'} or scope not in {'all', 'selected'}:
            raise InvalidInput('Выберите роль и область доступа.')
        if actor_id == owner_id:
            raise InvalidInput('Права владельца изменять нельзя.')
        with self._transaction() as conn:
            if self._business(conn, business_id).owner_id != owner_id:
                raise Forbidden('Нет доступа к компании.')
            member = self._access_role(conn, owner_id, actor_id)
            if member is None or (member == 'reviewer' and role == 'operator'):
                raise InvalidInput('Участник должен принять приглашение соответствующей роли.')
            conn.execute('INSERT INTO company_scopes VALUES(?,?,?) ON CONFLICT(owner_id,actor_id) '
                         'DO UPDATE SET scope=excluded.scope', (owner_id, actor_id, scope))
            conn.execute('INSERT INTO company_members VALUES(?,?,?) ON CONFLICT(business_id,actor_id) '
                         'DO UPDATE SET role=excluded.role', (business_id, actor_id, role))
            # Any change invalidates older approvals, even if access is later re-granted.
            self._audit(conn, 'company_access', str(actor_id), 'changed', now,
                        business_id=business_id, role=role, scope=scope)

    def company_access(self, owner_id, business_id):
        with self._reader() as conn:
            rows = conn.execute('SELECT actor_id,role FROM access_members WHERE owner_id=? AND revoked_at IS NULL',
                                (owner_id,)).fetchall()
            result = []
            for row in rows:
                scope = conn.execute('SELECT scope FROM company_scopes WHERE owner_id=? AND actor_id=?',
                                     (owner_id, row['actor_id'])).fetchone()
                result.append({'actor_id': row['actor_id'], 'workspace_role': row['role'],
                               'role': self._business_role(conn, owner_id, row['actor_id'], business_id) or 'none',
                               'scope': scope['scope'] if scope else 'all'})
            return result

    def form_draft(self, business_id, actor_id, kind):
        with self._reader() as conn:
            row = conn.execute('SELECT revision,data FROM form_drafts WHERE business_id=? AND actor_id=? AND kind=?',
                               (business_id, actor_id, kind)).fetchone()
            return {'revision': row['revision'], 'data': json.loads(row['data']) if row['data'] else None} if row else {'revision': 0, 'data': None}

    def save_form_draft(self, owner_id, business_id, actor_id, kind, revision, data, now):
        from app.shared.errors import Conflict
        with self._transaction() as conn:
            if not self._may_operate(conn, owner_id, actor_id, business_id):
                raise Forbidden('Нет права сохранять черновик этой компании.')
            if self._business(conn, business_id).archived:
                raise Conflict('Компания находится в архиве.')
            row = conn.execute('SELECT revision FROM form_drafts WHERE business_id=? AND actor_id=? AND kind=?',
                               (business_id, actor_id, kind)).fetchone()
            if revision != (row['revision'] if row else 0):
                raise Conflict('Черновик изменён в другом окне. Переоткройте форму, чтобы загрузить его.')
            encoded = json.dumps(data, ensure_ascii=False) if data is not None else None
            conn.execute('INSERT INTO form_drafts VALUES(?,?,?,?,?,?) ON CONFLICT(business_id,actor_id,kind) '
                         'DO UPDATE SET revision=excluded.revision,data=excluded.data,updated_at=excluded.updated_at',
                         (business_id, actor_id, kind, revision+1, encoded, stamp(now)))
            return {'revision': revision+1, 'data': data}

    @staticmethod
    def _migrate_v4_to_v5(conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS access_invites (id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, "
            "owner_id INTEGER NOT NULL, role TEXT NOT NULL CHECK(role IN ('operator','reviewer')), "
            "max_uses INTEGER NOT NULL CHECK(max_uses BETWEEN 1 AND 100), created_at TEXT NOT NULL, "
            "expires_at TEXT NOT NULL, revoked_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS access_members (owner_id INTEGER NOT NULL, actor_id INTEGER NOT NULL CHECK(actor_id>0), "
            "role TEXT NOT NULL CHECK(role IN ('operator','reviewer')), granted_at TEXT NOT NULL, "
            "revoked_at TEXT, PRIMARY KEY(owner_id,actor_id))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS access_claims (invite_id TEXT NOT NULL REFERENCES access_invites(id), "
            "actor_id INTEGER NOT NULL CHECK(actor_id>0), claimed_at TEXT NOT NULL, "
            "PRIMARY KEY(invite_id,actor_id))"
        )

    @staticmethod
    def _access_role(conn, owner_id, actor_id):
        if type(actor_id) is not int or actor_id <= 0:
            return None
        if actor_id == owner_id:
            return "owner"
        row = conn.execute(
            "SELECT role FROM access_members WHERE owner_id=? AND actor_id=? AND revoked_at IS NULL",
            (owner_id, actor_id),
        ).fetchone()
        return row["role"] if row else None

    @classmethod
    def _may_operate(cls, conn, owner_id, actor_id, business_id=None):
        role = (cls._business_role(conn, owner_id, actor_id, business_id) if business_id is not None
                else cls._access_role(conn, owner_id, actor_id))
        return role in {"owner", "operator"}

    def access_role(self, owner_id, actor_id):
        with self._reader() as conn:
            return self._access_role(conn, owner_id, actor_id)

    def require_access(self, owner_id, actor_id, *, write=False):
        role = self.access_role(owner_id, actor_id)
        if role is None or (write and role == "reviewer"):
            raise Forbidden("Доступ только для участников команды; роль наблюдателя разрешает только просмотр.")
        return role

    @classmethod
    def _approval_allowed(cls, conn, owner_id, post):
        if not cls._may_operate(conn, owner_id, post.approved_by, post.business_id):
            return False
        if post.approved_by == owner_id:
            return True
        approval = conn.execute(
            "SELECT id,details FROM audit WHERE entity_type='post' AND entity_id=? AND action='approved' "
            "ORDER BY id DESC LIMIT 1", (post.id,),
        ).fetchone()
        # A fresh invite after revocation never revives a former approval.
        cutoff = cls._revocation_sequence(conn, post.approved_by)
        company_cutoff = conn.execute("SELECT MAX(id) FROM audit WHERE entity_type='company_access' AND entity_id=?",
                                      (str(post.approved_by),)).fetchone()[0]
        cutoff = max(cutoff or 0, company_cutoff or 0)
        if not approval or (cutoff is not None and approval["id"] <= cutoff):
            return False
        details = json.loads(approval["details"])
        return details.get("actor_id") == post.approved_by and details.get("revision") == post.revision

    @staticmethod
    def _revocation_sequence(conn, actor_id):
        # Keep the cutoff in the durable audit even after a fresh grant clears
        # access_members.revoked_at. Sequence IDs also avoid clock-equality races.
        return conn.execute(
            "SELECT MAX(id) FROM audit WHERE entity_type='access_member' AND entity_id=? AND action='revoked'",
            (str(actor_id),),
        ).fetchone()[0]

    def approval_allowed(self, owner_id, post):
        with self._reader() as conn:
            return self._approval_allowed(conn, owner_id, post)

    def create_access_invite(self, owner_id, role, max_uses, expires_at, now, *, business_id=None):
        created, expires = stamp(now), stamp(expires_at)
        if role not in {"operator", "reviewer"} or type(max_uses) is not int or not 1 <= max_uses <= 100:
            raise InvalidInput("Укажите роль operator/reviewer и лимит от 1 до 100 участников.")
        if expires <= created or (expires_at - now).total_seconds() > 30 * 86400:
            raise InvalidInput("Приглашение должно действовать от текущего момента до 30 дней.")
        token, invite_id = secrets.token_urlsafe(32), str(uuid4())
        with self._transaction() as conn:
            if business_id is not None and self._business(conn, business_id).owner_id != owner_id:
                raise Forbidden('Нет доступа к компании приглашения.')
            conn.execute(
                "INSERT INTO access_invites VALUES (?,?,?,?,?,?,?,NULL)",
                (invite_id, hashlib.sha256(token.encode()).hexdigest(), owner_id, role, max_uses, created, expires),
            )
            if business_id is not None:
                conn.execute('INSERT INTO invite_companies VALUES(?,?)', (invite_id, business_id))
            self._audit(conn, "access_invite", invite_id, "created", now,
                        role=role, max_uses=max_uses, origin="local_admin")
        return {"id": invite_id, "token": token, "role": role, "max_uses": max_uses, "expires_at": expires}

    def claim_access_invite(self, owner_id, actor_id, token, now):
        if type(actor_id) is not int or actor_id <= 0 or not isinstance(token, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{43}", token,
        ):
            raise Forbidden("Приглашение недействительно или больше не доступно.")
        at = stamp(now)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._transaction() as conn:
            invite = conn.execute(
                "SELECT * FROM access_invites WHERE token_hash=? AND owner_id=?", (digest, owner_id),
            ).fetchone()
            if invite is None or invite["revoked_at"] or invite["expires_at"] <= at:
                raise Forbidden("Приглашение недействительно или больше не доступно.")
            member = conn.execute(
                "SELECT * FROM access_members WHERE owner_id=? AND actor_id=?", (owner_id, actor_id),
            ).fetchone()
            cutoff = self._revocation_sequence(conn, actor_id)
            company_cutoff = conn.execute("SELECT MAX(id) FROM audit WHERE entity_type='company_access' AND entity_id=?",
                                          (str(actor_id),)).fetchone()[0]
            cutoff = max(cutoff or 0, company_cutoff or 0) or None
            if cutoff is not None:
                issued = conn.execute(
                    "SELECT id FROM audit WHERE entity_type='access_invite' AND entity_id=? AND action='created' "
                    "ORDER BY id DESC LIMIT 1", (invite["id"],),
                ).fetchone()
                if not issued or issued["id"] <= cutoff:
                    raise Forbidden("Доступ отозван. Требуется новое приглашение администратора.")
            prior = conn.execute(
                "SELECT 1 FROM access_claims WHERE invite_id=? AND actor_id=?", (invite["id"], actor_id),
            ).fetchone()
            if not prior:
                used = conn.execute(
                    "SELECT COUNT(*) FROM access_claims WHERE invite_id=?", (invite["id"],),
                ).fetchone()[0]
                if used >= invite["max_uses"]:
                    raise Forbidden("Приглашение недействительно или больше не доступно.")
                conn.execute("INSERT INTO access_claims VALUES (?,?,?)", (invite["id"], actor_id, at))
            role = self._access_role(conn, owner_id, actor_id)
            # A reviewer link cannot downgrade a current operator or the owner.
            role = role if role in {"owner", "operator"} else invite["role"]
            if role != "owner" and (not member or member["revoked_at"] or member["role"] != role):
                conn.execute(
                    "INSERT INTO access_members VALUES (?,?,?,?,NULL) ON CONFLICT(owner_id,actor_id) "
                    "DO UPDATE SET role=excluded.role,granted_at=excluded.granted_at,revoked_at=NULL",
                    (owner_id, actor_id, role, at),
                )
            scoped = conn.execute('SELECT business_id FROM invite_companies WHERE invite_id=?', (invite['id'],)).fetchone()
            if scoped and role != 'owner':
                if not member or member['revoked_at'] or (member['role'] == 'reviewer' and invite['role'] == 'operator'):
                    conn.execute("INSERT INTO company_scopes VALUES(?,?,'selected') ON CONFLICT(owner_id,actor_id) "
                                 "DO UPDATE SET scope='selected'", (owner_id, actor_id))
                    conn.execute('DELETE FROM company_members WHERE actor_id=?', (actor_id,))
                prior_role = self._business_role(conn, owner_id, actor_id, scoped['business_id'])
                company_role = 'operator' if prior_role == 'operator' else invite['role']
                conn.execute('INSERT INTO company_members VALUES(?,?,?) ON CONFLICT(business_id,actor_id) '
                             'DO UPDATE SET role=excluded.role', (scoped['business_id'], actor_id, company_role))
            self._audit(conn, "access_member", str(actor_id), "invite_claimed", now,
                        actor_id=actor_id, role=role, invite_id=invite["id"])
            return role

    def revoke_access_member(self, owner_id, actor_id, now):
        if type(actor_id) is not int or actor_id <= 0 or actor_id == owner_id:
            raise InvalidInput("Нельзя отозвать владельца пространства.")
        with self._transaction() as conn:
            conn.execute("UPDATE access_members SET revoked_at=? WHERE owner_id=? AND actor_id=?",
                         (stamp(now), owner_id, actor_id))
            self._audit(conn, "access_member", str(actor_id), "revoked", now, origin="local_admin")

    def revoke_access_invite(self, owner_id, invite_id, now):
        with self._transaction() as conn:
            conn.execute("UPDATE access_invites SET revoked_at=? WHERE owner_id=? AND id=?",
                         (stamp(now), owner_id, invite_id))
            self._audit(conn, "access_invite", invite_id, "revoked", now, origin="local_admin")

    def list_access(self, owner_id):
        with self._reader() as conn:
            invites = conn.execute(
                "SELECT i.id,i.role,i.max_uses,i.created_at,i.expires_at,i.revoked_at,"
                "(SELECT COUNT(*) FROM access_claims c WHERE c.invite_id=i.id) AS uses "
                "FROM access_invites i WHERE owner_id=? ORDER BY created_at", (owner_id,),
            ).fetchall()
            members = conn.execute(
                "SELECT actor_id,role,granted_at,revoked_at FROM access_members WHERE owner_id=? ORDER BY actor_id",
                (owner_id,),
            ).fetchall()
            return {"invites": [dict(row) for row in invites], "members": [dict(row) for row in members]}

    def record_actor_action(self, actor_id, action, target, now):
        with self._transaction() as conn:
            self._audit(conn, "workspace_action", str(target), action, now, actor_id=actor_id)

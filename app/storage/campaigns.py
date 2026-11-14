import hashlib
import json
from uuid import uuid4

from app.shared.errors import Conflict, Forbidden, NotFound


class CampaignRepository:
    @staticmethod
    def initialize_campaigns(conn):
        conn.execute('CREATE TABLE IF NOT EXISTS campaigns (id TEXT PRIMARY KEY, business_id TEXT NOT NULL '
                     'REFERENCES businesses(id), request_key TEXT NOT NULL, payload_hash TEXT NOT NULL, '
                     'data TEXT NOT NULL, UNIQUE(business_id,request_key))')
        conn.execute('CREATE TABLE IF NOT EXISTS leads (id TEXT PRIMARY KEY, business_id TEXT NOT NULL '
                     'REFERENCES businesses(id), campaign_id TEXT REFERENCES campaigns(id), request_key TEXT NOT NULL, '
                     'payload_hash TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(business_id,request_key))')
        conn.execute('CREATE INDEX IF NOT EXISTS leads_company ON leads(business_id)')

    def _campaign_write(self, conn, business_id, actor_id):
        business = self._business(conn, business_id)
        if business.archived or not self._may_operate(conn, business.owner_id, actor_id, business_id):
            raise Forbidden('Нет прав изменять обращения этой компании.')

    def save_campaign_item(self, business_id, actor_id, kind, data, request_key, now):
        if kind not in {'campaigns', 'leads'}:
            raise ValueError('Invalid item kind')
        fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self._transaction() as conn:
            self._campaign_write(conn, business_id, actor_id)
            row = conn.execute(f'SELECT data,payload_hash FROM {kind} WHERE business_id=? AND request_key=?',
                               (business_id, request_key)).fetchone()
            if row:
                if row['payload_hash'] != fingerprint:
                    raise Conflict('Этот идентификатор уже использован для других данных.')
                return json.loads(row['data'])
            limit = 100 if kind == 'campaigns' else 5000
            if conn.execute(f'SELECT COUNT(*) FROM {kind} WHERE business_id=?', (business_id,)).fetchone()[0] >= limit:
                raise Conflict(f'Лимит записей этого вида на компанию: {limit}.')
            if kind == 'leads' and data.get('campaign_id'):
                campaign = conn.execute('SELECT business_id FROM campaigns WHERE id=?', (data['campaign_id'],)).fetchone()
                if campaign is None or campaign['business_id'] != business_id:
                    raise Forbidden('Кампания недоступна в этой компании.')
            result = {**data, 'id': str(uuid4()), 'business_id': business_id, 'revision': 1,
                      'created_at': now.isoformat(), 'updated_at': now.isoformat(), 'created_by': actor_id}
            columns = 'id,business_id,request_key,payload_hash,data'
            values = [result['id'], business_id, request_key, fingerprint, json.dumps(result, ensure_ascii=False)]
            if kind == 'leads':
                columns += ',campaign_id'
                values.append(data.get('campaign_id'))
            conn.execute(f'INSERT INTO {kind} ({columns}) VALUES ({",".join("?" for _ in values)})', values)
            self._audit(conn, kind, result['id'], 'created', now, business_id=business_id, actor_id=actor_id)
            return result

    def edit_lead(self, business_id, actor_id, lead_id, revision, stage, amount_kopecks, now):
        with self._transaction() as conn:
            self._campaign_write(conn, business_id, actor_id)
            row = conn.execute('SELECT data FROM leads WHERE id=? AND business_id=?', (lead_id,business_id)).fetchone()
            if not row:
                raise NotFound('Обращение не найдено.')
            data = json.loads(row['data'])
            if data['revision'] != revision:
                raise Conflict('Обращение изменилось. Откройте актуальную версию.')
            data.update(stage=stage, amount_kopecks=amount_kopecks, revision=revision+1, updated_at=now.isoformat())
            conn.execute('UPDATE leads SET data=? WHERE id=?', (json.dumps(data,ensure_ascii=False),lead_id))
            self._audit(conn, 'lead', lead_id, 'stage_changed', now, actor_id=actor_id, stage=stage,
                        amount_kopecks=amount_kopecks, revision=revision+1)
            return data

    def campaign_items(self, business_id, kind):
        if kind not in {'campaigns', 'leads'}:
            raise ValueError('Invalid item kind')
        with self._reader() as conn:
            self._business(conn, business_id)
            return [json.loads(row['data']) for row in conn.execute(
                f"SELECT data FROM {kind} WHERE business_id=? ORDER BY json_extract(data,'$.created_at') DESC,id DESC",
                (business_id,))]

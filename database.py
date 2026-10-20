"""Async MongoDB persistence, lease fencing and atomic single-document accounting."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from pymongo import AsyncMongoClient, ReturnDocument
from pymongo.write_concern import WriteConcern
from cloud_settings import defaults, validate_document, patched, empty_stats, MODES


def utcnow():
    return datetime.now(timezone.utc)


class Repository:
    def __init__(self, uri, db_name, collection=None):
        self.client = None
        if collection is None:
            self.client = AsyncMongoClient(uri, serverSelectionTimeoutMS=8000,
                                           connectTimeoutMS=8000, socketTimeoutMS=10000,
                                           tz_aware=True)
            self.collection = self.client[db_name].get_collection(
                'bot_settings', write_concern=WriteConcern(w='majority'))
        else:
            self.collection = collection  # offline test injection
        self.owner = uuid4().hex
        self.doc = None
        self.lock = asyncio.Lock()
        self.leader = False

    async def initialize(self):
        if self.client:
            await self.client.admin.command('ping')
        await self.collection.update_one({'_id': 'current_settings'},
                                         {'$setOnInsert': defaults()}, upsert=True)
        raw = await self.collection.find_one({'_id': 'current_settings'})
        # Merge new schema defaults in RAM, without overriding any stored values.
        def merge(a, b):
            for k, v in b.items():
                if k not in a:
                    a[k] = deepcopy(v)
                elif isinstance(v, dict) and isinstance(a[k], dict):
                    merge(a[k], v)
            return a
        previous = deepcopy(raw)
        self.doc = validate_document(merge(raw, defaults()))
        if self.leader:
            missing = {}
            def collect(old, new, prefix=''):
                for key, value in new.items():
                    path = prefix + key
                    if key not in old:
                        missing[path] = deepcopy(value)
                    elif isinstance(value, dict) and isinstance(old[key], dict):
                        collect(old[key], value, path + '.')
            collect(previous, self.doc)
            if missing:
                result = await self.collection.update_one(self.fence(), {'$set': missing})
                if result.matched_count != 1:
                    raise RuntimeError('lease_lost')
        return self.doc

    def fence(self):
        return {'_id': 'current_settings', 'lease.owner': self.owner,
                'lease.expires_at': {'$gt': utcnow()}}

    async def acquire(self):
        now = utcnow()
        q = {'_id': 'current_settings', '$or': [
            {'lease.expires_at': {'$lte': now}},
            {'lease': {'$exists': False}}, {'lease.owner': self.owner}]}
        doc = await self.collection.find_one_and_update(q, {'$set': {
            'lease': {'owner': self.owner, 'expires_at': now + timedelta(seconds=30)}}},
            return_document=ReturnDocument.AFTER)
        self.leader = doc is not None
        if self.leader:
            # persist any missing schema fields after acquiring the only writer lease
            await self.initialize()
        return self.leader

    async def renew(self):
        r = await self.collection.update_one(self.fence(), {'$set': {
            'lease.expires_at': utcnow() + timedelta(seconds=30)}})
        self.leader = r.matched_count == 1
        if not self.leader:
            raise RuntimeError('lease_lost')

    async def release(self):
        await self.collection.update_one({'_id': 'current_settings', 'lease.owner': self.owner},
                                         {'$unset': {'lease': ''}})
        self.leader = False

    async def update(self, fields):
        async with self.lock:
            new_doc = patched(self.doc, fields)
            r = await self.collection.update_one(self.fence(), {'$set': fields})
            if r.matched_count != 1:
                raise RuntimeError('lease_lost')
            # DB first: a failed write is never reported as a saved setting.
            self.doc = new_doc

    async def reserve(self, order):
        async with self.lock:
            q = self.fence()
            q['runtime.pending'] = None
            r = await self.collection.update_one(q, {'$set': {'runtime.pending': order}})
            if r.matched_count != 1:
                raise RuntimeError('pending_or_lease_lost')
            self.doc['runtime']['pending'] = deepcopy(order)

    async def resolve(self, order, result, before, after, next_step, dispatched=True,
                      seq_lost=0, seq_lost_amount=0.0):
        """Idempotent by pending ID. Unknown finances are never counted as full loss."""
        amount = order['amount']
        mode = order['mode']
        q = self.fence()
        q['runtime.pending.id'] = order['id']
        inc = {}
        if dispatched:
            values = {'total_trades': 1, 'total_staked': amount}
            if result in ('win', 'loss', 'draw'):
                key = {'win': 'won_trades', 'loss': 'lost_trades', 'draw': 'draws'}[result]
                values[key] = 1
                if before is not None and after is not None:
                    delta = round(after - before, 6)
                    values.update(net_pnl=delta, total_returned=round(max(0, amount+delta), 6),
                                  financial_trades=1)
            else:
                values['unconfirmed'] = 1
            if seq_lost:
                values['sequence_losses'] = seq_lost
                values['lost_amount'] = round(seq_lost_amount, 6)
            for prefix in ('session_stats', 'account_stats.' + mode):
                inc.update({prefix + '.' + k: v for k, v in values.items()})
        update = {'$set': {'runtime.pending': None, 'runtime.martingale_step': next_step,
                           'runtime.sizing_signature': order.get('sizing_signature')}}
        if inc:
            update['$inc'] = inc
        async with self.lock:
            r = await self.collection.update_one(q, update)
            if r.matched_count != 1:
                raise RuntimeError('result_already_saved_or_lease_lost')
            self.doc['runtime'].update(pending=None, martingale_step=next_step,
                                       sizing_signature=order.get('sizing_signature'))
            for path, v in inc.items():
                parts = path.split('.')
                node = self.doc
                for p in parts[:-1]:
                    node = node.setdefault(p, {})
                node[parts[-1]] = node.get(parts[-1], 0) + v

    async def acknowledge_pending(self):
        order = self.doc['runtime'].get('pending')
        if order:
            # Unknown order is explicitly archived as unconfirmed, not retried.
            await self.resolve(order, None, None, None, 0, dispatched=True)

    async def reset_step(self):
        async with self.lock:
            r = await self.collection.update_one(self.fence(), {'$set': {'runtime.martingale_step': 0}})
            if r.matched_count != 1:
                raise RuntimeError('lease_lost')
            self.doc['runtime']['martingale_step'] = 0

    async def reset_stats(self):
        """Zero all statistics and the martingale step (ledger reset on user request)."""
        async with self.lock:
            update = {'$set': {'session_stats': empty_stats(),
                               'account_stats': {m: empty_stats() for m in MODES},
                               'runtime.martingale_step': 0}}
            r = await self.collection.update_one(self.fence(), update)
            if r.matched_count != 1:
                raise RuntimeError('lease_lost')
            self.doc['session_stats'] = empty_stats()
            self.doc['account_stats'] = {m: empty_stats() for m in MODES}
            self.doc['runtime']['martingale_step'] = 0

    async def close(self):
        if self.client:
            await self.client.close()

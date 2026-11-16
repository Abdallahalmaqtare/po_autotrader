"""HTTP Mini App adapter, using the provider's public app.js contract.
Evidence: /miniapp/assets/app.js?v=20261115.meta15.0-live6-grok-lang
POST bootstrap/status/action; UUID request_id; opaque server-issued capabilities.
No browser, login-code collection, signal generation or access-level bypass.
"""
import asyncio
import hashlib
import html
import json
import math
import os
import re
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import aiohttp
from diagnostics import SafeIssue
from meta_navigation import NavigationError
from payout_guard import symbol

SOURCE_URL = 'https://sdasdsahdsa.lol/miniapp/assets/app.js?v=20261115.meta15.0-live6-grok-lang'


def env_bool(name, default=True):
    value = os.getenv(name, '1' if default else '0').strip().lower()
    if value not in ('0','1','true','false','yes','no'):
        raise SafeIssue('META_API_CONFIG')
    return value in ('1','true','yes')


def tf_seconds(value):
    match = re.fullmatch(r'([SMH])(\d+)', str(value or ''))
    if not match or int(match[2]) <= 0:
        return None
    return int(match[2]) * {'S':1,'M':60,'H':3600}[match[1]]


def plain(value):
    return html.unescape(re.sub(r'<[^>]*>', '', str(value or '')))


def selection(card):
    s = card.get('selection') or {}
    sym = symbol(s.get('symbol'))
    named = symbol(s.get('asset'))
    if s.get('market') != 'o' or not sym or (named and named != sym):
        return None, None
    return sym, tf_seconds(s.get('timeframe'))


def card_direction(card):
    structured = card.get('direction')
    # BUY/SELL and the four text labels below are present in public app.js.
    mapped = {'BUY':'UP','SELL':'DOWN'}.get(structured) if structured else None
    if structured and mapped is None:
        raise NavigationError('META_UNKNOWN_DIRECTION')
    labels = re.findall(r'Meta\s+Signal\s*:\s*(Higher|Lower|Выше|Ниже)\b', plain(card.get('text')), re.I)
    values = {('UP' if x.lower() in ('higher','выше') else 'DOWN') for x in labels}
    if mapped: values.add(mapped)
    if len(values) != 1:
        raise NavigationError('META_DIRECTION_UNVERIFIED')
    return next(iter(values))


class MetaHTTPSettings:
    def __init__(self):
        self.base = os.getenv('META_API_BASE','https://sdasdsahdsa.lol/miniapp/api').strip().rstrip('/')
        self.bot_id = os.getenv('META_BOT_ID','8808473133').strip()
        self.dry_run = env_bool('META_DRY_RUN', True)
        self.language = os.getenv('META_DEVICE_LANGUAGE','ar-YE').strip()
        try:
            self.poll = float(os.getenv('META_POLL_SECONDS','0.5'))
            self.timeout = float(os.getenv('META_HTTP_TIMEOUT_SECONDS','12'))
            self.operation_timeout = float(os.getenv('META_OPERATION_TIMEOUT_SECONDS','45'))
        except ValueError:
            raise SafeIssue('META_API_CONFIG') from None
        parsed = urlparse(self.base)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path != '/miniapp/api'
                or not self.bot_id.isdigit() or not self.language or len(self.language)>24
                or not all(math.isfinite(x) for x in (self.poll,self.timeout,self.operation_timeout))
                or not .15 <= self.poll <= 10 or not 1 <= self.timeout <= 60
                or not 5 <= self.operation_timeout <= 180):
            raise SafeIssue('META_API_CONFIG')


class HTTPMiniApp:
    def __init__(self, owner, settings=None, http=None):
        self.owner = owner
        self.settings = settings or MetaHTTPSettings()
        self.dry_run = self.settings.dry_run
        self.http = http
        self.own_http = http is None
        self.init_data = ''
        self.session_id = None
        self.sequence = 0
        self.card = {}
        self.profile = {}
        self.navigation = []
        self.customer = {}
        self.busy = False
        self.operation = None
        self.expected = None
        self.pending = False
        self.arm = None
        self.before_result = None
        self.origin_operation = None
        self.new_operation_seen = False
        self.consumed = set()
        self.consumed_order = []
        self.request_started = 0.0
        self.renewed_at = 0.0
        self.query_id = None
        self.keepalive = None
        self.keepalive_error = False
        self.launch_reference = None
        self.api_lock = asyncio.Lock()
        self.uncertain_action = False

    def check(self):
        self.owner.check()
        if self.uncertain_action:
            raise SafeIssue('META_API_ACTION_UNCERTAIN')
        if self.keepalive_error:
            raise SafeIssue('META_API_AUTH')

    async def launch(self, launch):
        from telethon import functions, types
        self.check()
        self.launch_reference = launch
        url, kind = launch
        parsed = urlparse(url)
        if parsed.scheme != 'https': raise SafeIssue('META_API_CONFIG')
        bot = self.owner.entity
        if str(getattr(bot,'id','')) != self.settings.bot_id:
            raise SafeIssue('META_API_BOT_MISMATCH')
        if parsed.hostname in ('t.me','telegram.me'):
            parts = parsed.path.strip('/').split('/')
            if len(parts)!=2 or parts[0].lower()!=(getattr(bot,'username','') or '').lower():
                raise SafeIssue('META_API_BOT_MISMATCH')
            request = functions.messages.RequestAppWebViewRequest(peer=bot,
                app=types.InputBotAppShortName(bot,parts[1]),platform='android',
                start_param=parse_qs(parsed.query).get('startapp',[None])[0])
        elif kind == 'simple':
            request = functions.messages.RequestSimpleWebViewRequest(bot=bot,platform='android',url=url)
        else:
            request = functions.messages.RequestWebViewRequest(peer=bot,bot=bot,
                platform='android',url=url,from_bot_menu=(kind=='menu'))
        try:
            result = await asyncio.wait_for(self.owner.client(request),self.owner.timeout)
            signed = urlparse(result.url)
            base = urlparse(self.settings.base)
            if signed.scheme != 'https' or signed.netloc != base.netloc:
                raise SafeIssue('META_API_ORIGIN')
            query = parse_qs(signed.query)
            if query.get('bot_id',[self.settings.bot_id])[0] != self.settings.bot_id:
                raise SafeIssue('META_API_BOT_MISMATCH')
            # Decode the URL parameter ONCE: do not unquote initData.user twice.
            fragments = parse_qs(signed.fragment)
            self.init_data = fragments.get('tgWebAppData',query.get('tgWebAppData',['']))[0]
            auth = parse_qs(self.init_data)
            if not self.init_data or not all(k in auth for k in ('user','auth_date','hash')):
                raise SafeIssue('META_API_AUTH')
            self.query_id = getattr(result,'query_id',None)
            self.renewed_at = time.monotonic()
            if self.http is None:
                self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.settings.timeout),
                    cookie_jar=aiohttp.CookieJar())
            self.session_id = None
            self.sequence = 0
            self.pending = False
            await self.api('bootstrap',reset_view=True,device_language=self.settings.language)
            await self.settle()
            if self.query_id is not None and self.keepalive is None:
                self.keepalive = asyncio.create_task(self.keep_alive())
        except asyncio.CancelledError:
            await self.close()
            raise
        except SafeIssue:
            await self.close()
            raise
        except Exception:
            await self.close()
            raise SafeIssue('META_API_AUTH') from None

    async def keep_alive(self):
        from telethon import functions
        try:
            while True:
                await asyncio.sleep(30)
                await asyncio.wait_for(self.owner.client(functions.messages.ProlongWebViewRequest(
                    peer=self.owner.entity,bot=self.owner.entity,query_id=self.query_id)),self.owner.timeout)
                self.renewed_at = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.keepalive_error = True

    async def close(self):
        if self.keepalive:
            self.keepalive.cancel()
            try: await self.keepalive
            except asyncio.CancelledError: pass
            self.keepalive = None
        if self.http is not None and self.own_http:
            await self.http.close()
        self.http = None
        self.init_data = ''
        self.session_id = None
        self.pending = False
        self.operation = None
        self.origin_operation = None
        self.query_id = None
        self.card = {}
        self.profile = {}
        self.customer = {}
        self.navigation = []

    def accept(self, data):
        if not isinstance(data,dict): raise SafeIssue('META_API_SCHEMA')
        incoming = data.get('session_id')
        if incoming and incoming != self.session_id:
            if self.pending: raise SafeIssue('META_API_SESSION_CHANGED')
            self.session_id = incoming
            self.sequence = 0
        seq = data.get('snapshot_seq')
        if seq is not None and (not isinstance(seq,int) or isinstance(seq,bool) or seq<0):
            raise SafeIssue('META_API_SCHEMA')
        if seq and seq < self.sequence:
            return False
        if seq: self.sequence = seq
        for key,attr,typ in (('card','card',dict),('profile','profile',dict),
                             ('nav','navigation',list),('customer','customer',dict)):
            if key in data and data[key] is not None:
                if not isinstance(data[key],typ): raise SafeIssue('META_API_SCHEMA')
                if key == 'profile': self.profile.update(data[key])
                else: setattr(self,attr,data[key])
        if self.card.get('selection') is not None and not isinstance(self.card['selection'],dict):
            raise SafeIssue('META_API_SCHEMA')
        if not isinstance(data.get('busy'),bool): raise SafeIssue('META_API_SCHEMA')
        self.busy = data['busy']
        if data.get('operation_id'):
            if not isinstance(data['operation_id'],str): raise SafeIssue('META_API_SCHEMA')
            self.operation = data['operation_id']
        if self.pending and self.operation and self.operation != self.origin_operation:
            self.new_operation_seen = True
        if not self.session_id: raise SafeIssue('META_API_SCHEMA')
        return True

    async def api(self, endpoint, **values):
        self.check()
        if endpoint not in ('bootstrap','status','action') or self.http is None:
            raise SafeIssue('META_API_CONFIG')
        body = {'bot_id':self.settings.bot_id, **values}
        if self.session_id: body['session_id'] = self.session_id
        headers = {'Content-Type':'application/json','X-Telegram-Init-Data':self.init_data}
        async with self.api_lock:
            try:
                async with self.http.post(self.settings.base+'/'+endpoint,json=body,
                                          headers=headers,allow_redirects=False) as response:
                    if 300 <= response.status < 400: raise SafeIssue('META_API_ORIGIN')
                    chunks = []
                    size = 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size>2_000_000: raise SafeIssue('META_API_SCHEMA')
                        chunks.append(chunk)
                    raw = b''.join(chunks)
                    data = json.loads(raw)
                    if not isinstance(data,dict): raise SafeIssue('META_API_SCHEMA')
                    error = data.get('error') or {}
                    code = error.get('code') if isinstance(error,dict) else None
                    if code=='session_expired': raise SafeIssue('META_API_SESSION_EXPIRED')
                    if response.status in (401,403): raise SafeIssue('META_API_AUTH')
                    if response.status==429: raise SafeIssue('META_API_RATE_LIMIT')
                    if code in ('stale_action','expired_action'): raise SafeIssue('META_API_STALE_ACTION')
                    if response.status>=500:
                        if endpoint=='action': raise SafeIssue('META_API_ACTION_UNCERTAIN')
                        raise NavigationError('META_API_STATUS_NETWORK')
                    if response.status>=400 or data.get('ok') is False or error:
                        raise SafeIssue('META_API_REJECTED')
                    self.accept(data)
                    return data
            except asyncio.CancelledError:
                if endpoint=='action': self.uncertain_action = True
                raise
            except (SafeIssue, NavigationError):
                raise
            except (aiohttp.ClientError,asyncio.TimeoutError,ValueError,TypeError):
                if endpoint=='action': raise SafeIssue('META_API_ACTION_UNCERTAIN') from None
                raise NavigationError('META_API_STATUS_NETWORK') from None

    async def pause(self, seconds):
        deadline = time.monotonic()+seconds
        while time.monotonic()<deadline:
            self.check()
            await asyncio.sleep(min(.1,max(0,deadline-time.monotonic())))

    async def settle(self, timeout=None):
        deadline = time.monotonic()+(timeout or self.settings.operation_timeout)
        failures = 0
        while self.busy:
            self.check()
            if time.monotonic()>=deadline:
                raise SafeIssue('META_API_OPERATION_TIMEOUT')
            await self.pause(min(self.settings.poll if not failures else min(2.5, .5+failures*.5),
                                 max(0,deadline-time.monotonic())))
            try:
                remaining = deadline-time.monotonic()
                if remaining <= 0: raise SafeIssue('META_API_OPERATION_TIMEOUT')
                await asyncio.wait_for(self.api('status'),min(remaining,self.settings.timeout))
                failures = 0
            except (NavigationError,asyncio.TimeoutError):
                failures += 1
                if failures>=4: raise SafeIssue('META_API_OPERATION_TIMEOUT') from None
        return self.card

    def screen_name(self):
        return self.card.get('screen') or {'trade_menu':'trade','test':'trade'}.get(
            self.card.get('section'),self.card.get('section','home'))

    def buttons(self):
        rows = self.card.get('keyboard') or []
        if not isinstance(rows,list) or any(not isinstance(row,list) for row in rows):
            raise SafeIssue('META_API_SCHEMA')
        keyboard = [b for row in rows for b in row]
        duration = self.card.get('duration_choices') or []
        catalog = self.card.get('asset_catalog') or []
        if not isinstance(duration,list) or not isinstance(catalog,list):
            raise SafeIssue('META_API_SCHEMA')
        buttons = duration + [b for b in keyboard if not duration or not isinstance(b,dict) or b.get('kind')!='confirm'] + catalog
        if any(not isinstance(b,dict) or (b.get('context') is not None and not isinstance(b['context'],dict)) for b in buttons):
            raise SafeIssue('META_API_SCHEMA')
        # The same capability may be present in both catalog and keyboard.
        unique = {}
        for b in buttons:
            token = b.get('action_token')
            if isinstance(token,str) and token: unique[token] = b
        return list(unique.values())

    def usable(self, item):
        return (bool(item.get('action_token')) and not item.get('locked') and not item.get('disabled')
                and item.get('available') is not False and item.get('availability','open')=='open')

    def find(self, predicate):
        matches = [b for b in self.buttons() if predicate(b)]
        if len(matches)>1: raise SafeIssue('META_API_AMBIGUOUS_ACTION')
        return matches[0] if matches else None

    async def act(self, item, settle=True):
        self.check()
        if self.busy: raise SafeIssue('META_API_STATE')
        if not item or not self.usable(item): raise NavigationError('META_DURATION_OR_ASSET_UNAVAILABLE')
        token = item['action_token']
        # UUID is the frontend's documented idempotency key. Conservative policy:
        # never replay an ambiguous action, even with the same UUID.
        await self.api('action',action_token=token,request_id=str(uuid4()))
        if settle: await self.settle()

    async def nav_trade(self):
        options = [n for n in self.navigation if isinstance(n,dict) and n.get('key')=='trade_menu']
        if len(options)!=1 or not options[0].get('action_token'):
            raise SafeIssue('META_API_NAVIGATION')
        await self.act(options[0])

    async def to_assets(self):
        await self.settle()
        for _ in range(16):
            self.check()
            s = self.screen_name()
            if s=='assets' and (self.card.get('selection') or {}).get('market')=='o': return
            if s.startswith('customer_') or s.startswith('onboarding'):
                raise SafeIssue('META_API_ACCESS')
            item = None
            if s=='trade':
                item = self.find(lambda b:b.get('kind')=='assets' and (b.get('context') or {}).get('market')=='o')
            elif s=='timeframes':
                item = self.find(lambda b:b.get('kind')=='assets' and (b.get('context') or {}).get('market')=='o')
            elif s in ('confirm','signal') and (self.card.get('selection') or {}).get('market')=='o':
                item = self.find(lambda b:b.get('kind')=='timeframes')
            if item: await self.act(item)
            else: await self.nav_trade()
        raise SafeIssue('META_API_NAVIGATION')

    def access(self, duration):
        allowed = self.profile.get('allowed_timeframes')
        if not isinstance(allowed,list): raise SafeIssue('META_API_SCHEMA')
        if self.profile.get('can_trade') is not True: raise SafeIssue('META_API_ACCESS')
        if duration not in {tf_seconds(x) for x in allowed}:
            raise SafeIssue('META_API_DURATION')

    async def choose_asset(self, asset):
        await self.to_assets()
        for _ in range(80):
            item = self.find(lambda b:b.get('kind')=='timeframes'
                and (b.get('context') or {}).get('market')=='o'
                and symbol((b.get('context') or {}).get('symbol'))==asset)
            if item:
                if not self.usable(item): raise SafeIssue('META_API_ASSET')
                await self.act(item)
                return
            if isinstance(self.card.get('asset_catalog'),list):
                raise SafeIssue('META_API_ASSET')
            current = (self.card.get('selection') or {}).get('page',0)
            item = self.find(lambda b:b.get('kind')=='assets'
                and (b.get('context') or {}).get('market')=='o'
                and isinstance((b.get('context') or {}).get('page'),int)
                and (b.get('context') or {})['page']==current+1)
            if not item: raise SafeIssue('META_API_ASSET')
            await self.act(item)
        raise SafeIssue('META_API_NAVIGATION')

    async def configure(self, asset, duration, before_confirm):
        self.check()
        await self.settle()
        self.access(duration)
        self.expected = (asset,duration)
        self.pending = False
        if self.screen_name()=='signal' and selection(self.card)==self.expected:
            return False
        # Reuse the saved selection when it matches; otherwise follow the real
        # server capabilities that back the visual Back/Start/asset-picker flow.
        if selection(self.card)!=self.expected or self.screen_name() not in ('confirm','timeframes'):
            await self.choose_asset(asset)
            self.access(duration)
            choice = self.find(lambda b:b.get('kind')=='confirm'
                and (b.get('context') or {}).get('market')=='o'
                and symbol((b.get('context') or {}).get('symbol'))==asset
                and tf_seconds((b.get('context') or {}).get('timeframe'))==duration)
            if choice is None or not self.usable(choice): raise SafeIssue('META_API_DURATION')
            await self.act(choice)
        if self.screen_name()!='confirm' or selection(self.card)!=self.expected:
            raise NavigationError('META_CONFIRM_MISMATCH')
        await self.request_signal(before_confirm)
        return True

    def result_key(self):
        card = self.card
        return hashlib.sha256(json.dumps([self.session_id,card.get('issued_at_ms',card.get('issued_at',card.get('id'))),
            card.get('direction'),card.get('text'),card.get('selection')],sort_keys=True,ensure_ascii=False).encode()).hexdigest()

    async def request_signal(self, before_click):
        await self.settle()
        if self.pending: raise SafeIssue('META_API_STATE')
        if selection(self.card)!=self.expected: raise NavigationError('META_SELECTION_CHANGED')
        self.access(self.expected[1])
        item = self.find(lambda b:b.get('kind')=='signal' and self.usable(b))
        if not item: raise SafeIssue('META_API_NAVIGATION')
        ctx = item.get('context') or {}
        if (ctx.get('market')!='o' or symbol(ctx.get('symbol'))!=self.expected[0]
                or tf_seconds(ctx.get('timeframe'))!=self.expected[1]):
            raise NavigationError('META_SIGNAL_SELECTION_MISMATCH')
        self.before_result = self.result_key()
        self.origin_operation = self.operation
        self.new_operation_seen = False
        self.pending = True
        self.request_started = time.monotonic()
        self.arm = before_click
        # Arm when the signal action is sent, NOT when its analysis completes.
        before_click()
        try: await self.act(item,settle=False)
        except BaseException:
            self.pending = False
            raise

    async def new_signal(self, before_click):
        await self.request_signal(before_click)

    async def screen(self):
        await self.settle()
        sym,duration = selection(self.card)
        text = '' if not sym or not duration else f'{sym[:3]}/{sym[3:6]} OTC\n{duration} sec'
        return SimpleNamespace(raw_text=text)

    async def wait_signal(self, timeout):
        if not self.pending: raise NavigationError('META_API_NOT_ARMED')
        try:
            await self.settle(timeout)
            self.check()
            if self.screen_name()!='signal' or selection(self.card)!=self.expected:
                raise SafeIssue('META_API_RESULT_MISMATCH')
            key = self.result_key()
            if (not self.new_operation_seen or key==self.before_result or key in self.consumed
                    or self.operation==self.origin_operation):
                raise SafeIssue('META_API_FRESHNESS')
            try: direction = card_direction(self.card)
            except NavigationError:
                raise SafeIssue('META_API_RESULT_MISMATCH') from None
            self.consumed.add(key)
            self.consumed_order.append(key)
            if len(self.consumed_order)>2048:
                self.consumed.discard(self.consumed_order.pop(0))
            sym,duration = self.expected
            print(f'[META_HTTP] signal={direction}; asset={sym}; seconds={duration}; '
                  f'elapsed_ms={(time.monotonic()-self.request_started)*1000:.0f}; dry_run={int(self.dry_run)}',flush=True)
            if self.dry_run:
                # SafeIssue is NOT retried by the worker's NavigationError loop.
                raise SafeIssue('META_DRY_RUN_COMPLETE')
            return f'{sym[:3]}/{sym[3:6]} OTC\n{duration} sec\nMeta Signal: {"Higher" if direction=="UP" else "Lower"}'
        finally:
            self.pending = False

    async def available(self):
        await self.to_assets()
        assets = set()
        for _ in range(80):
            for item in self.buttons():
                ctx = item.get('context') or {}
                sym = symbol(ctx.get('symbol'))
                if item.get('kind')=='timeframes' and ctx.get('market')=='o' and sym and self.usable(item):
                    assets.add(sym)
            if isinstance(self.card.get('asset_catalog'),list): return assets
            current = (self.card.get('selection') or {}).get('page',0)
            nxt = self.find(lambda b:b.get('kind')=='assets' and (b.get('context') or {}).get('market')=='o'
                and (b.get('context') or {}).get('page')==current+1)
            if not nxt: return assets
            await self.act(nxt)
        raise SafeIssue('META_API_NAVIGATION')

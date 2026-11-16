"""Real local aiohttp integration; all provider I/O is synthetic, no live signals.
Wire field names/endpoints are verified against public app.js. No private tokens.
"""
import asyncio
from copy import deepcopy
import io
import json
import os
import re
from contextlib import redirect_stdout
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import aiohttp
from aiohttp import web
from diagnostics import SafeIssue,diagnose_exception
from meta_api_client import HTTPMiniApp,MetaHTTPSettings,card_direction,selection,tf_seconds
from meta_navigation import NavigationError
from meta_webapp import AdaptiveMetaNavigator
from synchronized_signals import SynchronizedSignalListener
from meta_fixture import FakeMetaClient


class Provider:
    def __init__(self):
        self.calls=[];self.seq=0;self.op=0;self.stage='confirm';self.asset='YERUSD_otc';self.tf='M1'
        self.session='fixture-session';self.tokens={};self.signal_count=0;self.analysis=0
        self.allowed=['M1','M2','M3','M5','M10','M15','M30','H1','H4']
        self.fail=None;self.wrong=False;self.conflict=False;self.stale=False;self.same_id=False
        self.catalog=True;self.page=0

    def sel(self,asset=None,tf=None):
        a=asset or self.asset
        return dict(market='o',asset_index='~'+str({'AUDCAD_otc':67,'EURUSD_otc':1,'GBPUSD_otc':2}.get(a,99)),
            asset=a[:3]+'/'+a[3:6]+' OTC',symbol=a,broker_id=67,category='currencies',timeframe=tf or self.tf)

    def button(self,kind,ctx=None,label=None,locked=False,extra=None):
        token=f'fixture-{self.seq}-{len(self.tokens)}'
        item=dict(label=label or kind,action_token=token,kind=kind,locked=locked,context=ctx or {})
        if extra:item.update(extra)
        self.tokens[token]=deepcopy(item)
        return item

    def snapshot(self,busy=False):
        self.seq+=1;self.tokens={}
        nav=[self.button('nav',{'target':'trade'},'Trade')]
        nav[0]['key']='trade_menu'
        card=dict(id='fixture-card-'+str(self.seq),screen=self.stage,selection=self.sel(),text='',keyboard=[])
        if self.stage=='trade':
            card['keyboard']=[[self.button('assets',dict(market='o',page=0),'Trade')]]
        elif self.stage=='assets':
            card['selection']['page']=self.page
            names=['AUDCAD_otc','EURUSD_otc','GBPUSD_otc']
            items=[self.button('timeframes',self.sel(a),a,extra={'availability':'open'}) for a in names]
            if self.catalog: card['asset_catalog']=items
            else:
                card['keyboard']=[[items[self.page]]]
                if self.page<2:card['keyboard'].append([self.button('assets',dict(market='o',page=self.page+1))])
        elif self.stage in ('timeframes','confirm'):
            card['duration_choices']=[self.button('confirm',self.sel(tf=tf),tf,tf not in self.allowed) for tf in ['S5']+self.allowed]
            card['keyboard']=[[self.button('timeframes',self.sel())]]
            if self.stage=='confirm':card['keyboard'].append([self.button('signal',self.sel(),'Get signal')])
        elif self.stage=='signal':
            card['id']='signal-'+str(self.signal_count if not self.same_id else 0)
            card['issued_at_ms']=self.signal_count if not self.same_id else 0
            card['direction']='BUY' if self.signal_count%2 else 'SELL'
            if self.conflict:card['text']='Meta Signal: Lower' if card['direction']=='BUY' else 'Meta Signal: Higher'
            if self.wrong:card['selection']=self.sel('GBPUSD_otc')
            card['keyboard']=[[self.button('signal',self.sel(),'New signal')],[self.button('timeframes',self.sel())]]
        return dict(ok=True,session_id=self.session,snapshot_seq=self.seq,busy=busy,operation_id='fixture-op-'+str(self.op),
            card=card,profile=dict(can_trade=True,allowed_timeframes=self.allowed),nav=nav,
            customer={'saved_selection':self.sel()},server_time_ms=1)

    async def handle(self,request):
        body=await request.json();self.calls.append((request.match_info['endpoint'],body,dict(request.headers)))
        endpoint=request.match_info['endpoint']
        if self.fail:
            code,status=self.fail
            return web.json_response({'ok':False,'error':{'code':code}},status=status)
        if endpoint=='bootstrap':
            return web.json_response(self.snapshot())
        if body.get('session_id')!=self.session:
            return web.json_response({'ok':False,'error':{'code':'session_expired'}},status=403)
        if endpoint=='status':
            if self.analysis:
                self.analysis-=1
                if not self.analysis:self.stage='signal'
            data=self.snapshot(busy=bool(self.analysis))
            if self.stale:
                self.stale=False;data['snapshot_seq']=1;data['busy']=False
            return web.json_response(data)
        if endpoint=='action':
            button=self.tokens.get(body.get('action_token'))
            if not button:return web.json_response({'ok':False,'error':{'code':'stale_action'}},status=409)
            ctx=button['context'];kind=button['kind'];self.op+=1
            if kind=='nav':self.stage='trade'
            elif kind=='assets':self.stage='assets';self.page=ctx['page']
            elif kind=='timeframes':self.stage='timeframes';self.asset=ctx['symbol']
            elif kind=='confirm':self.stage='confirm';self.asset=ctx['symbol'];self.tf=ctx['timeframe']
            elif kind=='signal':self.stage='analysis';self.analysis=2;self.signal_count+=1
            return web.json_response(self.snapshot(busy=bool(self.analysis)))
        raise AssertionError('unobserved endpoint')


class SchemaTests(unittest.TestCase):
    def test_timeframes(self):
        self.assertEqual([tf_seconds(x) for x in ['M1','M2','H4','S5']], [60,120,14400,5])
        self.assertIsNone(tf_seconds('M0'));self.assertIsNone(tf_seconds('minute'))

    def test_structured_buy_and_sell(self):
        self.assertEqual(card_direction({'direction':'BUY'}),'UP')
        self.assertEqual(card_direction({'direction':'SELL'}),'DOWN')

    def test_text_fallback_and_russian(self):
        self.assertEqual(card_direction({'text':'<b>Meta Signal: Higher</b>'}),'UP')
        self.assertEqual(card_direction({'text':'Meta Signal: Ниже'}),'DOWN')

    def test_conflicting_or_unknown_rejected(self):
        for card in [{'direction':'BUY','text':'Meta Signal: Lower'}, {'direction':'MAYBE'},{'text':'69%'}]:
            with self.assertRaises(NavigationError):card_direction(card)

    def test_selection_rejects_regular_or_conflicting_pair(self):
        for s in [dict(market='s',symbol='AUDCAD_otc',timeframe='M1'),dict(market='o',symbol='AUDCAD_otc',asset='EUR/USD OTC',timeframe='M1')]:
            self.assertEqual(selection({'selection':s}),(None,None))

    def test_environment_validation_rejects_invalid_configuration(self):
        for key,value in [('META_API_BASE','http://example.org/miniapp/api'),('META_API_BASE','https://example.org/miniapp/api?secret=x'),('META_BOT_ID','abc'),('META_DRY_RUN','maybe'),('META_POLL_SECONDS','0'),('META_POLL_SECONDS','nan'),('META_HTTP_TIMEOUT_SECONDS','inf')]:
            with patch.dict(os.environ,{key:value}):
                with self.assertRaises(SafeIssue):MetaHTTPSettings()

    def test_supplemental_audcad_does_not_change_legacy_pages(self):
        from cloud_settings import defaults,patched
        from forex_assets import FOREX_OTC
        from control_bot import assets_kb
        self.assertEqual(len(FOREX_OTC),30)
        self.assertEqual(patched(defaults(),{'trading.asset':'AUDCAD_otc'})['trading']['asset'],'AUDCAD_otc')
        self.assertIn('asset:AUDCAD_otc',[b.callback_data for row in assets_kb(0).inline_keyboard for b in row])

    def test_dry_run_defaults_on(self):
        with patch.dict(os.environ,{},clear=True):self.assertTrue(MetaHTTPSettings().dry_run)

    def test_diagnostics_never_echo_exception_secrets(self):
        diag=diagnose_exception(RuntimeError('PRIVATE_INIT_DATA_AND_TOKEN'),'meta_signal')
        self.assertNotIn('PRIVATE',diag.public_text()+diag.log_line())


class RouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_http_does_not_fall_back_to_legacy(self):
        client=FakeMetaClient();nav=AdaptiveMetaNavigator(client,SimpleNamespace(id=8808473133))
        nav.discover=AsyncMock(return_value=None)
        with patch.dict(os.environ,{'META_SIGNAL_BACKEND':'http'}),patch('meta_webapp.asyncio.sleep',new=AsyncMock()):
            with self.assertRaisesRegex(SafeIssue,'META_API_NAVIGATION'):await nav.ensure()
        self.assertEqual(client.clicks,[])
        self.assertEqual(client.sent,['/start'])

    async def test_http_uses_adapter_not_browser(self):
        nav=AdaptiveMetaNavigator(FakeMetaClient(),SimpleNamespace(id=8808473133))
        nav.discover=AsyncMock(return_value=('https://sdasdsahdsa.lol/miniapp/','menu'))
        stub=SimpleNamespace(launch=AsyncMock())
        with patch.dict(os.environ,{'META_SIGNAL_BACKEND':'http'}),patch('meta_api_client.HTTPMiniApp',return_value=stub):
            await nav.ensure()
        self.assertIs(nav.web,stub)
        stub.launch.assert_awaited_once()

    async def test_explicit_telegram_keeps_legacy_navigator(self):
        nav=AdaptiveMetaNavigator(FakeMetaClient(),SimpleNamespace(id=1))
        nav.discover=AsyncMock()
        with patch.dict(os.environ,{'META_SIGNAL_BACKEND':'telegram'}):await nav.ensure()
        nav.discover.assert_not_awaited()
        self.assertIsNone(nav.web)


class HTTPIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider=Provider()
        app=web.Application();app.router.add_post('/miniapp/api/{endpoint}',self.provider.handle)
        self.runner=web.AppRunner(app);await self.runner.setup()
        site=web.TCPSite(self.runner,'127.0.0.1',0);await site.start()
        port=self.runner.addresses[0][1]
        self.client=FakeMetaClient()
        self.listener=SynchronizedSignalListener(self.client);await self.listener.init()
        self.owner=self.listener.nav
        self.settings=SimpleNamespace(base=f'http://127.0.0.1:{port}/miniapp/api',bot_id='8808473133',
            dry_run=False,language='ar-YE',poll=.001,timeout=1,operation_timeout=1)
        self.http=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1))
        self.adapter=HTTPMiniApp(self.owner,self.settings,self.http)
        self.adapter.init_data='FIXTURE_ONLY_NOT_A_REAL_CREDENTIAL'
        self.owner.web=self.adapter
        await self.adapter.api('bootstrap',reset_view=True,device_language='ar-YE')

    async def asyncTearDown(self):
        await self.listener.close();await self.http.close();await self.runner.cleanup()

    def endpoints(self):return [x[0] for x in self.provider.calls]
    def actions(self):return [x for x in self.provider.calls if x[0]=='action']

    async def test_bootstrap_headers_and_body_match_public_frontend(self):
        endpoint,body,headers=self.provider.calls[0]
        self.assertEqual(endpoint,'bootstrap')
        self.assertEqual(body,dict(bot_id='8808473133',reset_view=True,device_language='ar-YE'))
        self.assertEqual(headers['X-Telegram-Init-Data'],'FIXTURE_ONLY_NOT_A_REAL_CREDENTIAL')

    async def test_end_to_end_first_signal_reads_up(self):
        self.assertEqual(await self.listener.next_direction('AUDCAD_otc',60),'UP')
        self.assertTrue(self.listener.verifies('AUDCAD_otc',60))
        self.assertEqual(self.provider.asset,'AUDCAD_otc')
        self.assertIn('status',self.endpoints())

    async def test_repeat_uses_only_one_new_signal_action(self):
        await self.listener.next_direction('AUDCAD_otc',60)
        n=len(self.actions());total_boot=self.endpoints().count('bootstrap')
        self.assertEqual(await self.listener.next_direction('AUDCAD_otc',60),'DOWN')
        self.assertEqual(len(self.actions())-n,1)
        self.assertEqual(self.endpoints().count('bootstrap'),total_boot)

    async def test_change_currency_and_duration(self):
        await self.listener.next_direction('AUDCAD_otc',60)
        self.assertEqual(await self.listener.next_direction('EURUSD_otc',180),'DOWN')
        self.assertTrue(self.listener.verifies('EURUSD_otc',180))
        self.assertEqual(self.provider.tf,'M3')

    async def test_duration_only_change(self):
        await self.listener.next_direction('AUDCAD_otc',60)
        await self.listener.next_direction('AUDCAD_otc',120)
        self.assertEqual(self.provider.asset,'AUDCAD_otc');self.assertEqual(self.provider.tf,'M2')

    async def test_saved_final_card_does_not_get_reused(self):
        self.provider.stage='signal';self.provider.asset='AUDCAD_otc';self.provider.signal_count=7
        self.adapter.accept(self.provider.snapshot())
        self.assertEqual(await self.listener.next_direction('AUDCAD_otc',60),'DOWN')
        self.assertEqual(len(self.actions()),1)

    async def test_saved_matching_confirm_starts_directly(self):
        self.provider.asset='AUDCAD_otc';self.adapter.accept(self.provider.snapshot())
        await self.listener.next_direction('AUDCAD_otc',60)
        self.assertEqual(len(self.actions()),1)

    async def test_locked_seconds_reject_without_action(self):
        with self.assertRaisesRegex(SafeIssue,'META_API_DURATION'):
            await self.listener.next_direction('AUDCAD_otc',5)
        self.assertEqual(len(self.actions()),0)

    async def test_no_access_reject_without_signal(self):
        self.adapter.profile['can_trade']=False
        with self.assertRaisesRegex(SafeIssue,'META_API_ACCESS'):
            await self.listener.next_direction('AUDCAD_otc',60)
        self.assertEqual(self.provider.signal_count,0)

    async def test_missing_pair_does_not_select_other_pair(self):
        with self.assertRaisesRegex(SafeIssue,'META_API_ASSET'):
            await self.listener.next_direction('USDJPY_otc',60)
        self.assertEqual(self.provider.signal_count,0)

    async def test_dry_run_stops_worker_path_and_clears_prepared(self):
        self.adapter.dry_run=True
        with self.assertRaisesRegex(SafeIssue,'META_DRY_RUN_COMPLETE'):
            await self.listener.next_direction('AUDCAD_otc',60)
        self.assertIsNone(self.listener.prepared);self.assertIsNone(self.owner.web)
        self.assertEqual(self.provider.signal_count,1)
        self.assertFalse(self.listener.verifies('AUDCAD_otc',60))

    async def test_wrong_result_selection_rejected(self):
        self.provider.wrong=True
        with self.assertRaisesRegex(SafeIssue,'META_API_RESULT_MISMATCH'):
            await self.listener.next_direction('AUDCAD_otc',60)
        self.assertFalse(self.listener.verifies('AUDCAD_otc',60))

    async def test_conflicting_direction_rejected(self):
        self.provider.conflict=True
        with self.assertRaisesRegex(SafeIssue,'META_API_RESULT_MISMATCH'):
            await self.listener.next_direction('AUDCAD_otc',60)

    async def test_old_snapshot_not_accepted(self):
        data=self.provider.snapshot();self.adapter.accept(data)
        old=deepcopy(data);old['snapshot_seq']=1;old['card']['selection']=self.provider.sel('GBPUSD_otc')
        self.assertFalse(self.adapter.accept(old))
        self.assertNotEqual(selection(self.adapter.card)[0],'GBPUSD_otc')

    async def test_session_change_resets_snapshot_counter_only_when_not_pending(self):
        data=self.provider.snapshot();data['session_id']='new-fixture';data['snapshot_seq']=1
        self.adapter.accept(data);self.assertEqual(self.adapter.sequence,1)
        self.adapter.pending=True;data['session_id']='third-fixture'
        with self.assertRaisesRegex(SafeIssue,'META_API_SESSION_CHANGED'):self.adapter.accept(data)

    async def test_status_may_omit_card(self):
        saved=self.adapter.card
        self.adapter.accept(dict(ok=True,busy=False,session_id=self.provider.session,snapshot_seq=99,card=None))
        self.assertEqual(self.adapter.card,saved)

    async def test_status_wire_has_session_and_no_operation_id(self):
        await self.adapter.api('status')
        body=self.provider.calls[-1][1]
        self.assertEqual(set(body),{'bot_id','session_id'})

    async def test_action_uses_uuid_request_id(self):
        await self.listener.next_direction('AUDCAD_otc',60)
        for _,body,_ in self.actions():
            self.assertEqual(set(body),{'bot_id','session_id','action_token','request_id'})
            self.assertRegex(body['request_id'],r'^[0-9a-f-]{36}$')
        self.assertEqual(len({x[1]['request_id'] for x in self.actions()}),len(self.actions()))

    async def test_action_server_error_not_replayed(self):
        self.provider.fail=('server_error',503)
        token=next(iter(self.provider.tokens))
        with self.assertRaisesRegex(SafeIssue,'META_API_ACTION_UNCERTAIN'):
            await self.adapter.act(dict(action_token=token))
        self.assertEqual(len(self.actions()),1)

    async def test_rate_limit_is_terminal(self):
        self.provider.fail=('rate_limit',429)
        with self.assertRaisesRegex(SafeIssue,'META_API_RATE_LIMIT'):await self.adapter.api('status')

    async def test_auth_error_never_echoes_initdata(self):
        self.provider.fail=('invalid_auth',401)
        with self.assertRaisesRegex(SafeIssue,'META_API_AUTH') as caught:await self.adapter.api('status')
        self.assertNotIn('FIXTURE_ONLY',str(caught.exception))

    async def test_session_expired_is_terminal(self):
        self.provider.fail=('session_expired',403)
        with self.assertRaisesRegex(SafeIssue,'META_API_SESSION_EXPIRED'):await self.adapter.api('status')

    async def test_expired_capability_is_not_retried(self):
        self.provider.fail=('expired_action',409)
        with self.assertRaisesRegex(SafeIssue,'META_API_STALE_ACTION'):
            await self.adapter.act(dict(action_token='fixture-old'))
        self.assertEqual(len(self.actions()),1)

    async def test_cancel_before_navigation_sends_nothing(self):
        self.owner.cancel=asyncio.Event();self.owner.cancel.set()
        with self.assertRaises(asyncio.CancelledError):await self.adapter.configure('AUDCAD_otc',60,lambda:None)
        self.assertEqual(len(self.actions()),0)

    async def test_available_reads_catalog_without_requesting_signal(self):
        self.assertEqual(await self.adapter.available(),{'AUDCAD_otc','EURUSD_otc','GBPUSD_otc'})
        self.assertEqual(self.provider.signal_count,0)

    async def test_paged_catalog_uses_only_observed_page_tokens(self):
        self.provider.catalog=False
        self.assertEqual(await self.adapter.available(),{'AUDCAD_otc','EURUSD_otc','GBPUSD_otc'})
        self.assertEqual(self.provider.signal_count,0)

    async def test_same_direction_twice_is_allowed_when_fresh(self):
        self.assertEqual(await self.listener.next_direction('AUDCAD_otc',60),'UP')
        self.provider.signal_count=2
        self.assertEqual(await self.listener.next_direction('AUDCAD_otc',60),'UP')

    async def test_cannot_consume_one_result_twice(self):
        await self.adapter.configure('AUDCAD_otc',60,lambda:None)
        await self.adapter.wait_signal(1)
        with self.assertRaisesRegex(NavigationError,'NOT_ARMED'):await self.adapter.wait_signal(1)

    async def test_freshness_requires_new_operation(self):
        await self.adapter.configure('AUDCAD_otc',60,lambda:None)
        await self.adapter.settle();self.adapter.new_operation_seen=False
        with self.assertRaisesRegex(SafeIssue,'META_API_FRESHNESS'):await self.adapter.wait_signal(1)

    async def test_no_stable_identity_is_not_accepted_as_new(self):
        self.provider.same_id=True
        await self.listener.next_direction('AUDCAD_otc',60)
        self.provider.signal_count=2
        with self.assertRaisesRegex(SafeIssue,'META_API_FRESHNESS'):
            await self.listener.next_direction('AUDCAD_otc',60)

    async def test_logs_redact_session_and_tokens(self):
        out=io.StringIO()
        with redirect_stdout(out):await self.listener.next_direction('AUDCAD_otc',60)
        value=out.getvalue()
        for private in [self.provider.session,'FIXTURE_ONLY_NOT_A_REAL_CREDENTIAL','fixture-op-','fixture-1-']:
            self.assertNotIn(private,value)

    async def test_launch_obtains_real_requestwebview_contract_without_browser(self):
        # A synthetic signed URL tests parsing only; it is never fetched.
        self.owner.entity=SimpleNamespace(id=8808473133,username='signal_meta_bot')
        init='user=%7B%22id%22%3A1%7D&auth_date=1&hash=fixture'
        signed='https://'+self.settings.base.split('/')[2]+'/miniapp/?bot_id=8808473133#'+urlencode({'tgWebAppData':init})
        self.owner.client=AsyncMock(return_value=SimpleNamespace(url=signed))
        await self.adapter.launch(('https://sdasdsahdsa.lol/miniapp/?bot_id=8808473133','menu'))
        self.assertEqual(self.adapter.init_data,init)
        request=self.owner.client.await_args.args[0]
        self.assertEqual(type(request).__name__,'RequestWebViewRequest')
        self.assertTrue(request.from_bot_menu)

    async def api_worker(self):
        from test_v16_features import WorkerLimitsTests
        worker=await WorkerLimitsTests().make_worker([('win',.9)],mode='TRADITIONAL',stake=1,stop_on_exhaustion=True)
        await worker.repo.update({'payout.mode':'OFF','trading.asset':'AUDCAD_otc','trading.duration_seconds':60})
        worker.listener=self.listener
        return worker

    async def test_worker_dry_run_sends_zero_orders_and_stops_once(self):
        self.adapter.dry_run=True
        worker=await self.api_worker()
        await asyncio.wait_for(worker._run(),2)
        worker.po.place_trade.assert_not_awaited()
        self.assertEqual(worker.last_error,'META_DRY_RUN_COMPLETE')
        self.assertFalse(worker.running)
        self.assertEqual(self.provider.signal_count,1)

    async def test_worker_orders_only_after_fresh_verified_signal(self):
        worker=await self.api_worker()
        await asyncio.wait_for(worker._run(),2)
        worker.po.place_trade.assert_awaited_once_with('UP',1)
        self.assertEqual(self.provider.signal_count,1)

    async def test_http_error_is_terminal_in_worker_not_infinite_signal_retries(self):
        self.provider.fail=('rate_limit',429)
        worker=await self.api_worker()
        await asyncio.wait_for(worker._run(),2)
        worker.po.place_trade.assert_not_awaited()
        self.assertEqual(worker.last_error,'META_API_RATE_LIMIT')
        self.assertFalse(worker.running)

    async def test_cancelled_inflight_action_marks_state_uncertain(self):
        started=asyncio.Event()
        class SlowHTTP:
            def post(self,*args,**kwargs):
                class Response:
                    async def __aenter__(self):
                        started.set();await asyncio.Event().wait()
                    async def __aexit__(self,*args):pass
                return Response()
        self.adapter.http=SlowHTTP()
        task=asyncio.create_task(self.adapter.api('action',action_token='fixture',request_id='fixture'))
        await started.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(self.adapter.uncertain_action)
        with self.assertRaisesRegex(SafeIssue,'META_API_ACTION_UNCERTAIN'):self.adapter.check()

    async def test_malformed_nested_selection_rejected(self):
        data=self.provider.snapshot();data['card']['selection']='not-an-object'
        with self.assertRaisesRegex(SafeIssue,'META_API_SCHEMA'):self.adapter.accept(data)

    async def test_wrong_bot_id_rejected_before_http(self):
        self.owner.entity=SimpleNamespace(id=1)
        with self.assertRaisesRegex(SafeIssue,'META_API_BOT_MISMATCH'):
            await self.adapter.launch(('https://sdasdsahdsa.lol/miniapp/','menu'))

    async def test_different_launch_origin_rejected(self):
        self.owner.entity=SimpleNamespace(id=8808473133,username='signal_meta_bot')
        self.owner.client=AsyncMock(return_value=SimpleNamespace(url='https://evil.invalid/miniapp/#tgWebAppData=private'))
        with self.assertRaisesRegex(SafeIssue,'META_API_ORIGIN'):
            await self.adapter.launch(('https://sdasdsahdsa.lol/miniapp/','menu'))

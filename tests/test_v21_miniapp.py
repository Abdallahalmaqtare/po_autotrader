"""Browser integration against a local fixture, NOT a live provider test.
The actual provider DOM is not present in the supplied archive/video.
"""
import asyncio
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from playwright.async_api import async_playwright
from meta_webapp import MiniApp, AdaptiveMetaNavigator, live_selection, HOST_BRIDGE
from meta_navigation import NavigationError
from synchronized_signals import SynchronizedSignalListener
from meta_fixture import FakeMetaClient


FIXTURE = r"""<meta charset="utf-8"><style>button{display:block;margin:5px}body{font:16px sans-serif}</style><main id="root"></main>
<script>
let state='config', asset='YER/USD', duration=60, seq=0;
window.clicks=[];window.lockSeconds=true;window.stale=false;window.wrong=false;window.conflict=false;window.nativeBack=false;
const root=document.getElementById('root');
function b(t,fn,disabled=false){const x=document.createElement('button');x.textContent=t;x.disabled=disabled;x.onclick=()=>{clicks.push(t);fn()};root.append(x);return x}
function text(t){const e=document.createElement('div');e.innerText=t;root.append(e)}
function render(){
 root.innerHTML='';
 window.__metaHost=window.__metaHost||{};
 window.__metaHost.backVisible=state!=='start'&&window.nativeBack;
 if(state!=='start'&&!window.nativeBack)b('Back',()=>{state='start';render()});
 if(state==='start'){text('META TRADING\nGet signal');b('Start',()=>{state='categories';render()})}
 if(state==='categories'){text('Choose an asset');b('Currencies\n79 Instruments',()=>{state='assets';render()});b('Crypto',()=>{})}
 if(state==='assets'){
   b('OTC\n58 Instruments',()=>{});b('Regular',()=>{});
   const s=document.createElement('input');s.placeholder='Search all assets';root.append(s);
   for(const p of ['AUD/CAD','EUR/USD','GBP/USD'])b(p+'\nOTC',()=>{asset=p;state='config';render()});
 }
 if(state==='config'){
   text(asset+'\nOTC\nCurrencies\nTrade duration');
   for(const [l,v] of [['S5',5],['S15',15],['S30',30],['M1',60],['M3',180],['M5',300],['M10',600],['M30',1800],['H1',3600]])b(l,()=>{duration=v;render()},v<60&&window.lockSeconds);
   text('Fine adjustment\n'+duration/60+' min');
   const range=document.createElement('input');range.type='range';range.min=1;range.max=240;range.step=1;range.value=duration/60;
   range.oninput=()=>{duration=+range.value*60;root.querySelector('#fine').innerText='Fine adjustment\n'+duration/60+' min'};
   root.lastElementChild.id='fine';root.append(range);
   text('1 min\n4 h');b('Get signal →',request);
 }
 if(state==='analysis'){text(asset+' OTC\nM'+duration/60+'\nPreparing your signal\nAnalysis visualization')}
 if(state==='ready'){
   text('SIGNAL READY\n'+(window.wrong?'GBP/USD':asset)+' OTC\nM'+duration/60);
   text(window.conflict?'Higher\nLower':(seq%2?'Higher':'Lower'));
   text('Current Time (UTC+3)\n23:56:59\nConfidence\n69%');
   b('New signal →',request);b('Change - Trading asset >',()=>{state='start';render()});
 }
}
function request(){if(window.stale)return;state='analysis';render();setTimeout(()=>{seq++;state='ready';render()},120)}
window.Telegram={WebView:{receiveEvent(name){if(name==='back_button_pressed'){clicks.push('Native Back');state='start';render()}}}};
render();
</script>"""


class SelectionTests(unittest.TestCase):
    def test_config_does_not_read_all_presets_as_selected_duration(self):
        self.assertEqual(live_selection('AUD/CAD OTC\nM1 M3 M5 H1\nFine adjustment\n1 min\n1 min\n4 h'),('AUDCAD_otc',60))

    def test_signal_hour_and_parenthesized_otc(self):
        self.assertEqual(live_selection('SIGNAL READY\nAUD/CAD (OTC)\nH1\nHigher'),('AUDCAD_otc',3600))

    def test_multiple_pairs_or_durations_fail_closed(self):
        self.assertEqual(live_selection('SIGNAL READY\nAUD/CAD OTC\nEUR/USD OTC\nM1 M3\nHigher'),(None,None))

    def test_range_end_labels_do_not_prove_selected_duration(self):
        self.assertEqual(live_selection('AUD/CAD OTC\nM1 M3\n1 min\n4 h'),('AUDCAD_otc',None))


class MiniAppBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw=await async_playwright().start()
        self.browser=await self.pw.chromium.launch()
        self.page=await self.browser.new_page()
        await self.page.set_content(FIXTURE)
        self.client=FakeMetaClient()
        self.listener=SynchronizedSignalListener(self.client)
        await self.listener.init()
        self.nav=self.listener.nav
        self.web=MiniApp(self.nav)
        self.web.page=self.page
        self.nav.web=self.web
        self.nav.timeout=1

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def clicks(self): return await self.page.evaluate('window.clicks')

    async def test_reopened_configuration_goes_back_start_categories_otc_pair_duration(self):
        self.assertEqual(await self.listener.next_direction('AUDCAD_otc',60),'UP')
        self.assertEqual(await self.clicks(),['Back','Start','Currencies\n79 Instruments','OTC\n58 Instruments','AUD/CAD\nOTC','M1','Get signal →'])
        self.assertTrue(self.listener.verifies('AUDCAD_otc',60))

    async def test_native_telegram_back_is_dispatched_without_closing_webview(self):
        await self.page.evaluate('window.nativeBack=true;render()')
        self.assertEqual(await self.listener.next_direction('AUDCAD_otc',60),'UP')
        self.assertEqual((await self.clicks())[0],'Native Back')
        self.assertFalse(self.page.is_closed())

    async def test_same_selection_repeats_new_signal_only(self):
        await self.listener.next_direction('AUDCAD_otc',60)
        clicks=await self.clicks()
        self.assertEqual(await self.listener.next_direction('AUDCAD_otc',60),'DOWN')
        self.assertEqual((await self.clicks())[len(clicks):],['New signal →'])

    async def test_changed_currency_restarts_flow_and_reads_new_pair(self):
        await self.listener.next_direction('AUDCAD_otc',60)
        n=len(await self.clicks())
        self.assertEqual(await self.listener.next_direction('EURUSD_otc',180),'DOWN')
        self.assertEqual((await self.clicks())[n:],['Back','Start','Currencies\n79 Instruments','OTC\n58 Instruments','EUR/USD\nOTC','M3','Get signal →'])
        self.assertTrue(self.listener.verifies('EURUSD_otc',180))
        self.assertFalse(self.listener.verifies('AUDCAD_otc',60))

    async def test_changed_duration_alone_reconfigures_same_pair(self):
        await self.listener.next_direction('AUDCAD_otc',60)
        n=len(await self.clicks())
        self.assertEqual(await self.listener.next_direction('AUDCAD_otc',180),'DOWN')
        self.assertEqual((await self.clicks())[n:],['Back','Start','Currencies\n79 Instruments','OTC\n58 Instruments','AUD/CAD\nOTC','M3','Get signal →'])

    async def test_identical_consecutive_direction_is_fresh_after_analysis(self):
        self.assertEqual(await self.listener.next_direction('AUDCAD_otc',60),'UP')
        await self.page.evaluate('seq=0')
        self.assertEqual(await self.listener.next_direction('AUDCAD_otc',60),'UP')
        self.assertEqual((await self.clicks()).count('New signal →'),1)

    async def test_existing_matching_final_card_skips_back_and_start(self):
        await self.page.evaluate("asset='AUD/CAD';state='ready';render()")
        self.assertEqual(await self.listener.next_direction('AUDCAD_otc',60),'UP')
        self.assertEqual(await self.clicks(),['New signal →'])

    async def test_first_signal_from_synchronize_not_requested_twice(self):
        await self.listener.synchronize('AUDCAD_otc',60)
        self.assertEqual(await self.listener.next_direction('AUDCAD_otc',60),'UP')
        self.assertEqual((await self.clicks()).count('Get signal →'),1)
        self.assertNotIn('New signal →',await self.clicks())

    async def test_locked_seconds_never_fall_back_to_one_minute(self):
        with self.assertRaisesRegex(NavigationError,'DURATION_UNAVAILABLE'):
            await self.listener.next_direction('AUDCAD_otc',5)
        self.assertNotIn('Get signal →',await self.clicks())

    async def test_custom_duration_uses_observed_range_scale(self):
        self.assertEqual(await self.listener.next_direction('AUDCAD_otc',900),'UP')
        self.assertTrue(self.listener.verifies('AUDCAD_otc',900))

    async def test_hours_are_selected_using_h1_not_m60(self):
        self.assertEqual(await self.listener.next_direction('AUDCAD_otc',3600),'UP')
        self.assertIn('H1',await self.clicks())

    async def test_no_intermediate_analysis_never_accepts_stale_ready_card(self):
        await self.page.evaluate("asset='AUD/CAD';state='ready';window.stale=true;render()")
        self.web.expected=('AUDCAD_otc',60)
        await self.web.new_signal(lambda:None)
        with self.assertRaisesRegex(NavigationError,'SCREEN_TIMEOUT'):
            await self.web.wait_signal(.25)

    async def test_wrong_pair_final_card_rejected(self):
        await self.page.evaluate('window.wrong=true')
        await self.web.configure('AUDCAD_otc',60,lambda:None)
        with self.assertRaisesRegex(NavigationError,'SIGNAL_SELECTION_MISMATCH'):
            await self.web.wait_signal(1)

    async def test_conflicting_directions_rejected(self):
        await self.page.evaluate('window.conflict=true')
        await self.web.configure('AUDCAD_otc',60,lambda:None)
        with self.assertRaisesRegex(NavigationError,'SCREEN_TIMEOUT'):
            await self.web.wait_signal(.4)

    async def test_cancel_stops_before_any_ui_click(self):
        self.nav.cancel=asyncio.Event();self.nav.cancel.set()
        with self.assertRaises(asyncio.CancelledError):
            await self.web.configure('AUDCAD_otc',60,lambda:None)
        self.assertEqual(await self.clicks(),[])

    async def test_missing_pair_never_selects_another_pair(self):
        with self.assertRaises(NavigationError):
            await self.web.configure('USDJPY_otc',60,lambda:None)
        self.assertNotIn('Get signal →',await self.clicks())

    async def test_available_reads_actual_otc_rows(self):
        self.assertEqual(await self.web.available(),{'AUDCAD_otc','EURUSD_otc','GBPUSD_otc'})

    async def test_ready_card_can_be_consumed_only_once(self):
        await self.web.configure('AUDCAD_otc',60,lambda:None)
        await self.web.wait_signal(1)
        with self.assertRaisesRegex(NavigationError,'NOT_ARMED'):
            await self.web.wait_signal(1)

    async def test_external_selection_change_blocks_new_request(self):
        await self.listener.next_direction('AUDCAD_otc',60)
        await self.page.evaluate("asset='GBP/USD';render()")
        with self.assertRaisesRegex(NavigationError,'SELECTION_CHANGED'):
            await self.listener.next_direction('AUDCAD_otc',60)
        self.assertNotIn('New signal →',await self.clicks())


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_beta_webview_detected_without_clicking_url_as_callback(self):
        client=FakeMetaClient()
        client.current.buttons=[[SimpleNamespace(text='Beta App',url='https://example.org/app',button=SimpleNamespace(url='https://example.org/app'))]]
        nav=AdaptiveMetaNavigator(client,SimpleNamespace(id=1))
        self.assertEqual(await nav.discover(),('https://example.org/app','inline'))
        self.assertEqual(client.clicks,[])

    async def test_persistent_menu_can_supply_app_url(self):
        class Client(FakeMetaClient):
            async def __call__(self,request):
                return SimpleNamespace(full_user=SimpleNamespace(bot_info=SimpleNamespace(menu_button=SimpleNamespace(url='https://example.org/app'))))
        client=Client();client.current.buttons=[]
        nav=AdaptiveMetaNavigator(client,SimpleNamespace(id=1))
        self.assertEqual(await nav.discover(),('https://example.org/app','menu'))

    async def test_expired_query_fails_safely_without_printing_secret_url(self):
        nav=AdaptiveMetaNavigator(AsyncMock(side_effect=RuntimeError('SECRET')),SimpleNamespace(id=1))
        web=MiniApp(nav)
        with self.assertRaisesRegex(NavigationError,'AUTH_FAILED') as caught:
            await web.launch(('https://example.org/app','menu'))
        self.assertNotIn('SECRET',str(caught.exception))

    async def test_webview_keepalive_uses_existing_client(self):
        client=AsyncMock()
        web=MiniApp(AdaptiveMetaNavigator(client,SimpleNamespace(id=1)))
        web.query_id=123;web.renewed_at=0
        await web.renew()
        self.assertEqual(client.await_args.args[0].query_id,123)

    async def test_bridge_tracks_native_back_button(self):
        pw=await async_playwright().start();browser=await pw.chromium.launch()
        try:
            page=await browser.new_page()
            await page.evaluate(HOST_BRIDGE)
            await page.evaluate("TelegramWebviewProxy.postEvent('web_app_setup_back_button','{\"is_visible\":true}')")
            self.assertTrue(await page.evaluate('window.__metaHost.backVisible'))
        finally:
            await browser.close();await pw.stop()

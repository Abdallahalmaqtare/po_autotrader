"""Mini App adapter. Existing Telegram navigator and trading/session code stay intact.
Only observed UI controls are used; no private provider endpoints are guessed.
"""
import asyncio
import re
import time
from contextlib import suppress
from types import SimpleNamespace
from urllib.parse import urlparse, parse_qs
from meta_navigation import MetaNavigator, NavigationError, clean
from payout_guard import symbol


# Telegram's official JS SDK talks to this host bridge in an Android WebView.
# Authentication remains the signed URL returned by the EXISTING MTProto client.
HOST_BRIDGE = r"""(() => {
  window.__metaHost = {backVisible:false};
  window.TelegramWebviewProxy = {postEvent(type, raw) {
    let data={}; try {data=JSON.parse(raw||'{}')} catch (_) {}
    const emit=(name,value)=>setTimeout(()=>{
      window.Telegram?.WebView?.receiveEvent(name,value);
    },0);
    if(type==='web_app_setup_back_button') window.__metaHost.backVisible=!!data.is_visible;
    if(type==='web_app_request_theme') emit('theme_changed',{theme_params:{bg_color:'#0b0c13',text_color:'#ffffff',button_color:'#58b9e7',button_text_color:'#ffffff',secondary_bg_color:'#141822',hint_color:'#8a8f99'}});
    if(type==='web_app_request_viewport') emit('viewport_changed',{height:window.innerHeight,is_state_stable:true,is_expanded:true});
    if(type==='web_app_request_safe_area') emit('safe_area_changed',{top:0,bottom:0,left:0,right:0});
    if(type==='web_app_request_content_safe_area') emit('content_safe_area_changed',{top:0,bottom:0,left:0,right:0});
  }};
})();"""


def live_selection(text):
    pairs = re.findall(r'\b([A-Z]{3}\s*/\s*[A-Z]{3})\s*(?:\(\s*OTC\s*\)|OTC)(?!\w)', text, re.I)
    assets = {symbol(p + ' OTC') for p in pairs}
    asset = next(iter(assets)) if len(assets) == 1 else None
    duration = None
    # Read the CURRENT value, not the list of all available preset buttons.
    m = re.search(r'Fine\s+adjustment\s*[:\n ]*([0-9]+)\s*(min(?:utes?)?|h(?:ours?)?|sec(?:onds?)?)\b', text, re.I)
    if m:
        duration = int(m[1]) * (3600 if m[2].lower().startswith('h') else 60 if m[2].lower().startswith('m') else 1)
    if re.search(r'\bSIGNAL\s+READY\b', text, re.I):
        tags = re.findall(r'(?<!\w)([MSH])(\d+)(?!\w)', text, re.I)
        values = {int(n) * {'S':1,'M':60,'H':3600}[u.upper()] for u,n in tags}
        duration = next(iter(values)) if len(values) == 1 else None
    return asset, duration


class MiniApp:
    def __init__(self, owner):
        self.owner = owner
        self.page = None
        self.browser = None
        self.playwright = None
        self.query_id = None
        self.renewed_at = 0.0
        self.expected = None
        self.request_pending = False
        self.delivered = False
        self.keepalive = None
        self.keepalive_error = False

    def check(self):
        self.owner.check()
        if self.keepalive_error:
            raise NavigationError('META_WEBAPP_SESSION_EXPIRED')

    async def launch(self, launch):
        from telethon import functions, types
        url, kind = launch
        kwargs = dict(bot=self.owner.entity, platform='android')
        if kind == 'simple':
            req = functions.messages.RequestSimpleWebViewRequest(url=url, **kwargs)
        elif urlparse(url).hostname in ('t.me','telegram.me'):
            parsed = urlparse(url)
            path = parsed.path.strip('/').split('/')
            if len(path) != 2:
                raise NavigationError('META_WEBAPP_LINK_UNSUPPORTED')
            # Do not follow a link to a different bot with the user's identity.
            name = (getattr(self.owner.entity, 'username', '') or '').lower()
            if path[0].lower() != name:
                raise NavigationError('META_WEBAPP_BOT_MISMATCH')
            req = functions.messages.RequestAppWebViewRequest(
                peer=self.owner.entity,
                app=types.InputBotAppShortName(self.owner.entity, path[1]),
                platform='android', start_param=parse_qs(parsed.query).get('startapp',[None])[0])
        else:
            if urlparse(url).scheme != 'https':
                raise NavigationError('META_WEBAPP_URL_INVALID')
            req = functions.messages.RequestWebViewRequest(peer=self.owner.entity, url=url,
                from_bot_menu=(kind == 'menu'), **kwargs)
        try:
            result = await asyncio.wait_for(self.owner.client(req), self.owner.timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise NavigationError('META_WEBAPP_AUTH_FAILED') from None
        self.query_id = getattr(result, 'query_id', None)
        self.renewed_at = time.monotonic()
        try:
            from playwright.async_api import async_playwright
            self.playwright = await async_playwright().start()
            # Chromium sandbox remains enabled/disabled according to Playwright defaults.
            self.browser = await self.playwright.chromium.launch(headless=True)
            context = await self.browser.new_context(viewport={'width':498,'height':950},
                is_mobile=True, has_touch=True, locale='en-US',
                user_agent='Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36')
            await context.add_init_script(HOST_BRIDGE)
            self.page = await context.new_page()
            self.page.set_default_timeout(self.owner.timeout * 1000)
            # Never log this signed URL or persist a browser auth profile.
            await self.page.goto(result.url, wait_until='domcontentloaded', timeout=45000)
            await self.wait_for(self.recognized, timeout=45)
            if self.query_id is not None:
                self.keepalive = asyncio.create_task(self.keep_alive())
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception:
            await self.close()
            raise NavigationError('META_WEBAPP_BROWSER_OR_SCREEN_FAILED') from None

    async def keep_alive(self):
        try:
            while True:
                await asyncio.sleep(30)
                await self.renew()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.keepalive_error = True

    async def close(self):
        if self.keepalive:
            self.keepalive.cancel()
            with suppress(asyncio.CancelledError, Exception): await self.keepalive
            self.keepalive = None
        if self.browser:
            with suppress(Exception): await self.browser.close()
        if self.playwright:
            with suppress(Exception): await self.playwright.stop()
        self.browser = self.playwright = self.page = None
        self.request_pending = False

    async def renew(self):
        if self.query_id is None or time.monotonic() - self.renewed_at < 25:
            return
        from telethon import functions
        try:
            await asyncio.wait_for(self.owner.client(functions.messages.ProlongWebViewRequest(
                peer=self.owner.entity, bot=self.owner.entity, query_id=self.query_id)), self.owner.timeout)
        except Exception:
            raise NavigationError('META_WEBAPP_SESSION_EXPIRED') from None
        self.renewed_at = time.monotonic()

    async def text(self):
        self.check()
        if self.page is None or self.page.is_closed():
            raise NavigationError('META_WEBAPP_CLOSED')
        return clean(await self.page.locator('body').inner_text())

    async def find(self, pattern):
        rx = re.compile(pattern, re.I)
        for group in (self.page.get_by_role('button', name=rx), self.page.get_by_text(rx, exact=True)):
            visible = []
            for i in range(await group.count()):
                item = group.nth(i)
                if await item.is_visible(): visible.append(item)
            if len(visible) == 1: return visible[0]
            if len(visible) > 1: raise NavigationError('META_WEBAPP_AMBIGUOUS_CONTROL')
        return None

    async def tap(self, pattern):
        self.check()
        item = await self.find(pattern)
        if item is None: raise NavigationError('META_WEBAPP_CONTROL_MISSING')
        if not await item.is_enabled() or await item.get_attribute('aria-disabled') == 'true':
            raise NavigationError('META_DURATION_UNAVAILABLE')
        await item.click(timeout=self.owner.timeout * 1000)

    async def wait_for(self, predicate, timeout=None):
        deadline = time.monotonic() + (timeout or self.owner.timeout)
        while time.monotonic() < deadline:
            self.check()
            result = await predicate()
            if result: return result
            await asyncio.sleep(.1)
        raise NavigationError('META_WEBAPP_SCREEN_TIMEOUT')

    async def recognized(self):
        text = await self.text()
        return bool(re.search(r'\b(Start|Trade duration|SIGNAL READY|Choose an asset|Preparing your signal)\b',text,re.I))

    async def back_to_start(self):
        if await self.find(r'^Start\s*$'): return
        # Prefer the top in-app back control. Never browser.go_back or Telegram close.
        for _ in range(4):
            if await self.find(r'^Start\s*$'): return
            named = await self.find(r'^(?:Back|Go back|Navigate back|←|‹|<)\s*$')
            if named is not None:
                await named.click()
            else:
                candidates = self.page.locator('button:has(svg.lucide-arrow-left), button:has(svg.lucide-chevron-left), [role="button"]:has(svg.lucide-arrow-left)')
                tops = []
                for i in range(await candidates.count()):
                    loc = candidates.nth(i)
                    box = await loc.bounding_box()
                    if box and box['x'] < 120 and box['y'] < 300: tops.append(loc)
                if len(tops) == 1:
                    await tops[0].click()
                elif await self.page.evaluate('!!window.__metaHost?.backVisible'):
                    await self.page.evaluate("window.Telegram.WebView.receiveEvent('back_button_pressed', {})")
                else: raise NavigationError('META_WEBAPP_BACK_MISSING')
            await asyncio.sleep(.2)
        if not await self.find(r'^Start\s*$'):
            raise NavigationError('META_WEBAPP_BACK_LIMIT')

    async def asset_menu(self):
        self.request_pending = False
        await self.back_to_start()
        await self.tap(r'^Start\s*$')
        async def categories(): return await self.find(r'^Currencies(?:\s+\d+\s+Instruments)?\s*$')
        await self.wait_for(categories)
        await self.tap(r'^Currencies(?:\s+\d+\s+Instruments)?\s*$')
        async def otc(): return await self.find(r'^OTC(?:\s+\d+\s+Instruments)?\s*$')
        await self.wait_for(otc)
        await self.tap(r'^OTC(?:\s+\d+\s+Instruments)?\s*$')
        async def has_pairs(): return bool(await self.asset_rows())
        await self.wait_for(has_pairs)

    async def asset_rows(self):
        # Smallest visible element containing BOTH pair and OTC. A Regular row
        # with an identical pair name must never be used as a substitute.
        return await self.page.evaluate(r"""() => {
          const rx=/^[A-Z]{3}\s*\/\s*[A-Z]{3}\s*(?:\(\s*OTC\s*\)|OTC)$/i;
          return [...document.querySelectorAll('body *')].filter(el=>el.getClientRects().length &&
            rx.test(el.innerText?.trim()||'') && ![...el.children].some(c=>rx.test(c.innerText?.trim()||'')))
            .map(el=>el.innerText.trim());
        }""")

    async def available(self):
        await self.renew()
        await self.asset_menu()
        available = set()
        for _ in range(80):
            self.check()
            available.update(symbol(re.sub(r'\s+', ' ', x)) for x in await self.asset_rows())
            moved = await self.page.evaluate(r"""() => {
              const rx=/^[A-Z]{3}\s*\/\s*[A-Z]{3}\s*(?:\(\s*OTC\s*\)|OTC)$/i;
              const row=[...document.querySelectorAll('body *')].find(el=>
                el.getClientRects().length && rx.test(el.innerText?.trim()||''));
              let parent=row?.parentElement;
              while(parent) {
                const style=getComputedStyle(parent);
                if(/auto|scroll/.test(style.overflowY) && parent.scrollHeight>parent.clientHeight+2) {
                  const before=parent.scrollTop;
                  parent.scrollTop+=Math.max(50,parent.clientHeight*0.8);
                  return parent.scrollTop>before;
                }
                parent=parent.parentElement;
              }
              return false;
            }""")
            if not moved: return available - {None}
            await asyncio.sleep(.15)
        raise NavigationError('META_PAGE_LIMIT')

    async def choose_asset(self, asset):
        pair = asset[:3] + '/' + asset[3:6]
        pattern = r'^' + pair[:3] + r'\s*\/\s*' + pair[4:] + r'\s*(?:\(\s*OTC\s*\)|OTC)\s*$'
        # Search the actual rendered list when possible (works with virtual lists).
        fields = self.page.get_by_placeholder(re.compile('Search',re.I))
        if await fields.count() == 1 and await fields.is_visible():
            await fields.fill(pair)
        async def found(): return await self.find(pattern)
        await self.wait_for(found)
        await self.tap(pattern)
        async def configured(): return live_selection(await self.text())[0] == asset and bool(await self.find(r'^Get signal\s*[→➜>-]*\s*$'))
        await self.wait_for(configured)

    async def set_duration(self, seconds):
        if seconds <= 0: raise NavigationError('META_DURATION_UNAVAILABLE')
        label = f'H{seconds//3600}' if seconds%3600 == 0 else f'M{seconds//60}' if seconds%60 == 0 else f'S{seconds}'
        preset = await self.find('^'+label+'$')
        if preset is not None:
            await self.tap('^'+label+'$')
        else:
            sliders = self.page.locator('input[type="range"]')
            if await sliders.count() != 1: raise NavigationError('META_DURATION_UNAVAILABLE')
            slider = sliders.first
            # Determine units from the observed current value, not a guessed scale.
            current = live_selection(await self.text())[1]
            value = float(await slider.input_value())
            if current is None or value <= 0: raise NavigationError('META_DURATION_UNAVAILABLE')
            scale = current/value
            target = seconds/scale
            lo = float(await slider.get_attribute('min') or '0')
            hi = float(await slider.get_attribute('max') or '100')
            step_text = await slider.get_attribute('step') or '1'
            step = 0 if step_text == 'any' else float(step_text)
            if not lo <= target <= hi or (step and abs((target-lo)/step-round((target-lo)/step)) > 1e-6):
                raise NavigationError('META_DURATION_UNAVAILABLE')
            await slider.fill(str(int(target)) if target.is_integer() else str(target))
            await slider.dispatch_event('change')
        async def selected(): return live_selection(await self.text())[1] == seconds
        try: await self.wait_for(selected, timeout=3)
        except NavigationError: raise NavigationError('META_DURATION_UNAVAILABLE') from None

    async def configure(self, asset, duration, before_confirm):
        await self.renew()
        text = await self.text()
        if (await self.find(r'^New signal\s*[→➜>-]*\s*$') and
                live_selection(text) == (asset,duration)):
            self.expected = (asset,duration)
            return False
        await self.asset_menu()
        await self.choose_asset(asset)
        await self.set_duration(duration)
        if live_selection(await self.text()) != (asset,duration):
            raise NavigationError('META_CONFIRM_MISMATCH')
        self.expected = (asset,duration)
        await self.request(r'^Get signal\s*[→➜>-]*\s*$', before_confirm)
        return True

    async def request(self, pattern, before_click):
        await self.renew()
        if live_selection(await self.text()) != self.expected:
            raise NavigationError('META_SELECTION_CHANGED')
        # Freshness requires observing the analysis phase after THIS request,
        # even when two consecutive cards have identical direction and text.
        await self.page.evaluate(r"""() => {
          window.__metaSignalObserver?.disconnect();
          window.__metaSignalRequest={busy:false,clicked:false};
          const sample=()=>{
            const t=document.body.innerText;
            if(window.__metaSignalRequest.clicked &&
              /Preparing your signal|Analysis visualization|Searching for a suitable entry/i.test(t) &&
              !/SIGNAL\s+READY/i.test(t)) window.__metaSignalRequest.busy=true;
          };
          window.__metaSignalObserver=new MutationObserver(sample);
          window.__metaSignalObserver.observe(document.body,{subtree:true,childList:true,characterData:true});
        }""")
        self.check()
        before_click()
        self.request_pending = True
        self.delivered = False
        await self.page.evaluate('window.__metaSignalRequest.clicked=true')
        try: await self.tap(pattern)
        except BaseException:
            self.request_pending = False
            raise

    async def new_signal(self, before_click):
        await self.request(r'^New signal\s*[→➜>-]*\s*$', before_click)

    async def screen(self):
        text = await self.text()
        asset,duration = live_selection(text)
        raw = text
        if asset and duration:
            raw = f'{asset[:3]}/{asset[3:6]} OTC\n{duration} sec\n' + text
        return SimpleNamespace(raw_text=raw)

    async def wait_signal(self, timeout):
        if not self.request_pending or self.delivered:
            raise NavigationError('META_WEBAPP_NOT_ARMED')
        async def final():
            text = await self.text()
            busy = await self.page.evaluate('!!window.__metaSignalRequest?.busy')
            if not busy or not re.search(r'\bSIGNAL\s+READY\b',text,re.I): return
            if live_selection(text) != self.expected:
                raise NavigationError('META_SIGNAL_SELECTION_MISMATCH')
            directions = set(re.findall(r'^\s*(Higher|Lower)\s*$',text,re.I|re.M))
            directions = {d.lower() for d in directions}
            if len(directions) != 1: return
            if await self.find(r'^New signal\s*[→➜>-]*\s*$') is None: return
            asset,duration = self.expected
            # Feed the existing strict parser its original normalized interface.
            return f'{asset[:3]}/{asset[3:6]} OTC\n{duration} sec\nMeta Signal: {directions.pop()}'
        try:
            text = await self.wait_for(final, timeout)
            self.delivered = True
            return text
        finally:
            self.request_pending = False


class AdaptiveMetaNavigator(MetaNavigator):
    """Lazily chooses Mini App or the untouched legacy message navigator."""
    def __init__(self, client, entity, timeout=15):
        super().__init__(client,entity,timeout)
        self.web = None

    @property
    def web_active(self): return self.web is not None

    async def discover(self):
        from telethon import functions
        try: msg = await super().screen()
        except NavigationError: msg = None
        if msg:
            for button in self.buttons(msg):
                raw = getattr(button, 'button', button)
                kind = type(raw).__name__
                url = getattr(raw,'url',None) or getattr(button,'url',None)
                if url and (kind in ('KeyboardButtonWebView','KeyboardButtonSimpleWebView') or
                            clean(button.text).casefold() == 'beta app'):
                    return url, 'simple' if kind == 'KeyboardButtonSimpleWebView' else 'inline'
        # The persistent Beta App menu may exist without a new chat message.
        try:
            info = await asyncio.wait_for(self.client(functions.users.GetFullUserRequest(self.entity)),self.timeout)
            menu = getattr(getattr(info.full_user,'bot_info',None),'menu_button',None)
            if getattr(menu,'url',None): return menu.url,'menu'
        except (TypeError, AttributeError):
            pass
        return None

    async def ensure(self):
        if self.web is not None: return
        import os
        from diagnostics import SafeIssue
        backend = os.getenv('META_SIGNAL_BACKEND', 'auto').strip().lower()
        if backend not in ('auto','http','browser','telegram'):
            raise SafeIssue('META_API_CONFIG')
        if backend == 'telegram': return
        launch = await self.discover()
        if launch is None and backend == 'http':
            # Explicit HTTP mode must NEVER fall through to legacy trading or
            # silently bypass HTTP dry-run. /start is a menu request, not a signal.
            self.check()
            await self.client.send_message(self.entity, '/start')
            for _ in range(10):
                self.check()
                await asyncio.sleep(.2)
                launch = await self.discover()
                if launch is not None: break
            if launch is None: raise SafeIssue('META_API_NAVIGATION')
        if launch is None:
            # Auto mode preserves the legacy navigator for old message bots.
            return
        if backend in ('auto','http'):
            from meta_api_client import HTTPMiniApp
            web = HTTPMiniApp(self)
        else:
            web = MiniApp(self)
        await web.launch(launch)
        self.web = web

    async def configure(self,asset,duration,before_confirm,cancel=None):
        self.cancel = cancel
        await self.ensure()
        if self.web: return await self.web.configure(asset,duration,before_confirm)
        # Legacy /start can expose Beta App for the first time. Check again
        # only after an unknown-menu result, never re-send a requested signal.
        try: return await super().configure(asset,duration,before_confirm,cancel)
        except NavigationError as exc:
            if str(exc) not in ('META_MENU_UNKNOWN','META_URL_BUTTON'): raise
            await self.ensure()
            if not self.web: raise
            return await self.web.configure(asset,duration,before_confirm)

    async def available(self,cancel=None):
        self.cancel = cancel
        await self.ensure()
        if self.web: return await self.web.available()
        try: return await super().available(cancel)
        except NavigationError as exc:
            if str(exc) not in ('META_MENU_UNKNOWN','META_URL_BUTTON'): raise
            await self.ensure()
            if not self.web: raise
            return await self.web.available()

    async def screen(self):
        if self.web: return await self.web.screen()
        return await super().screen()

    async def new_signal(self,before_click):
        if self.web: return await self.web.new_signal(before_click)
        return await super().new_signal(before_click)

    async def wait_signal(self,timeout):
        return await self.web.wait_signal(timeout)

    async def close(self):
        if self.web:
            await self.web.close()
            self.web = None

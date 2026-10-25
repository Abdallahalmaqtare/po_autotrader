"""Best-effort public health requests, independent of trading and DB readiness.
Not an uptime guarantee: a suspended process cannot issue HTTP requests itself.
No trading actions or model calls occur here. Metrics expose no credentials.
"""
import asyncio
import math
import time
from urllib.parse import urlsplit
import aiohttp
from runtime_support import timestamp_print as print


def public_health_url(env):
    value=(env.get('RENDER_EXTERNAL_URL') or env.get('APP_PUBLIC_URL') or '').strip()
    if not value and env.get('RENDER_EXTERNAL_HOSTNAME'):
        value='https://'+env['RENDER_EXTERNAL_HOSTNAME'].strip()
    if not value:return None
    p=urlsplit(value)
    if p.scheme not in ('https','http') or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise ValueError('Public service URL must be http(s) without credentials/query/fragment')
    return value.rstrip('/')+'/health'


def heartbeat_interval(env):
    try:value=float(env.get('KEEP_ALIVE_SECONDS','300') or 300)
    except (ValueError,TypeError):return 300.0
    return min(600.0,max(60.0,value)) if math.isfinite(value) else 300.0


async def shutdown_wait(event,seconds):
    try:await asyncio.wait_for(event.wait(),seconds)
    except asyncio.TimeoutError:pass


async def keep_alive_loop(url,shutdown,metrics,interval=300,session_factory=None,waiter=shutdown_wait):
    """Immediate first request; regular public health requests while alive.
    Failures retry after 30..120s, not a tight loop. Cancel/shutdown remains prompt.
    Metrics can be inspected through /health or /status; URLs aren't returned.
    """
    factory=session_factory or aiohttp.ClientSession
    metrics.update(enabled=True,interval=interval,attempts=0,failures=0,last_attempt=None,
                   last_success=None,last_status=None,last_error=None)
    async with factory(timeout=aiohttp.ClientTimeout(total=15,connect=5)) as session:
        while not shutdown.is_set():
            metrics['attempts']+=1;metrics['last_attempt']=time.time();metrics['last_status']=None
            delay=interval
            try:
                async with session.get(url,allow_redirects=False) as response:
                    status=response.status;metrics['last_status']=status
                    if status!=200:raise RuntimeError('non_200')
                    # Our health endpoint has a bounded small JSON response. Don't
                    # mistake a proxy/loading page for the app accepting a request.
                    payload=await response.json(content_type=None)
                    if not isinstance(payload,dict) or payload.get('alive') is not True:
                        raise RuntimeError('not_application_health')
                metrics.update(last_success=time.time(),last_error=None,failures=0)
                print(f"[HOST] public health OK status=200 attempts={metrics['attempts']} next={interval:g}s",flush=True)
            except asyncio.CancelledError:raise
            except Exception as exc:
                metrics['failures']+=1;metrics['last_error']=type(exc).__name__
                delay=min(120,30*metrics['failures'])
                print(f"[HOST] public health failed status={metrics.get('last_status')} error={type(exc).__name__} retry={delay}s",flush=True)
            await waiter(shutdown,delay)

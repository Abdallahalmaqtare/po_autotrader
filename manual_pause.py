"""Manual pause is independent of Stop and scheduled rest. No order is cancelled.
Remaining run/rest time is frozen only at a settled pause boundary. A manual
resume never bypasses an unresolved order, money limit, or account mismatch.
"""
import asyncio
from copy import deepcopy
from contextlib import suppress


def pause_defaults():
    return dict(requested=False,paused=False,remaining_run=None,remaining_rest=None,cycle_key=None)


def paused(doc):return bool(doc.get('runtime',{}).get('manual_pause',{}).get('requested'))


class ManualPause:
    def request_pause(self):
        # Synchronous pre-send barrier, before answering the Telegram callback.
        already=self.pause_requested
        self.pause_requested=True;self.stop_event.set()
        if already:return
        if self.task and not self.in_flight and self.stage!='order_reserve':self.task.cancel()

    async def complete_manual_pause(self):
        state={**pause_defaults(),**deepcopy(self.repo.doc['runtime'].get('manual_pause',{}))}
        if not self.pause_requested and not state['requested']:return
        was_paused=bool(state['paused'])
        state['requested']=True
        if self.pending:
            state['paused']=False;self.stage='pause_waiting_result'
        else:
            if not state['paused']:
                cycle=self.repo.doc['runtime']['cycle'];now=self.clock()
                state['remaining_run']=max(0,cycle.get('run_until',0)-now) if cycle.get('phase') in ('RUN','DRAIN') else None
                state['remaining_rest']=max(0,cycle.get('rest_until',0)-now) if cycle.get('phase')=='REST' else None
                state['cycle_key']=deepcopy(cycle.get('key'))
            state['paused']=True;self.stage='paused'
            disarm=getattr(self.listener,'disarm',None)
            if callable(disarm):disarm();self.listener.prepared=None
        await self.repo.save_manual_pause(state)
        refresh=getattr(self,'refresh_pause_panel',None)
        if state['paused'] and not was_paused and callable(refresh):
            with suppress(Exception):await asyncio.wait_for(refresh(),5)

    async def pause(self):
        self.request_pause()
        async with self.operations:
            previous={**pause_defaults(),**deepcopy(self.repo.doc['runtime'].get('manual_pause',{}))}
            previous['requested']=True
            await self.repo.save_manual_pause(previous)
            active=self.task is not None and not self.task.done()
            if not active:await self.complete_manual_pause()
            elif not self.in_flight and self.stage!='order_reserve':
                with suppress(asyncio.CancelledError):await self.task
            if self.pending or self.in_flight:
                return '⏳ طلب الإيقاف المؤقت محفوظ. جاري انتظار إغلاق الصفقة وتسوية نتيجتها؛ لن يُرسل دخول جديد.'
            return '⏸ تم الإيقاف مؤقتاً وحفظ المستوى والتعزيز والإحصائيات. يمكنك تعديل الزوج ثم الضغط على استمرار.'

    async def resume(self):
        async with self.operations:
            if self.pending:return '⏳ جاري انتظار الصفقة المعلّقة؛ الاستمرار ممنوع حتى تُحسم نتيجتها.'
            if self.task and not self.task.done():return 'جارٍ إكمال الإيقاف المؤقت؛ انتظر لحظة.'
            state={**pause_defaults(),**self.repo.doc['runtime'].get('manual_pause',{})}
            if self.pause_requested:state['requested']=True
            if not state['requested']:
                # Start validates credentials/limits outside this lock below.
                pass
            elif await self.enforce_limits(notify=False):
                return 'تحقق حد الربح أو الخسارة؛ لا يمكن الاستمرار قبل مراجعة الحدود.'
            else:
                await self.repo.resume_manual_pause(self.clock())
                self.pause_requested=False
                disarm=getattr(self.listener,'disarm',None)
                if callable(disarm):disarm();self.listener.prepared=None
        return await self.start()

    def pause_text(self):
        state=self.repo.doc['runtime'].get('manual_pause',{})
        if (self.pause_requested or state.get('requested')) and self.pending:return '⏳ إيقاف مؤقت مطلوب؛ جاري انتظار نتيجة الصفقة.'
        if state.get('paused'):return '⏸ متوقف مؤقتاً — الحالة محفوظة.'
        return '▶️ قيد التشغيل' if self.running else '⏹ متوقف'

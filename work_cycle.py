"""Persistent run/rest clock. Rest waits for a Martingale ladder, not a
whole recovery campaign. Monetary limits and manual Stop always take priority.
Clock functions are injectable for offline deterministic tests.
"""
import asyncio
from runtime_support import timestamp_print as print
import math
import time
from copy import deepcopy


def cycle_defaults():
    return dict(mode='OFF',run_minutes=10.0,rest_minutes=10.0,finish='LADDER')


def cycle_state():
    return dict(phase='IDLE',run_until=0.0,rest_until=0.0,key=[])


def validate_cycle(cfg):
    if cfg.get('mode') not in ('OFF','STOP','REPEAT'):
        raise ValueError('وضع وقت التشغيل غير صالح')
    if cfg.get('finish') not in ('ORDER','LADDER'):raise ValueError('قاعدة إنهاء التدبيل غير صالحة')
    for key in ('run_minutes','rest_minutes'):
        value=cfg.get(key)
        if isinstance(value,bool):raise ValueError('المدة يجب أن تكون موجبة')
        try:n=float(value)
        except (ValueError,TypeError):raise ValueError('المدة يجب أن تكون رقماً موجباً') from None
        if not math.isfinite(n) or not 0.01<=n<=10080:
            raise ValueError('المدة بين 0.01 و10080 دقيقة')


def cycle_key(cfg):
    return [cfg['mode'],float(cfg['run_minutes']),float(cfg['rest_minutes']),cfg['finish']]


def transition(cfg,state,step,pending=False,force_rest=False,now=None):
    now=time.time() if now is None else now
    state=deepcopy(state or cycle_state())
    if cfg['mode']=='OFF':return 'ALLOW',cycle_state()
    if state.get('key')!=cycle_key(cfg) or state.get('phase')=='IDLE':
        state=dict(phase='RUN',run_until=now+cfg['run_minutes']*60,rest_until=0.0,key=cycle_key(cfg))
    phase=state['phase']
    if phase=='REST':
        if now<state['rest_until']:return 'REST',state
        state.update(phase='RUN',run_until=now+cfg['run_minutes']*60,rest_until=0.0)
        return 'ALLOW',state
    if phase=='DONE':return 'STOP',state
    if pending:return 'DRAIN',state
    if phase=='DRAIN' or now>=state['run_until']:
        if step>0 and cfg['finish']=='LADDER' and not force_rest:
            state['phase']='DRAIN'
            return 'DRAIN',state
        if cfg['mode']=='STOP':
            state['phase']='DONE';return 'STOP',state
        state.update(phase='REST',rest_until=now+cfg['rest_minutes']*60)
        return 'REST',state
    return 'ALLOW',state


class CycleWorkflow:
    clock=staticmethod(time.time)

    async def cycle_gate(self,step):
        cfg=deepcopy(self.repo.doc['schedule'])
        old=deepcopy(self.repo.doc['runtime'].get('cycle',cycle_state()))
        action,new=transition(cfg,old,step,bool(self.pending),self.cycle_force_rest,self.clock())
        if new!=old:
            if not await self.repo.save_cycle(new,cfg):return False
        self.cycle_force_rest=False
        if action in ('ALLOW','DRAIN'):
            if action=='DRAIN':self.cycle_note='انتهى الوقت؛ استكمال سلسلة التدبيل الحالية.'
            else:self.cycle_note=''
            return True
        if action=='STOP':
            self.stage='time_limit_reached';self.stop_event.set()
            self.cycle_note='انتهى وقت التشغيل المحدد.'
            print('[CYCLE] time limit reached; entries disabled',flush=True)
            if self.notify:
                try:await asyncio.wait_for(self.notify('⏱ انتهى وقت التشغيل؛ التداول متوقف.'),5)
                except Exception:pass
            return False
        self.stage='scheduled_rest'
        self.cycle_note='استراحة مجدولة؛ الاستئناف تلقائي بعد انتهائها.'
        if old.get('phase')!='REST':
            print('[CYCLE] rest started',flush=True)
            disarm=getattr(self.listener,'disarm',None)
            if callable(disarm):
                disarm()
                self.listener.prepared=None
        while not self.stop_event.is_set():
            if await self.enforce_limits():return False
            if self.repo.doc['schedule']!=cfg:return False
            remaining=new['rest_until']-self.clock()
            if remaining<=0:return False  # next loop persists a new RUN phase
            await self._pause(min(remaining,1))
        return False

    async def wait_cycle_boundary(self,step):
        while True:
            cfg=self.repo.doc['schedule']
            old=self.repo.doc['runtime'].get('cycle',cycle_state())
            action,new=transition(cfg,old,step,bool(self.pending),False,self.clock())
            if action in ('STOP','REST'):return
            if cfg['mode']=='OFF' or action=='DRAIN':
                await asyncio.Event().wait()
            remaining=new['run_until']-self.clock()
            await asyncio.sleep(min(max(remaining,0.01),1))


def cycle_text(doc,now=None):
    cfg=doc['schedule'];state=doc['runtime'].get('cycle',cycle_state())
    labels={'OFF':'◻️ بدون حد وقت','STOP':'⏹ توقف عند انتهاء الوقت','REPEAT':'🔁 تشغيل وراحة متكررة'}
    phases={'IDLE':'متوقف','RUN':'تشغيل','DRAIN':'استكمال سلسلة التدبيل','REST':'استراحة','DONE':'اكتمل وقت التشغيل'}
    now=time.time() if now is None else now
    text=(f"⏱ وقت التشغيل والراحة\n{labels[cfg['mode']]}\n"
          f"التشغيل: {cfg['run_minutes']:g} دقيقة | الراحة: {cfg['rest_minutes']:g} دقيقة\n"
          f"المرحلة: {phases.get(state.get('phase'),'—')}")
    end=state.get('rest_until') if state.get('phase')=='REST' else state.get('run_until')
    if end and state.get('phase') in ('REST','RUN'):text+=f' | متبقٍ {max(0,int(end-now))}ث'
    text+='\nعند نهاية الوقت: '+('إكمال سلسلة التدبيل' if cfg['finish']=='LADDER' else 'حسم الصفقة المفتوحة فقط')
    return text+'\nحدود المال وStop تمنع الدخول الجديد فوراً.'

"""Task-local isolation for legacy config reads/writes, including child tasks."""
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass,field
from types import ModuleType
_current=ContextVar('aboud_scope',default=None)
SCOPED={'ADMIN_TELEGRAM_ID','TELEGRAM_API_ID','TELEGRAM_API_HASH','TELEGRAM_STRING_SESSION','TELEGRAM_EXPECTED_USER_ID','SIGNAL_BOT_USERNAME','ACCOUNT_TYPE','IS_DEMO','ASSET','DURATION_SECONDS','TRADE_AMOUNT','BALANCE_SETTLE_SECONDS','LOSS_RECHECK_SECONDS','RESULT_TIMEOUT_EXTRA','ENTRY_DELAY_SECONDS','MARTINGALE_STEPS','LOOP_DELAY_SECONDS','NEW_SIGNAL_BUTTON_TEXT'}
@dataclass
class Scope:
    uid:int
    values:dict=field(default_factory=dict)
@contextmanager
def bind(scope):
    token=_current.set(scope)
    try:yield scope
    finally:_current.reset(token)
def current():return _current.get()
class ScopedConfig(ModuleType):
    def __getattribute__(self,name):
        scope=_current.get()
        if scope and name in SCOPED:
            if name=='ADMIN_TELEGRAM_ID':return scope.uid
            if name in scope.values:return scope.values[name]
            if name in ('TELEGRAM_API_HASH','TELEGRAM_STRING_SESSION'):return ''
            if name in ('TELEGRAM_API_ID','TELEGRAM_EXPECTED_USER_ID'):return 0
        return super().__getattribute__(name)
    def __setattr__(self,name,value):
        scope=_current.get()
        if scope and name in SCOPED:scope.values[name]=value
        else:super().__setattr__(name,value)
def install(module):module.__class__=ScopedConfig

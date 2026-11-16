"""Serialized admin operations. Switching never replays or transfers an open order."""
from contextlib import suppress
from profile_registry import selected_source,source_key,active_profile_id
from telegram_bootstrap import signal_username


class ProfileWorkflow:
    async def _prepare_registry_change(self):
        await self._stop()
        if self.pending:
            raise ValueError('يوجد أمر غير محسوم؛ احسمه أو أرشفه من حسابه الحالي قبل التبديل')

    async def _drop_signal_connection(self):
        if self.listener:
            disarm=getattr(self.listener,'disarm',None)
            if callable(disarm):disarm()
        if self.tg:
            with suppress(Exception):await self.tg.disconnect()
        self.tg=None;self.listener=None;self._listener_source_key=None
        self.selection_changed.set()

    async def source_action(self,action,key=None,value=None):
        async with self.operations:
            await self._prepare_registry_change()
            if action=='add':
                result=await self.repo.add_source(value)
            elif action=='select':
                await self.repo.select_source(key);result=key
            elif action=='edit':
                await self.repo.edit_source(key,value);result=key
            elif action=='delete':
                await self.repo.delete_source(key);result=None
            else:raise ValueError('عملية مصدر غير صالحة')
            await self._drop_signal_connection()
            self.stage='stopped'
            return result

    async def account_action(self,action,key=None,value=None):
        async with self.operations:
            await self._prepare_registry_change()
            if action=='add':
                result=await self.repo.add_profile(value['name'],value['mode'],value['credentials'])
            elif action=='select':
                await self.repo.select_profile(key);result=key
            elif action=='rename':
                await self.repo.rename_profile(key,value);result=key
            elif action=='edit':
                await self.repo.edit_profile(key,value);result=key
            elif action=='delete':
                await self.repo.delete_profile(key);result=None
            else:raise ValueError('عملية حساب غير صالحة')
            if self.po:
                await self.po.close()
            self.po=None
            await self._drop_signal_connection()
            self.payout_note='';self.payout_refresh_at=0.0;self.cycle_force_rest=False
            self._last_limit_notice=None;self.stage='stopped'
            return result

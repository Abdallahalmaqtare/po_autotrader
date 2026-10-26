"""Offline Bot transport supporting real aiogram InputMediaPhoto objects."""
from types import SimpleNamespace
from unittest.mock import AsyncMock


class FakeBot:
    def __init__(self):
        self.sequence = 100
        self.messages = {}
        self.send_photo = AsyncMock(side_effect=self._photo)
        self.edit_message_media = AsyncMock(side_effect=self._media)
        self.edit_message_caption = AsyncMock(side_effect=self._caption)
        self.edit_message_reply_markup = AsyncMock(return_value=True)
        self.delete_message = AsyncMock(return_value=True)
        self.send_message = AsyncMock(side_effect=self._text)

    def create(self, caption='', photo=False, mid=None):
        if mid is None:
            self.sequence += 1
            mid = self.sequence
        msg = SimpleNamespace(message_id=mid, caption=caption,
                              photo=[SimpleNamespace(file_id=f'cached-photo-{mid}')] if photo else [],
                              chat=SimpleNamespace(id=1, type='private'))
        self.messages[mid] = msg
        return msg

    async def _photo(self, chat_id, photo, **kwargs):
        return self.create(caption=kwargs.get('caption',''),photo=True)

    async def _media(self, **kwargs):
        return self.create(caption=kwargs['media'].caption,photo=True,mid=kwargs['message_id'])

    async def _caption(self, **kwargs):
        return self.create(caption=kwargs['caption'],photo=True,mid=kwargs['message_id'])

    async def _text(self, chat_id, text, **kwargs):
        return self.create(caption=text)


def synchronized_fixture(listener):
    """Adapt v18 unit queues to v19's request interface; real navigation tested separately."""
    async def next_direction(asset,duration,cancel=None):
        listener.drain_pending()
        if not await listener.press_new_signal(): return None
        return await listener.wait_for_signal(15)
    listener.next_direction=AsyncMock(side_effect=next_direction)
    listener.verifies=lambda *args,**kwargs: True
    listener.synchronize=AsyncMock()
    listener.available_assets=AsyncMock(return_value=set())
    return listener

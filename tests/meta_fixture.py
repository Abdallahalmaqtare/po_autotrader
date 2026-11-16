"""A deterministic fake of the six screenshot screens, using real listener events."""
from types import SimpleNamespace
from datetime import datetime, timezone
from forex_assets import FOREX_OTC
from meta_navigation import duration_label


class FakeMetaMessage:
    def __init__(self,client,text,rows):
        client.sequence+=1
        self.id=client.sequence
        self.client=client
        self.raw_text=text
        self.buttons=[[SimpleNamespace(text=b,url=None) for b in row] for row in rows]
        self.date=datetime.now(timezone.utc)
        self.edit_date=None
        self.out=False

    async def click(self,text):
        if self.id!=self.client.current.id:
            raise AssertionError('clicked a historical keyboard')
        if text not in [b.text for row in self.buttons for b in row]:
            raise AssertionError('invented button')
        await self.client.click(text)


class FakeMetaClient:
    def __init__(self):
        self.sequence=0; self.history=[]; self.current=None
        self.state='menu'; self.page=0; self.asset='EURUSD_otc'; self.duration=5
        self.handlers=[]; self.clicks=[]; self.sent=[]; self.omit_duration=None
        self.pairs=FOREX_OTC[:]
        self.render()

    def on(self,builder):
        def register(fn):
            if fn not in self.handlers: self.handlers.append(fn)
            return fn
        return register

    async def get_entity(self,name):
        return SimpleNamespace(bot=True,id=12345)

    async def iter_messages(self,entity,limit=8):
        if self.current: yield self.current
        for msg in self.history[-(limit-1):][::-1]: yield msg

    async def send_message(self,entity,text):
        self.sent.append(text)
        if text=='/start': self.state='menu'; self.render()

    def name(self):
        return next(name for name,sym in FOREX_OTC if sym==self.asset)

    def render(self):
        state=self.state
        if state=='menu':
            text='Welcome to Meta Trading'; rows=[['Trade'],['Support','Learn more'],['My profile','Change language']]
        elif state=='trading':
            text='Trading section\nChoose a market:'; rows=[['Stock','OTC'],['Back']]
        elif state=='assets':
            text='Select an asset\nMarket: OTC\nChoose an asset:'
            selected=self.pairs[self.page*6:self.page*6+6]
            rows=[[name for name,_ in selected[i:i+2]] for i in range(0,len(selected),2)]
            pages=(len(self.pairs)+5)//6
            rows += [['<' if self.page else '.',f'{self.page+1}/{pages}','>' if self.page<pages-1 else '.'],['Back']]
        elif state=='duration':
            text='Select duration\nAsset: '+self.name()+'\nChoose trade duration:'
            choices=[duration_label(v) for v in (5,15,30,60,180,300,600,900,1800) if v!=self.omit_duration]
            rows=[choices[i:i+3] for i in range(0,len(choices),3)]+[['Back']]
        elif state=='confirm':
            unit=f'{self.duration} sec' if self.duration<60 else f'{self.duration//60} min'
            text=f'Asset: {self.name()}\nTrade duration: {unit}\nRun analysis?'; rows=[['Confirm'],['Back']]
        else:
            unit=f'{self.duration} sec' if self.duration<60 else f'{self.duration//60} min'
            text=f'Trading signal\n{self.name()}\n{unit}\nCurrent Time (UTC+3):\n12:17:38\nMeta Signal: Higher'
            rows=[['New signal'],['Back']]
        if self.current: self.history.append(self.current)
        self.current=FakeMetaMessage(self,text,rows)

    async def emit(self,text=None,asset=None,duration=None,age_seconds=0):
        msg=self.current
        raw=text or msg.raw_text
        event=SimpleNamespace(id=msg.id,message=msg,raw_text=raw)
        for handler in self.handlers: await handler(event)

    async def click(self,text):
        self.clicks.append(text)
        if text=='Trade': self.state='trading'
        elif text=='OTC': self.state='assets'; self.page=0
        elif text=='>': self.page+=1
        elif text=='<': self.page-=1
        elif text=='Back':
            self.state={'signal':'confirm','confirm':'duration','duration':'assets',
                        'assets':'trading','trading':'menu'}[self.state]
        elif text=='Confirm':
            self.state='signal'; self.render(); await self.emit(); return
        elif text=='New signal':
            self.state='signal'; self.render(); await self.emit(); return
        elif self.state=='assets':
            self.asset=next(sym for name,sym in self.pairs if name==text)
            self.state='duration'
        elif self.state=='duration':
            self.duration=int(text[1:])*(60 if text.startswith('M') else 1)
            self.state='confirm'
        else: raise AssertionError('unexpected click')
        self.render()

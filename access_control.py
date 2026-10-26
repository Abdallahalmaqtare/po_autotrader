"""ID-bound one-time approval, ten-minute expiry and five-guess limit."""
import hashlib,hmac,secrets,time
class AccessStore:
    def __init__(self,collection,owner,secret,clock=time.time):
        self.collection=collection;self.owner=int(owner);self.secret=secret.encode();self.clock=clock
    async def get(self,uid):
        if int(uid)==self.owner:return {'_id':self.owner,'status':'active','role':'owner'}
        return await self.collection.find_one({'_id':int(uid)})
    async def request(self,uid,name=''):
        uid=int(uid);now=self.clock()
        await self.collection.update_one({'_id':uid},{'$setOnInsert':dict(status='pending',created_at=now,last_notice=0),'$set':{'name':str(name)[:80]}},upsert=True)
        doc=await self.get(uid)
        if doc.get('status') in ('active','blocked','deleted'):return False
        r=await self.collection.update_one({'_id':uid,'last_notice':{'$lte':now-60}},{'$set':{'last_notice':now}})
        return r.matched_count==1
    def digest(self,uid,nonce,code):return hmac.new(self.secret,f'{uid}:{nonce}:{code}'.encode(),hashlib.sha256).hexdigest()
    async def issue(self,actor,uid):
        if int(actor)!=self.owner:raise PermissionError('owner_only')
        uid=int(uid)
        if uid<=0 or uid==self.owner:raise ValueError('معرّف غير صالح')
        code=f'{secrets.randbelow(100000000):08d}';nonce=secrets.token_hex(16);now=self.clock()
        await self.collection.update_one({'_id':uid},{'$set':dict(status='pending',nonce=nonce,code_hash=self.digest(uid,nonce,code),code_expires=now+600,attempts=0),'$setOnInsert':{'created_at':now}},upsert=True)
        return code
    async def verify(self,uid,code):
        uid=int(uid);d=await self.get(uid);now=self.clock()
        if not d or d.get('status')!='pending' or d.get('code_expires',0)<now or d.get('attempts',0)>=5:return False
        if not hmac.compare_digest(self.digest(uid,d.get('nonce',''),code.strip()),d.get('code_hash','')):
            await self.collection.update_one({'_id':uid,'status':'pending'},{'$inc':{'attempts':1}});return False
        r=await self.collection.update_one({'_id':uid,'status':'pending','code_hash':d['code_hash'],'attempts':{'$lt':5},'code_expires':{'$gte':now}},
            {'$set':{'status':'active','activated_at':now},'$unset':{'nonce':'','code_hash':'','code_expires':''}})
        return r.matched_count==1
    async def status(self,actor,uid,value):
        if int(actor)!=self.owner:raise PermissionError('owner_only')
        uid=int(uid)
        if uid<=0 or uid==self.owner or value not in ('blocked','deleted'):raise ValueError('غير مسموح')
        await self.collection.update_one({'_id':uid},{'$set':{'status':value},'$unset':{'nonce':'','code_hash':'','code_expires':''}},upsert=True)
    async def list(self):return await self.collection.find({}).sort('_id',1).limit(100).to_list(length=100)

class ResourceClaims:
    """Short renewable leases: never let two users trade the same UID/mode."""
    def __init__(self,collection,clock=time.time):self.collection=collection;self.clock=clock
    async def claim(self,key,holder,ttl=40):
        now=self.clock()
        await self.collection.update_one({'_id':key},{'$setOnInsert':{'holder':None,'expires':0}},upsert=True)
        r=await self.collection.update_one({'_id':key,'$or':[{'holder':holder},{'expires':{'$lte':now}}]}, {'$set':{'holder':holder,'expires':now+max(40,float(ttl))}})
        return r.matched_count==1
    async def extend(self,key,holder,until):
        await self.collection.update_one({'_id':key,'holder':holder},{'$set':{'expires':max(self.clock()+40,float(until))}})
    async def release(self,key,holder):await self.collection.update_one({'_id':key,'holder':holder},{'$set':{'expires':0,'holder':None}})

"""Bounded Socket.IO JSON decoding. No raw frames or credentials are logged."""
import json
import re
import zlib
from payout_guard import symbol

MAX_BYTES=4*1024*1024


def payload_json(frame):
    if isinstance(frame,bytes):
        if len(frame)>MAX_BYTES: raise ValueError('frame_too_large')
        data=frame
        if data[:1]==b'\x04': data=data[1:]
        if data.startswith(b'\x1f\x8b') or data[:2] in (b'x\x9c',b'x\xda',b'x\x01'):
            decoder=zlib.decompressobj(31 if data.startswith(b'\x1f\x8b') else 15)
            data=decoder.decompress(data,MAX_BYTES+1)
            if len(data)>MAX_BYTES or decoder.unconsumed_tail: raise ValueError('expanded_frame_too_large')
        text=data.decode('utf-8-sig').strip()
    else:
        text=str(frame).lstrip('\ufeff').strip()
        if len(text)>MAX_BYTES: raise ValueError('frame_too_large')
    if text[:1]=='4' and text[1:2] in ('[','{','"'): text=text[1:]
    result=json.loads(text)
    if isinstance(result,str) and result[:1] in ('[','{'): result=json.loads(result)
    return result


def event_payload(value):
    if isinstance(value,list) and value and isinstance(value[0],str): return [value]
    if isinstance(value,dict):
        if 'deals' in value and isinstance(value['deals'],list):return [['closedDeals',value]]
        if 'requestId' in value and 'asset' in value and 'amount' in value:return [['orderData',value]]
        name=value.get('event')
        if name in ('updateAssets','assets','updatePayout','updatePayouts','successopenOrder','successcloseOrder','closedDeals','successauth','failauth','errorauth','authError','updateBalance','profile','updateProfile'):
            return [[name,value.get('data',value.get('assets'))]]
    if isinstance(value,list) and any(isinstance(row,list) and len(row)>5 and symbol(row[1]) for row in value):
        return [['updateAssets',value]]
    return []


class EventDecoder:
    def __init__(self):
        self.pending=None
        self.stats={'decoded':0,'rejected':0,'attachments':0}

    def feed(self,frame):
        try:
            if isinstance(frame,str):
                # Engine.IO message (4) + Socket.IO binary event (5), with optional
                # namespace/ack before JSON. Never scan arbitrary text for numbers.
                if frame.startswith('45') and '-' in frame:
                    count,body=frame[2:].split('-',1)
                    n=int(count)
                    if not 1<=n<=10: raise ValueError('attachment_count')
                    pos=body.find('[')
                    if pos<0 or not re.fullmatch(r'(?:/[\w/.-]+,)?\d*',body[:pos]): raise ValueError('event_prefix')
                    self.pending=(json.loads(body[pos:]),n,[])
                    return []
                if frame.startswith('42'):
                    body=frame[2:];pos=body.find('[')
                    if pos<0 or not re.fullmatch(r'(?:/[\w/.-]+,)?\d*',body[:pos]): raise ValueError('event_prefix')
                    result=[json.loads(body[pos:])]
                    self.stats['decoded']+=len(result)
                    return result
                if frame in ('2','3','40','41') or frame.startswith('0'): return []
            value=payload_json(frame)
            if self.pending:
                event,expected,attachments=self.pending
                attachments.append(value);self.stats['attachments']+=1
                if len(attachments)<expected:return []
                self.pending=None
                def restore(obj):
                    if isinstance(obj,dict) and obj.get('_placeholder') is True:
                        index=obj.get('num')
                        if isinstance(index,bool) or not isinstance(index,int) or not 0<=index<len(attachments): raise ValueError('placeholder')
                        return attachments[index]
                    if isinstance(obj,list):return [restore(v) for v in obj]
                    if isinstance(obj,dict):return {k:restore(v) for k,v in obj.items()}
                    return obj
                result=[restore(event)]
            else: result=event_payload(value)
            self.stats['decoded']+=len(result)
            return result
        except (ValueError,TypeError,IndexError,KeyError,UnicodeDecodeError,zlib.error,RecursionError):
            self.pending=None;self.stats['rejected']+=1
            return []

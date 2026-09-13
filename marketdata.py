"""Independent public-data workers and optional Kraken v2 WebSocket streams.

No strategy, news, or order execution runs in these readers. Public workers never
receive credentials. Private executions wake reconciliation; they do not bypass it.
"""
import asyncio
import copy
import gzip
import json
import math
import queue
import threading
import time
import zlib
from collections import deque
from datetime import datetime,timezone
from decimal import Decimal
from pathlib import Path
from core import Kraken,SafetyError,D
from persistence import encode


class BookError(SafetyError):pass


def stamp(value):
    return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()


class OrderBook:
    def __init__(self,symbol,depth=25):
        if depth not in (10,25,100,500,1000):raise ValueError('Ugyldig Kraken-dybde.')
        self.symbol=symbol;self.depth=depth;self.sides={'bids':{},'asks':{}};self.valid=False

    def checksum(self):
        content=''
        for side,reverse in (('asks',False),('bids',True)):
            for key in sorted(self.sides[side],reverse=reverse)[:10]:
                price,quantity=self.sides[side][key]
                for value in (price,quantity):content+=format(D(value),'f').replace('.','').lstrip('0')
        return zlib.crc32(content.encode('ascii'))&0xffffffff

    def apply(self,message):
        if message.get('channel')!='book':raise BookError('Feil kanal for ordrebok.')
        snapshot=message.get('type')=='snapshot'
        if not snapshot and not self.valid:raise BookError('Oppdatering uten gyldig øyeblikksbilde.')
        try:
            for data in message['data']:
                if data['symbol']!=self.symbol:continue
                if snapshot:self.sides={'bids':{},'asks':{}}
                for side in ('bids','asks'):
                    for row in data.get(side,[]):
                        price,quantity=D(row['price']),D(row['qty'])
                        if not price.is_finite() or not quantity.is_finite() or price<=0 or quantity<0:
                            raise BookError('Ugyldig pris eller mengde i strømmen.')
                        if quantity==0:self.sides[side].pop(price,None)
                        else:self.sides[side][price]=(str(price),str(quantity))
                    keys=sorted(self.sides[side],reverse=side=='bids')
                    self.sides[side]={p:self.sides[side][p] for p in keys[:self.depth]}
                if not self.sides['bids'] or not self.sides['asks'] or max(self.sides['bids'])>=min(self.sides['asks']):
                    raise BookError('Tom eller krysset ordrebok.')
                if self.checksum()!=int(data['checksum']):raise BookError('CRC32 stemmer ikke. Nytt øyeblikksbilde kreves.')
                self.valid=True
            if not self.valid:raise BookError('Mangler det valgte markedet i øyeblikksbildet.')
            return self.snapshot()
        except Exception:
            self.valid=False
            raise

    def snapshot(self):
        if not self.valid:raise BookError('Ordreboken er ugyldig.')
        return {side:[list(self.sides[side][p]) for p in sorted(self.sides[side],reverse=side=='bids')]
                for side in ('bids','asks')}


class Recorder:
    """Bounded queue and daily gzip segments; dropping events is explicitly recorded."""
    def __init__(self,folder,max_bytes=50*1024*1024):
        self.folder=Path(folder);self.folder.mkdir(parents=True,exist_ok=True)
        self.max_bytes=max_bytes;self.q=queue.Queue(5000);self.stop=threading.Event()
        self.dropped=0;self.error='';self.sequence=0;self.thread=None;self.lock=threading.Lock()

    def start(self):
        self.thread=threading.Thread(target=self._run,name='NordBot-capture',daemon=False);self.thread.start()

    def put(self,kind,data,observed=None):
        with self.lock:
            self.sequence+=1
            received=time.time()
            event={'schema':2,'sequence':self.sequence,'kind':kind,'received':received,
                   'observed':received if observed is None else observed,'data':data}
            try:self.q.put_nowait(event)
            except queue.Full:self.dropped+=1

    def _run(self):
        file=None;raw_path=None;day=None;part=0;dropped=0
        try:
            while not self.stop.is_set() or not self.q.empty():
                try:event=self.q.get(timeout=.2)
                except queue.Empty:continue
                current=datetime.fromtimestamp(event['received'],timezone.utc).strftime('%Y%m%d')
                if day!=current or file is None or raw_path.stat().st_size>self.max_bytes:
                    if file:file.close()
                    part+=1;day=current
                    raw_path=self.folder/f'market-{day}-{time.time_ns()}-{part}.jsonl.gz'
                    file=gzip.open(raw_path,'at',encoding='utf-8')
                if dropped!=self.dropped:
                    file.write(encode({'kind':'capture_gap','observed':event['observed'],'dropped':self.dropped-dropped})+'\n')
                    dropped=self.dropped
                file.write(encode(event)+'\n');file.flush()
        except Exception as exc:self.error=type(exc).__name__
        finally:
            if file:file.close()

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=10)


class MarketHub:
    def __init__(self,pair,interval=15,recorder=None):
        self.pair=pair;self.interval=interval;self.recorder=recorder
        self.lock=threading.Lock();self.book=None;self.quote_at=0.;self.quote_monotonic=0.;self.rows=[]
        self.history_at=0.;self.errors={};self.meta=None;self.meta_at=0.;self.market_status='unknown'
        self.source='';self.trades=deque(maxlen=2000);self.serial=0;self.last=None
        self.instrument=None

    def set_book(self,book,source,observed=None,last=None):
        at=time.time() if observed is None else observed
        normalized={side:[[str(p),str(q)] for p,q,*_ in book[side] if D(q)>0] for side in ('bids','asks')}
        for side in normalized:
            if not normalized[side]:raise BookError('Tom ordrebok.')
            if not all(D(p).is_finite() and D(q).is_finite() and D(p)>0 and D(q)>0 for p,q in normalized[side]):
                raise BookError('Ugyldig ordrebok.')
            normalized[side].sort(key=lambda r:D(r[0]),reverse=side=='bids')
        if D(normalized['bids'][0][0])>=D(normalized['asks'][0][0]):raise BookError('Krysset ordrebok.')
        with self.lock:
            self.book=normalized;self.quote_at=at;self.quote_monotonic=time.monotonic()
            self.source=source;self.errors.pop('quote',None);self.serial+=1
            if last is not None:self.last=str(last)
        if self.recorder:self.recorder.put('book',{'pair':self.pair,'book':normalized,'source':source,'last':self.last},at)

    def invalidate(self,component,error):
        with self.lock:
            self.errors[component]=str(error)
            if component=='quote':self.quote_monotonic=0
        if self.recorder:self.recorder.put('data_error',{'component':component,'error':str(error)})

    def set_history(self,rows):
        with self.lock:self.rows=copy.deepcopy(rows);self.history_at=time.time();self.errors.pop('history',None)
        if self.recorder:self.recorder.put('candles',{'pair':self.pair,'interval':self.interval,'rows':rows})

    def set_meta(self,key,metadata):
        with self.lock:
            self.meta=(key,copy.deepcopy(metadata));self.meta_at=time.time();self.market_status=metadata['status']
            self.errors.pop('metadata',None)
        if self.recorder:self.recorder.put('metadata',{'pair':self.pair,'key':key,'meta':metadata})

    def set_instrument(self,row):
        symbol='BTC/EUR' if self.pair=='XBTEUR' else 'ETH/EUR'
        if row.get('symbol')!=symbol:return
        mapping={'price_increment':'tick_size','qty_min':'ordermin','cost_min':'costmin'}
        with self.lock:
            self.instrument=copy.deepcopy(row);self.market_status=row['status']
            if self.meta:
                for source,target in mapping.items():
                    if source in row and (not D(row[source]).is_finite() or D(row[source])<=0):
                        raise BookError('Ugyldig oppdatert handelsregel.')
                    if source in row:self.meta[1][target]=str(row[source])
                if 'qty_precision' in row:self.meta[1]['lot_decimals']=int(row['qty_precision'])
                if 'qty_increment' in row:
                    step=D(10)**(-int(self.meta[1]['lot_decimals']))
                    if D(row['qty_increment'])!=step:
                        self.errors['metadata']='Ny mengdestørrelse krever kontroll av adapteren';self.market_status='unknown'
                self.meta[1]['status']=self.market_status
        if self.recorder:self.recorder.put('instrument',row)

    def add_trades(self,items):
        with self.lock:
            self.trades.extend(items)
            if items:self.last=str(items[-1]['price'])
        if self.recorder and items:self.recorder.put('trades',{'pair':self.pair,'trades':items})

    def snapshot(self):
        with self.lock:
            age=time.monotonic()-self.quote_monotonic if self.quote_monotonic else float('inf')
            return {'book':copy.deepcopy(self.book),'at':self.quote_at,'age':age,'rows':copy.deepcopy(self.rows),
                'history_at':self.history_at,'errors':dict(self.errors),'meta':copy.deepcopy(self.meta),
                'meta_at':self.meta_at,'status':self.market_status,'source':self.source,'serial':self.serial,
                'trades':list(self.trades),'last':self.last,'quote_ready':bool(self.book and age<=10),
                'capture_dropped':self.recorder.dropped if self.recorder else 0,
                'capture_error':self.recorder.error if self.recorder else ''}


class PublicData:
    def __init__(self,hub,use_websocket=False):
        self.hub=hub;self.use_websocket=use_websocket;self.stop=threading.Event();self.threads=[]
        self.ws=None

    def start(self):
        if self.use_websocket:
            self.ws=PublicWebSocket(self.hub,self.stop);self.ws.start()
        for task,name in [(self._quotes,'quotes'),(self._history,'candles'),(self._metadata,'instruments')]:
            thread=threading.Thread(target=task,name='NordBot-'+name,daemon=False);thread.start();self.threads.append(thread)

    def _quotes(self):
        api=Kraken(allow_private=False);failures=0;since=None
        while not self.stop.is_set():
            # WS owns the live book while fresh; REST fallback is an explicit source change.
            if self.use_websocket and self.hub.snapshot()['quote_ready'] and self.hub.source=='websocket':
                self.stop.wait(3);continue
            try:
                self.hub.set_book(api.depth(self.hub.pair,25),'rest');failures=0
            except Exception as exc:
                failures+=1;self.hub.invalidate('quote',type(exc).__name__+': '+str(exc))
            try:
                reply=api.call('Trades',{'pair':self.hub.pair,**({'since':since} if since else {})})
                since=str(reply['last']);rows=next(v for k,v in reply.items() if k!='last')
                items=[{'id':str(row[6]) if len(row)>6 else ':'.join(map(str,row[:5])),
                        'price':str(row[0]),'qty':str(row[1]),'time':float(row[2]),'side':row[3]} for row in rows]
                self.hub.add_trades(items)
            except Exception as exc:
                self.hub.invalidate('trades',type(exc).__name__)
            self.stop.wait(min(30,2**min(failures,5)) if failures else 3)

    def _history(self):
        api=Kraken(allow_private=False)
        while not self.stop.is_set():
            try:self.hub.set_history(api.history(self.hub.pair,self.hub.interval))
            except Exception as exc:self.hub.invalidate('history',type(exc).__name__+': '+str(exc))
            self.stop.wait(20)

    def _metadata(self):
        api=Kraken(allow_private=False)
        while not self.stop.is_set():
            try:self.hub.set_meta(*api.meta(self.hub.pair))
            except Exception as exc:self.hub.invalidate('metadata',type(exc).__name__+': '+str(exc))
            self.stop.wait(60)

    def close(self):
        self.stop.set()
        if self.ws:self.ws.close()
        for thread in self.threads:thread.join(timeout=12)


class AsyncStream:
    def __init__(self,stop):self.stop=stop;self.thread=None;self.loop=None;self.task=None
    def start(self):
        self.thread=threading.Thread(target=self._thread,name=type(self).__name__,daemon=False);self.thread.start()
    def _thread(self):
        async def run():
            self.loop=asyncio.get_running_loop();self.task=asyncio.current_task()
            try:await self.run()
            except asyncio.CancelledError:pass
        asyncio.run(run())
    def close(self):
        if self.loop and self.task and not self.loop.is_closed():self.loop.call_soon_threadsafe(self.task.cancel)
        if self.thread:self.thread.join(timeout=12)
    async def run(self):raise NotImplementedError


class PublicWebSocket(AsyncStream):
    def __init__(self,hub,stop):super().__init__(stop);self.hub=hub
    async def run(self):
        try:from websockets.asyncio.client import connect
        except ImportError:
            self.hub.invalidate('websocket','WebSocket-tillegget er ikke installert. Bruker REST.');return
        symbol='BTC/EUR' if self.hub.pair=='XBTEUR' else 'ETH/EUR'
        failures=0
        while not self.stop.is_set():
            book=OrderBook(symbol,25)
            try:
                async with connect('wss://ws.kraken.com/v2',open_timeout=8,close_timeout=2,
                                   ping_interval=10,ping_timeout=10,max_size=4*1024*1024,max_queue=128) as ws:
                    for channel in ('book','trade'):
                        params={'channel':channel,'symbol':[symbol],'snapshot':True}
                        if channel=='book':params['depth']=25
                        await ws.send(json.dumps({'method':'subscribe','params':params}))
                    await ws.send(json.dumps({'method':'subscribe','params':{'channel':'instrument','snapshot':True}}))
                    async for raw in ws:
                        if self.stop.is_set():break
                        message=json.loads(raw,parse_float=Decimal)
                        if message.get('success') is False:raise BookError(str(message.get('error','Subscription failed')))
                        channel=message.get('channel')
                        if channel=='book':
                            self.hub.set_book(book.apply(message),'websocket');failures=0
                        elif channel=='heartbeat' and book.valid:
                            self.hub.set_book(book.snapshot(),'websocket')
                        elif channel=='trade':
                            items=[{'id':str(r['trade_id']),'price':str(r['price']),'qty':str(r['qty']),
                                    'time':stamp(r['timestamp']),'side':r['side']} for r in message['data'] if r['symbol']==symbol]
                            self.hub.add_trades(items)
                        elif channel=='instrument':
                            data=message.get('data',{})
                            for row in data.get('pairs',[]):
                                if row['symbol']==symbol:
                                    self.hub.set_instrument(row)
                        elif channel=='status':
                            status=message['data'][0].get('system')
                            if status and status!='online':self.hub.invalidate('system','Kraken-status: '+status)
                            elif status=='online':
                                with self.hub.lock:self.hub.errors.pop('system',None)
            except asyncio.CancelledError:raise
            except Exception as exc:
                self.hub.invalidate('quote','WebSocket: '+type(exc).__name__+': '+str(exc));failures+=1
                await asyncio.sleep(min(30,2**min(failures,5)))


class ExecutionsStream(AsyncStream):
    """Private notifications only; credentials are confined to the broker and token."""
    def __init__(self,api,wake,stop):
        super().__init__(stop);self.api=api;self.wake=wake;self.events=queue.Queue(5000)
        self.ready=False;self.error='';self.sequence=None;self.last_received=0.

    def accept(self,message):
        if message.get('channel')!='executions':return
        sequence=message.get('sequence')
        if message.get('type')=='snapshot':self.sequence=None
        if type(sequence) is not int:raise BookError('Utførelsesstrøm mangler sekvensnummer.')
        if self.sequence is not None:
            if sequence<=self.sequence:return
            if sequence!=self.sequence+1:
                self.ready=False;raise BookError('Hull i privat utførelsesstrøm. Full avstemming kreves.')
        self.sequence=sequence
        try:self.events.put_nowait(message)
        except queue.Full:
            self.ready=False;raise BookError('For mange private hendelser. Avstemming kreves.')
        self.ready=True;self.error='';self.last_received=time.time();self.wake.set()

    async def run(self):
        try:from websockets.asyncio.client import connect
        except ImportError:self.error='WebSocket-tillegget mangler';return
        failures=0
        while not self.stop.is_set():
            try:
                result=await asyncio.to_thread(self.api.call,'GetWebSocketsToken',None,True)
                token=result['token']
                async with connect('wss://ws-auth.kraken.com/v2',open_timeout=8,close_timeout=2,
                                   ping_interval=10,ping_timeout=10,max_size=4*1024*1024,max_queue=128) as ws:
                    self.sequence=None
                    await ws.send(json.dumps({'method':'subscribe','params':{'channel':'executions',
                        'token':token,'snap_orders':True,'snap_trades':True,'ratecounter':True}}))
                    token=None
                    async for raw in ws:
                        if self.stop.is_set():break
                        message=json.loads(raw,parse_float=Decimal)
                        if message.get('success') is False:raise BookError(str(message.get('error','Private subscription failed')))
                        self.accept(message);failures=0
            except asyncio.CancelledError:raise
            except Exception as exc:
                self.ready=False;self.error=type(exc).__name__;failures+=1;self.wake.set()
                await asyncio.sleep(min(30,2**min(failures,5)))


def estimate_execution(book,side,quantity,limit=None):
    remaining=D(quantity);cost=D(0)
    for p,q,*_ in book['asks' if side=='buy' else 'bids']:
        price,available=D(p),D(q)
        if limit is not None and ((side=='buy' and price>D(limit)) or (side=='sell' and price<D(limit))):continue
        take=min(remaining,available);cost+=take*price;remaining-=take
        if remaining<=0:break
    filled=D(quantity)-remaining
    return {'filled_qty':str(filled),'unfilled_qty':str(remaining),
            'average_price':str(cost/filled) if filled else None,'notional':str(cost)}

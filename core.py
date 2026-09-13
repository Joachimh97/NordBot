"""NordBot 1.3 configuration, Kraken REST and legacy reference engine.
The GUI uses execution.ProtectedEngine in both paper and live mode.
"""
from __future__ import annotations
import base64, csv, hashlib, hmac, json, math, sqlite3, time, urllib.parse, urllib.request, uuid, threading, os
from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from datetime import datetime, timezone
import halts
from accounting import Ledger
from persistence import encode, initialize_tables, mirror_records, NonceStore
from limits import RateGovernor

D = lambda v: Decimal(str(v))
_KEY_LOCKS = {}
_KEY_GOVERNORS = {}
_KEY_LOCKS_GUARD = threading.Lock()

class SafetyError(Exception): pass
class APIError(Exception): pass
class SkipEntry(SafetyError): pass

@dataclass(frozen=True)
class Config:
    mode: str = 'paper'
    pair: str = 'XBTEUR'
    capital: float = 1000
    order_eur: float = 100
    fee: float = .008
    slippage: float = .001
    stop: float = .02
    target: float = .04
    trailing: float = .02
    daily_loss: float = .02
    total_loss: float = .05
    max_buys: int = 4
    interval: int = 15
    risk: float = .005
    rolling_loss: float = .03
    max_losses: int = 3
    strategy: str = 'ema_rebound'
    risk_model: str = 'fixed'
    entry_method: str = 'IOC'
    protection: str = 'separate'
    amend_stop: bool = False
    atr_period: int = 14
    atr_stop: float = 2.
    atr_target: float = 3.
    min_stop: float = .005
    max_stop: float = .05
    spread_max: float = .002
    min_net_reward: float = .005
    min_reward_risk: float = 0.
    regime_filter: bool = False
    max_atr_fraction: float = .05
    maker_fee: float = .004
    maker_lifetime: int = 60
    max_hold_hours: int = 6
    def validate(self):
        if self.mode not in ('paper','live') or self.pair not in ('XBTEUR','ETHEUR'):
            raise SafetyError('Ugyldig modus eller marked.')
        vals = [self.capital,self.order_eur,self.fee,self.slippage,self.stop,self.target,self.trailing,self.daily_loss,self.total_loss,self.risk,self.rolling_loss]
        if not all(math.isfinite(v) and v > 0 for v in vals):
            raise SafetyError('Alle tall må være positive og endelige.')
        if not 10 <= self.order_eur <= self.capital <= 100000:
            raise SafetyError('Handel må være minst 10 EUR og ikke overstige budsjettet.')
        if self.mode == 'live' and (self.capital > 100 or self.order_eur > 25):
            raise SafetyError('Førsteversjonen tillater maks 100 EUR live-budsjett og 25 EUR per kjøp.')
        if not (.0001 <= self.fee <= .02 and .0001 <= self.slippage <= .005):
            raise SafetyError('Gebyr må være 0,01–2 %, prisavvik 0,01–0,5 %.')
        if not all(.005 <= x <= .15 for x in [self.stop,self.trailing,self.daily_loss,self.total_loss]):
            raise SafetyError('Tapsgrenser må være mellom 0,5 og 15 %.')
        if not 2*(self.fee+self.slippage)+.005 < self.target <= .30:
            raise SafetyError('Kursmål må overstige estimerte tur-retur-kostnader med minst 0,5 prosentpoeng (maks 30 %).')
        if self.interval not in (1,5,15) or not isinstance(self.max_buys,int) or not 1 <= self.max_buys <= 10:
            raise SafetyError('Ugyldig intervall eller handelsgrense.')
        if not .001 <= self.risk <= .01 or not .005 <= self.rolling_loss <= .05:
            raise SafetyError('Beregnet risiko må være 0,1–1 %, rullende tapsgrense 0,5–5 %.')
        if type(self.max_losses) is not int or not 1 <= self.max_losses <= 5:
            raise SafetyError('Ugyldig grense for tapende handler.')
        if self.strategy not in ('ema_rebound','momentum') or self.risk_model not in ('fixed','atr'):
            raise SafetyError('Ukjent strategi eller risikomodell.')
        if self.entry_method not in ('IOC','FOK','POST') or self.protection not in ('separate','attached'):
            raise SafetyError('Ukjent ordre- eller beskyttelsesmetode.')
        extra=[self.atr_stop,self.atr_target,self.min_stop,self.max_stop,self.spread_max,
               self.min_net_reward,self.min_reward_risk,self.max_atr_fraction,self.maker_fee]
        if not all(math.isfinite(v) for v in extra):raise SafetyError('Ugyldige avanserte tall.')
        if type(self.amend_stop) is not bool or type(self.regime_filter) is not bool:
            raise SafetyError('Filter og stoppendring må være av eller på.')
        if not (type(self.atr_period) is int and 5<=self.atr_period<=60 and 0.5<=self.atr_stop<=5 and 1<=self.atr_target<=10
                and .005<=self.min_stop<=self.max_stop<=.15 and .0001<=self.spread_max<=.005
                and 0<=self.min_net_reward<=.1 and 0<=self.min_reward_risk<=5
                and .001<=self.max_atr_fraction<=.2 and 0<=self.maker_fee<=.02
                and type(self.maker_lifetime) is int and 5<=self.maker_lifetime<=300
                and type(self.max_hold_hours) is int and 1<=self.max_hold_hours<=6):
            raise SafetyError('Avanserte innstillinger er utenfor testgrensene.')
        if self.mode=='live' and (self.strategy!='ema_rebound' or self.interval!=15
                or self.risk_model!='fixed' or self.entry_method!='IOC' or self.regime_filter
                or self.protection!='separate' or self.amend_stop):
            raise SafetyError('Nye strategier, ATR, maker/FOK, følgeordre og stoppendring er foreløpig bare tilgjengelige i papirtester. Live beholder EMA og separat fast stopp.')

class Store:
    """Snapshot and audit event committed together. Uncertain orders survive a crash."""
    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, detail TEXT)')
        self.db.commit()
        initialize_tables(self.db)
    def load(self):
        row = self.db.execute('SELECT value FROM state WHERE id=1').fetchone()
        return json.loads(row[0]) if row else None
    def save(self, state, kind='tick', detail=None):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES (1,?)', (encode(state),))
            mirror_records(self.db,state)
            if kind != 'tick':
                self.db.execute('INSERT INTO events(ts,kind,detail) VALUES (?,?,?)',
                                (time.time(),kind,encode(detail or {})))
    def export(self, path):
        with open(path,'w',newline='',encoding='utf-8-sig') as f:
            w=csv.writer(f); w.writerow(['UTC','hendelse','detaljer'])
            for ts,k,v in self.db.execute('SELECT ts,kind,detail FROM events ORDER BY id'):
                w.writerow([datetime.fromtimestamp(ts,timezone.utc).isoformat(),k,v])
    def close(self): self.db.close()

def signature(secret, path, data):
    encoded=urllib.parse.urlencode(data)
    message=path.encode()+hashlib.sha256((str(data['nonce'])+encoded).encode()).digest()
    return base64.b64encode(hmac.new(base64.b64decode(secret,validate=True),message,hashlib.sha512).digest()).decode()

class Kraken:
    def __init__(self,key='',secret='',nonce_path=None,allow_private=None):
        self.key,self.secret,self.nonce=key,secret,0
        with _KEY_LOCKS_GUARD:
            fingerprint=hashlib.sha256(key.encode()).hexdigest()
            self._private_lock=_KEY_LOCKS.setdefault(fingerprint,threading.Lock())
            self.governor=_KEY_GOVERNORS.setdefault(fingerprint,RateGovernor())
        self.allow_private=bool(key and secret) if allow_private is None else allow_private
        self._nonce_store=None
        if key and self.allow_private:
            root=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'.local/share')))/'NordBot'
            self._nonce_store=NonceStore(nonce_path or root/'nonce.sqlite')
        self._health_cache=(0.,None)
        self._metrics=[]
        self._read_cache=None
    def begin_cycle(self):self._read_cache={}
    def end_cycle(self):self._read_cache=None
    def call(self, method, data=None, private=False):
        # Serialize the entire authenticated request, not merely nonce creation.
        if private:
            with self._private_lock:
                mutation=method in ('AddOrder','CancelOrder','AmendOrder')
                cacheable=method in ('OpenOrders','Balance','BalanceEx','QueryOrders','ClosedOrders')
                identity=(method,encode(data or {}))
                if mutation and self._read_cache is not None:self._read_cache.clear()
                if cacheable and self._read_cache is not None and identity in self._read_cache:
                    return json.loads(self._read_cache[identity])
                result=self._call(method,data,private)
                if cacheable and self._read_cache is not None:self._read_cache[identity]=encode(result)
                return result
        return self._call(method,data,private)
    def _call(self, method, data=None, private=False):
        data=dict(data or {})
        path='/0/'+('private/' if private else 'public/')+method
        headers={'User-Agent':'NordBot/1.3','Content-Type':'application/x-www-form-urlencoded'}
        if private:
            if not self.allow_private:raise SafetyError('Denne tilkoblingen tillater bare offentlige data. Papirhandel kan ikke sende private ordre.')
            if not self.key or not self.secret: raise SafetyError('API-nøkler mangler.')
            self.governor.acquire(method,data)
            self.nonce=(self._nonce_store.next(hashlib.sha256(self.key.encode()).hexdigest())
                        if self._nonce_store else max(self.nonce+1,time.time_ns()//1000000))
            data['nonce']=self.nonce
            headers.update({'API-Key':self.key,'API-Sign':signature(self.secret,path,data)})
        body=urllib.parse.urlencode(data).encode()
        req=urllib.request.Request('https://api.kraken.com'+path+('' if private else '?'+body.decode()),
                                   data=body if private else None,headers=headers)
        # No automatic retry, especially for mutations.
        started=time.monotonic()
        try:
            with urllib.request.urlopen(req,timeout=8) as r:
                blob=r.read(4*1024*1024+1)
                if len(blob)>4*1024*1024:raise APIError('For stort API-svar.')
                reply=json.loads(blob)
        finally:
            self._metrics=(self._metrics+[{'method':method,'duration':time.monotonic()-started,'at':time.time()}])[-100:]
        if reply.get('error'): raise APIError('; '.join(reply['error']))
        return reply['result']
    def meta(self,pair):
        r=self.call('AssetPairs',{'pair':pair}); key=next(iter(r)); m=r[key]
        if m['status']!='online' or m['quote']!='ZEUR': raise SafetyError('Markedet er ikke åpent for normal EUR-handel.')
        return key,m
    def market(self,pair,interval=15):
        t0=time.monotonic()
        server=self.call('Time')['unixtime']
        if abs(time.time()-server)>30: raise SafetyError('PC-klokken avviker fra børsen. Synkroniser Windows-klokken.')
        if self.call('SystemStatus')['status']!='online': raise SafetyError('Kraken er ikke i normal drift.')
        r=self.call('OHLC',{'pair':pair,'interval':interval})
        rows=next(v for k,v in r.items() if k!='last')[:-1]  # Never use unfinished candle.
        book=next(iter(self.call('Depth',{'pair':pair,'count':1}).values()))
        bid,ask=float(book['bids'][0][0]),float(book['asks'][0][0])
        if time.monotonic()-t0>15: raise SafetyError('Markedsdata kom for sent. Ingen handel.')
        validate_market(rows,bid,ask,time.time(),interval)
        return rows,bid,ask
    def account(self): return self.call('Balance',private=True)
    def orders(self): return self.call('OpenOrders',private=True)['open']
    def check_key(self):
        info=self.call('GetApiKeyInfo',private=True);perms=set(info['permissions'])
        # Do not guess additional permission identifiers. Unknown rights fail closed.
        allowed={'query-funds','query-open-trades','query-closed-trades','modify-trades','close-trades'}
        required={'query-funds','query-open-trades','query-closed-trades','modify-trades','close-trades'}
        if not required <= perms or not perms <= allowed:
            raise SafetyError('API-nøkkel må bare ha saldo, åpne/lukkede handler og opprette/kansellere ordre. Fjern alle andre rettigheter, særlig uttak.')
        self.key_info={'permissions':sorted(perms),'validUntil':info.get('validUntil','unknown'),
            'queryFrom':info.get('queryFrom','unknown'),'queryTo':info.get('queryTo','unknown')}
    def query(self,txid): return self.call('QueryOrders',{'txid':txid,'trades':'true'},True)[txid]
    def extended_balance(self):return self.call('BalanceEx',private=True)
    def fills(self,ids):return self.call('QueryTrades',{'txid':','.join(ids)},True)
    def history(self,pair,interval=15):
        r=self.call('OHLC',{'pair':pair,'interval':interval})
        return next(v for k,v in r.items() if k!='last')[:-1]
    def health(self):
        if time.monotonic()-self._health_cache[0]<10 and self._health_cache[1]:return
        if abs(time.time()-self.call('Time')['unixtime'])>30:
            raise SafetyError('Synkroniser Windows-klokken før ordre sendes.')
        if self.call('SystemStatus')['status']!='online':raise SafetyError('Kraken er ikke i normal drift.')
        self._health_cache=(time.monotonic(),True)
    def depth(self,pair,count=25):
        started=time.monotonic();self.health()
        book=next(iter(self.call('Depth',{'pair':pair,'count':count}).values()))
        if time.monotonic()-started>10:raise SafetyError('Ordreboken kom for sent.')
        return book
    def quote(self,pair):
        started=time.monotonic()
        if abs(time.time()-self.call('Time')['unixtime'])>30:
            raise SafetyError('Synkroniser Windows-klokken før ordre sendes.')
        if self.call('SystemStatus')['status']!='online':
            raise SafetyError('Kraken er ikke i normal drift.')
        book=next(iter(self.call('Depth',{'pair':pair,'count':1}).values()))
        bid,ask=float(book['bids'][0][0]),float(book['asks'][0][0])
        if not all(math.isfinite(v) and v>0 for v in (bid,ask)) or ask<bid or time.monotonic()-started>10:
            raise SafetyError('Ugyldig eller forsinket ordrebok. Ingen ny ordre.')
        return bid,ask

def validate_market(rows,bid,ask,now,interval=15):
    if not all(math.isfinite(x) and x>0 for x in (bid,ask)) or ask<bid:
        raise SafetyError('Ugyldig ordrebok.')
    if len(rows)<60: raise SafetyError('For lite kurshistorikk.')
    recent=rows[-60:]
    stamps=[int(r[0]) for r in recent]
    seconds=interval*60
    if any(b-a != seconds for a,b in zip(stamps,stamps[1:])):
        raise SafetyError('Manglende eller dupliserte prisperioder.')
    if not 0 <= now-(stamps[-1]+seconds) <= seconds+60:
        raise SafetyError('Historikken er utdatert eller fra fremtiden.')
    for r in recent:
        o,h,l,c=map(float,r[1:5])
        if not all(math.isfinite(x) and x>0 for x in [o,h,l,c]) or l>min(o,c) or h<max(o,c) or l>h:
            raise SafetyError('Ugyldige kursdata.')

def ema(values,n):
    out=[values[0]]; a=2/(n+1)
    for x in values[1:]: out.append(a*x+(1-a)*out[-1])
    return out

def signal(rows):
    closes=[float(r[4]) for r in rows]
    fast,slow=ema(closes,20),ema(closes,50)
    # Rebound above fast EMA while broad trend is rising. No future information.
    buy=(fast[-1]>slow[-1] and slow[-1]>slow[-5] and closes[-2]<=fast[-2] and closes[-1]>fast[-1])
    return buy, fast[-1]<slow[-1]

def rounded(value,step,up=False):
    return (D(value)/D(step)).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN)*D(step)

class Engine:
    def __init__(self,cfg,store,api=None):
        cfg.validate(); self.cfg,self.store,self.api=cfg,store,api or Kraken()
        self.s=store.load()
        if self.s:
            # Adding defaults never changes existing settings or forgets a position.
            merged={**asdict(Config()),**self.s['config']}
            if merged==asdict(cfg) and self.s['config']!=merged:
                self.s['config']=merged
                self.store.save(self.s,'config_schema_upgraded')
        if (self.s and self.s['config']!=asdict(cfg) and not self.s['account_id']
                and not self.s['trades'] and not self.s['pending'] and not self.s['qty'] and not self.s.get('halt') and not self.s.get('halts')):
            self.s=None
        if self.s and self.s['config']!=asdict(cfg):
            raise SafetyError('Eksisterende økt har andre innstillinger. Bruk innstillingene fra lagret økt. Papirøkter kan arkiveres.')
        if not self.s:
            self.s={'config':asdict(cfg),'cash':cfg.capital,'qty':0.,'basis':0.,'peak':0.,'entered':0.,
                    'last_bar':0,'last_trade':0.,'day':'','day_equity':cfg.capital,'buys':0,
                    'pending':None,'halt':'','exit_reason':'','fees':0.,'trades':0,'realized':0.,
                    'first_price':0.,'account_id':'','baseline_base':None,'last_equity':cfg.capital}
            self.store.save(self.s,'created',asdict(cfg))
        for name,value in {'risk_samples':[],'risk_last':0.,'loss_streak':0,'position_realized':0.,'estimated_risk':0.}.items():
            self.s.setdefault(name,value)
        halts.initialize(self.s)
        self.ledger=Ledger(self.s)
        self.s.setdefault('entry_stop_fraction',cfg.stop)
        self.s.setdefault('entry_target_fraction',cfg.target)
        self.s.setdefault('entry_stop_price',0.)
        self.s.setdefault('last_reconcile',0.)
        self.s.setdefault('last_signal_detail','Ingen vurdering ennå')
        self.key,self.meta=self.api.meta(cfg.pair)
        self.step=10**(-int(self.meta['lot_decimals']))
        if cfg.mode=='live':
            self.api.check_key()
            fingerprint=hashlib.sha256(self.api.key.encode()).hexdigest()
            if self.s['account_id'] and self.s['account_id']!=fingerprint:
                raise SafetyError('Denne økten tilhører en annen API-nøkkel. Ikke bytt konto eller nøkkel under en aktiv økt.')
            self.s['account_id']=fingerprint
            self.check_fee()
            if self.s['pending']: self.recover()
            self.check_account()
            self.store.save(self.s)
    def equity(self,bid):
        return float(self.ledger.equity(bid,self.fee_rate(),self.cfg.slippage))
    def fee_rate(self):return self.cfg.fee
    def entry_size(self,bid,ask):
        c=self.cfg; fee=self.fee_rate()
        price=rounded(D(bid) if c.entry_method=='POST' else D(ask)*(1+D(c.slippage)),self.meta['tick_size'])
        stop=rounded(price*(1-D(self.s.get('entry_stop_fraction',c.stop))),self.meta['tick_size'],up=True)
        buy_fee=D(c.maker_fee if c.entry_method=='POST' and c.mode=='paper' else fee)
        per_coin=price*(1+buy_fee)-stop*(1-D(c.slippage))*(1-D(fee))
        risk_budget=min(D(c.capital),max(D(0),self.ledger.equity(bid,fee,c.slippage)))*D(c.risk)
        budget=min(D(c.order_eur),self.ledger.get('cash'))
        qty=rounded(min(budget/(price*(1+buy_fee)),risk_budget/per_coin),self.step)
        if qty<D(self.meta['ordermin']) or qty*stop<D(self.meta.get('costmin','0')):
            raise SkipEntry('Hopper over kjøp: risikobudsjettet gir mindre enn børsens minsteordre.')
        self.s['estimated_risk']=float(qty*per_coin)
        self.s['planned_stop_price']=str(stop)
        self.s['planned_entry']={'quantity':str(qty),'limit':str(price),'stop':str(stop),
            'risk_eur':str(qty*per_coin),'outlay_eur':str(qty*price*(1+buy_fee)),
            'buy_fee':str(buy_fee),'sell_fee':str(D(fee))}
        return qty,price
    def risk_check(self,equity,now):
        s,c=self.s,self.cfg
        if now<s['risk_last']:raise SafetyError('Tid gikk bakover. Kontroller klokken før videre handel.')
        # Monotone queue gives the exact rolling peak without storing every flat tick.
        samples=[]
        for point in s['risk_samples']:
            if point[0]<now-86400:continue
            while samples and samples[-1][1]<=point[1]:samples.pop()
            samples.append(point)
        while samples and samples[-1][1]<=equity:samples.pop()
        samples.append([now,equity]);s['risk_samples']=samples;s['risk_last']=now
        high=samples[0][1]
        if high>0 and equity<=high*(1-c.rolling_loss):
            halts.latch(s,'rolling','Rullende 24-timers tapsgrense',now)
        if s['loss_streak']>=c.max_losses:
            halts.latch(s,'streak',f'{c.max_losses} tapende handler på rad',now)
    def check_fee(self):
        fees=self.api.call('TradeVolume',{'pair':self.cfg.pair},True)['fees']
        rate=float(next(iter(fees.values()))['fee'])/100
        if not math.isfinite(rate) or rate<0 or rate>self.cfg.fee:
            raise SafetyError('Oppgitt gebyr er lavere enn takergebyret på kontoen. Oppdater gebyrinnstillingen før første live-økt.')
    def check_account(self):
        balances=self.api.account()
        if self.api.orders(): raise SafetyError('Åpne ordre finnes på kontoen. Avklar dem på Kraken før oppstart.')
        base=float(balances.get(self.meta['base'],0))
        if self.s['baseline_base'] is None:
            if base>self.step*2: raise SafetyError('Start live med null beholdning av valgt krypto. Bruk en separat konto/underkonto hvis tilgjengelig.')
            self.s['baseline_base']=base
        expected=self.s['baseline_base']+self.s['qty']
        if abs(base-expected)>self.step*2:
            raise SafetyError('Børssaldo avviker fra botens beholdning. Kontroller handler og gebyrvaluta på Kraken; ikke fortsett automatisk.')
        if float(balances.get(self.meta['quote'],0))+.01 < self.s['cash']:
            raise SafetyError('For lite tilgjengelig EUR for lagret bot-budsjett.')
    def recover(self):
        p=self.s['pending']
        if not p: return
        if not p.get('txid'):
            found=[]
            opened=self.api.orders()
            found += [(k,v) for k,v in opened.items() if v.get('cl_ord_id')==p['id']]
            offset=0
            while True:
                r=self.api.call('ClosedOrders',{'start':int(p['at'])-60,'ofs':offset},True)
                found += [(k,v) for k,v in r['closed'].items() if v.get('cl_ord_id')==p['id']]
                offset+=len(r['closed'])
                if offset>=r['count'] or not r['closed']: break
                if offset>=1000: raise SafetyError('For mange ordre å avstemme automatisk.')
            if len(found)!=1:
                raise SafetyError('Ordreutfallet er ukjent. Ingen ny ordre sendes. Se hendelsesloggen og kontroller ordre-ID på Kraken.')
            p['txid']=found[0][0]; self.store.save(self.s,'order_found',{'txid':p['txid']})
        o=self.api.query(p['txid'])
        if o['status'] not in ('closed','canceled','expired'):
            raise SafetyError('Ordren er ikke ferdigbehandlet. Vent og bruk Avstem ordre.')
        if o['descr']['type']!=p['side'] or o['descr']['pair'] not in (self.cfg.pair,self.key,self.meta.get('altname'),self.meta.get('wsname')):
            raise SafetyError('Ordredetaljene stemmer ikke med journalen.')
        qty,cost,fee=map(float,(o['vol_exec'],o['cost'],o['fee']))
        if not all(math.isfinite(x) and x>=0 for x in (qty,cost,fee)) or qty>float(p['qty'])+self.step:
            raise SafetyError('Uventet ordresvar.')
        self.apply_fill(p['side'],qty,cost,fee,p['reason'],p['at'])
    def apply_fill(self,side,qty,cost,fee,reason,now):
        s=self.s
        if side=='buy':
            self.ledger.buy(qty,cost,fee,first=bool(qty))
            if qty:
                s['peak']=cost/qty; s['entry_price']=cost/qty; s['entered']=now; s['buys']+=1
                s['entry_stop_price']=s['entry_price']*(1-s['entry_stop_fraction'])
        else:
            if qty>s['qty']+self.step: raise SafetyError('Salg overstiger botens beholdning.')
            self.ledger.sell(qty,cost,fee,self.step)
            if s['qty']<self.step/2:
                self.ledger.set(qty=0,basis=0);s['peak']=0.;s['exit_reason']=''
                if qty:
                    s['loss_streak']=s['loss_streak']+1 if s['position_realized']<0 else 0
                    if s['loss_streak']>=self.cfg.max_losses:
                        halts.latch(s,'streak',f'{self.cfg.max_losses} tapende handler på rad',now)
        s['trades']+=int(qty>0); s['last_trade']=now; s['pending']=None
        self.store.save(s,'fill',{'side':side,'qty':qty,'cost_eur':cost,'fee_eur':fee,'reason':reason})
    def trade(self,side,bid,ask,reason,now):
        if side=='buy' and not getattr(self,'entry_guard',lambda:True)():raise SkipEntry('Nye kjøp er stanset.')
        if self.s['pending']: raise SafetyError('Uavklart ordre sperrer ny handel.')
        if side=='buy' and (self.s['halt'] or self.s['qty']): raise SafetyError('Kjøp er blokkert.')
        if self.cfg.mode=='live':
            checked_at=time.monotonic()
            self.check_fee()
            self.check_account()
            if time.monotonic()-checked_at>10:
                raise SafetyError('Kontokontrollen tok for lang tid. Prisgrunnlaget er for gammelt for en ny ordre.')
        limit=rounded(ask*(1+self.cfg.slippage) if side=='buy' else bid*(1-self.cfg.slippage),
                      self.meta['tick_size'],up=side=='sell')
        if side=='buy':
            qty,limit=self.entry_size(bid,ask)
        else: qty=rounded(self.s['qty'],self.step)
        if qty<D(self.meta['ordermin']) or qty*limit<D(self.meta.get('costmin','0')):
            raise SafetyError('Beløpet er under børsens minsteordre. En liten rest må eventuelt håndteres på Kraken.')
        if self.cfg.mode=='paper':
            cost=float(qty*limit)
            self.apply_fill(side,float(qty),cost,cost*self.cfg.fee,reason,now); return
        p={'id':str(uuid.uuid4()),'txid':None,'side':side,'qty':str(qty),'limit':str(limit),'reason':reason,'at':now}
        self.s['pending']=p
        self.store.save(self.s,'intent',p)  # Must finish BEFORE sending any order.
        data={'pair':self.cfg.pair,'type':side,'ordertype':'limit','price':str(limit),'volume':str(qty),
              'timeinforce':'IOC','oflags':'fciq','cl_ord_id':p['id']}
        # Any failure after intent stays unresolved. Never resend automatically.
        try:
            result=self.api.call('AddOrder',data,True)
        except APIError as exc:
            definite=('EOrder:Insufficient funds','EOrder:Order minimum not met','EOrder:Cost minimum not met',
                      'EGeneral:Invalid arguments','EAPI:Invalid key','EAPI:Invalid signature','EGeneral:Permission denied')
            if any(str(exc).startswith(code) for code in definite):
                self.s['pending']=None
                self.store.save(self.s,'rejected',{'id':p['id']})
            # Internal/server errors may be ambiguous; preserve intent in that case.
            raise
        p['txid']=result['txid'][0]
        self.store.save(self.s,'ack',{'txid':p['txid']})
        self.recover()
    def monitor(self,bid,ask,now=None,liquidate=False):
        """Position risk needs a fresh quote, never an OHLC or news request."""
        from policy import exit_reason
        now=time.time() if now is None else now
        if not all(math.isfinite(v) and v>0 for v in (bid,ask,now)) or ask<bid:
            raise SafetyError('Risikokontroll mangler en gyldig pris.')
        s,c=self.s,self.cfg
        halts.initialize(s,now)
        equity=self.equity(bid);day=datetime.fromtimestamp(now,timezone.utc).date().isoformat()
        if not s['first_price']:s['first_price']=ask*(1+c.slippage)
        if day!=s['day']:
            halts.rollover(s,day)
            s.update(day=day,day_equity=equity,buys=0)
        if equity<=c.capital*(1-c.total_loss):halts.latch(s,'total','Samlet tapsgrense',now)
        if equity<=s['day_equity']*(1-c.daily_loss):halts.latch(s,'daily','Døgnets tapsgrense',now)
        self.risk_check(equity,now)
        reason=''
        if s['qty']:
            s['peak']=max(s['peak'],bid)
            reason='Manuelt avslutt posisjon' if liquidate else exit_reason(s,c,bid,now)
            if reason:
                s['exit_reason']=reason;self.store.save(s)
                self.trade('sell',bid,ask,reason,now)
        elif liquidate:reason='Ingen posisjon å selge'
        s['last_equity']=self.equity(bid)
        self.store.save(s)
        return reason

    def snapshot(self,bid,ask,reason='',rows=None):
        s,c=self.s,self.cfg
        equity=self.equity(bid)
        first=s['first_price'] or ask*(1+c.slippage)
        hold=c.capital/(first*(1+self.fee_rate()))*bid*(1-self.fee_rate())*(1-c.slippage)-c.capital
        return {'bid':bid,'ask':ask,'equity':equity,'pnl':equity-c.capital,'hold':hold,
                'cash':s['cash'],'qty':s['qty'],'fees':s['fees'],'trades':s['trades'],
                'realized':s['realized'],'reason':reason or s['halt'] or s['last_signal_detail'],
                'closes':[float(r[4]) for r in (rows or [])[-80:]],'halts':halts.reasons(s),
                'plan':s.get('entry_plan',{}),'planned_entry':s.get('planned_entry',{}),
                'last_reconcile':s.get('last_reconcile',0)}

    def tick(self,rows,bid,ask,now=None,liquidate=False,entry_allowed=True):
        from policy import decision,cost_plan,validate_entry_plan,exit_reason
        now=time.time() if now is None else now
        # Risk runs even if the subsequent history check fails.
        reason=self.monitor(bid,ask,now,liquidate)
        validate_market(rows,bid,ask,now,self.cfg.interval)
        s,c=self.s,self.cfg
        if s['pending']:self.recover()
        if c.mode=='live' and not getattr(self,'protected_execution',False):self.check_account()
        if c.strategy=='ema_rebound' and not c.regime_filter:
            buy,trend_exit=signal(rows)
            detail=decision(rows,c)[2]
        else:buy,trend_exit,detail=decision(rows,c)
        s['last_signal_detail']=detail
        bar=int(rows[-1][0]);new=bar>s['last_bar'];s['last_bar']=max(s['last_bar'],bar)
        if s['qty'] and new and trend_exit and not reason:
            reason=exit_reason(s,c,bid,now,trend_exit=True)
            if reason:
                s['exit_reason']=reason;self.store.save(s);self.trade('sell',bid,ask,reason,now)
        elif not s['qty'] and not reason and entry_allowed and not s['halt'] and new and buy:
            if s['buys']>=c.max_buys:reason='Dagens kjøpsgrense er nådd'
            elif now-s['last_trade']<900:reason='Venter på 15 minutters pause etter siste utførelse'
            elif (ask-bid)/bid>c.spread_max:reason='Hopper over kjøp: spread er for stor'
            else:
                plan=cost_plan(c,rows,bid,ask,self.fee_rate(),getattr(self,'maker',c.maker_fee))
                s['entry_plan']=plan
                try:
                    validate_entry_plan(plan,c)
                    s['entry_stop_fraction']=plan['stop_fraction'];s['entry_target_fraction']=plan['target_fraction']
                    self.store.save(s)
                    self.trade('buy',bid,ask,'Strategisignal: '+c.strategy,now)
                    reason='Kjøpssignal behandlet'
                except SkipEntry as exc:reason=str(exc)
        if not reason:reason=s['halt'] or (detail if entry_allowed else 'Modell/datafilter blokkerer nye kjøp')
        s['last_equity']=self.equity(bid);self.store.save(s)
        return self.snapshot(bid,ask,reason,rows)

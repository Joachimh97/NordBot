"""Persistent paper exchange, using the same ProtectedEngine as live execution.

L2 taker fills consume displayed depth. Maker fills require later sell trades and
consume a conservative queue-ahead estimate. A touched candle NEVER fills a maker.
This is a model of execution, not a promise of Kraken queue position or fills.
"""
import copy
import json
import time
from core import D, APIError, SafetyError,rounded
from persistence import encode


class PaperExchange:
    simulated=True
    key='paper-local-no-credentials'

    def __init__(self,cfg,key,metadata,store,now=None,durable=True):
        if cfg.mode!='paper':raise SafetyError('Simulatoren kan bare brukes i papirhandel.')
        self.cfg=cfg;self.pair_key=key;self.metadata=copy.deepcopy(metadata);self.store=store
        self.now=time.time() if now is None else now
        self.durable=durable;self.step=D(10)**(-int(metadata['lot_decimals']))
        self.book=None;self.last_price=None;self.rows=[]
        self.participation=D(1);self.fill_fraction=D(1)
        self.inject={}
        store.db.execute('CREATE TABLE IF NOT EXISTS paper_exchange (id INTEGER PRIMARY KEY,value TEXT NOT NULL)')
        row=store.db.execute('SELECT value FROM paper_exchange WHERE id=1').fetchone()
        self.state=json.loads(row[0]) if row else {'cash':str(D(cfg.capital)),'base':'0','orders':{},
                'trades':{},'order_serial':0,'trade_serial':0,'processed_trades':[],'observed_at':self.now}
        self.now=max(self.now,self.state.get('observed_at',self.now))
        self.sent=[];self.canceled=[];self._persist()

    def _persist(self):
        if not self.durable:return
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO paper_exchange VALUES (1,?)',(encode(self.state),))

    def _failure(self,name):
        failure=self.inject.pop(name,None)
        if failure:raise failure

    def meta(self,pair):
        if pair!=self.cfg.pair:raise SafetyError('Simulatoren har feil marked.')
        self.step=D(10)**(-int(self.metadata['lot_decimals']))
        return self.pair_key,copy.deepcopy(self.metadata)

    def check_key(self):pass

    def quote(self,pair):
        if pair!=self.cfg.pair or not self.book:raise SafetyError('Papirbørsen mangler en fersk ordrebok.')
        return float(self.book['bids'][0][0]),float(self.book['asks'][0][0])

    def depth(self,pair,count=25):
        self.quote(pair)
        return {side:copy.deepcopy(self.book[side][:count]) for side in ('bids','asks')}

    def market(self,pair,interval=15):return self.rows,*self.quote(pair)
    def account(self):return {self.metadata['base']:self.state['base'],self.metadata['quote']:self.state['cash']}

    def extended_balance(self):
        held_base=D(0);held_eur=D(0)
        for row in self.state['orders'].values():
            if row['status'] not in ('open','pending'):continue
            rest=D(row['vol'])-D(row['vol_exec'])
            if row['descr']['type']=='sell':held_base+=rest
            elif row['descr']['ordertype']=='limit':held_eur+=rest*D(row['descr']['price'])*(1+D(self.cfg.fee))
        return {self.metadata['base']:{'balance':self.state['base'],'hold_trade':str(held_base)},
                self.metadata['quote']:{'balance':self.state['cash'],'hold_trade':str(held_eur)}}

    def orders(self):return {k:copy.deepcopy(v) for k,v in self.state['orders'].items() if v['status'] in ('open','pending')}
    def query(self,txid):
        self._failure('query');return copy.deepcopy(self.state['orders'][txid])
    def fills(self,ids):return {k:copy.deepcopy(self.state['trades'][k]) for k in ids}

    def _new_order(self,payload,refid=None):
        self.state['order_serial']+=1;txid='PAPER-'+str(self.state['order_serial'])
        row={'refid':refid,'status':'open','cl_ord_id':payload.get('cl_ord_id',''),
             'descr':{'pair':self.cfg.pair,'type':payload['type'],'ordertype':payload['ordertype'],
                      'price':payload.get('price','0'),'leverage':'none'},
             'vol':str(payload['volume']),'vol_exec':'0','cost':'0','fee':'0',
             'oflags':payload.get('oflags','fciq'),'opentm':self.now,'closetm':0,'trades':[],
             'payload':copy.deepcopy(payload),'queue_ahead':'0'}
        self.state['orders'][txid]=row
        return txid,row

    def _fill(self,txid,quantity,price,maker=False):
        row=self.state['orders'][txid];q=D(quantity);price=D(price)
        if q<=0:return
        cost=q*price;fee=cost*D(self.cfg.maker_fee if maker else self.cfg.fee)
        if row['descr']['type']=='sell' and q>D(self.state['base']):raise SafetyError('Simulator: salg overstiger saldo.')
        if row['descr']['type']=='buy' and cost+fee>D(self.state['cash']):raise APIError('EOrder:Insufficient funds')
        row['vol_exec']=str(D(row['vol_exec'])+q);row['cost']=str(D(row['cost'])+cost);row['fee']=str(D(row['fee'])+fee)
        side=row['descr']['type']
        self.state['base']=str(D(self.state['base'])+(q if side=='buy' else -q))
        self.state['cash']=str(D(self.state['cash'])+(-cost-fee if side=='buy' else cost-fee))
        self.state['trade_serial']+=1;trade_id='PAPER-TRADE-'+str(self.state['trade_serial'])
        self.state['trades'][trade_id]={'ordertxid':txid,'time':self.now,'pair':self.cfg.pair,
            'type':side,'vol':str(q),'price':str(price),'cost':str(cost),'fee':str(fee),
            'maker':maker,'simulation':True}
        row['trades'].append(trade_id)
        if D(row['vol_exec'])==D(row['vol']):row['status']='closed';row['closetm']=self.now
        parent=row['payload']
        if side=='buy' and parent.get('close[ordertype]'):
            stop={'type':'sell','ordertype':parent['close[ordertype]'],'volume':str(q),
                  'price':parent['close[price]'],'timeinforce':'GTC','oflags':'fciq'}
            child,record=self._new_order(stop,refid=txid)
            if q<D(self.metadata['ordermin']) or q*D(stop['price'])<D(self.metadata.get('costmin',0)):
                record['status']='canceled';record['reason']='Simulert følgeordre under minimum'
            if self.inject.pop('reject_child',False):
                record['status']='canceled';record['reason']='Simulert avvist følgeordre'

    def _levels(self,row):
        buy=row['descr']['type']=='buy';limit=D(row['descr']['price'])
        needed=D(row['vol'])-D(row['vol_exec']);fills=[]
        for level in self.book['asks' if buy else 'bids']:
            price,available=map(D,level[:2])
            if row['descr']['ordertype']=='limit' and ((buy and price>limit) or (not buy and price<limit)):continue
            quantity=rounded(min(needed,available*self.participation*self.fill_fraction),self.step)
            # The request's limit still caps a conservative latency-price adjustment.
            adjusted=(min(limit,price*(1+D(self.cfg.slippage))) if buy else price*(1-D(self.cfg.slippage)))
            if not buy and row['descr']['ordertype']=='limit':adjusted=max(limit,adjusted)
            if quantity>0:fills.append((quantity,adjusted,level));needed-=quantity
            if needed<=0:break
        return fills

    def _execute(self,txid):
        row=self.state['orders'][txid];fills=self._levels(row)
        if row['payload'].get('timeinforce')=='FOK' and sum((q for q,_,_ in fills),D(0))<D(row['vol']):
            row['status']='canceled';return
        for quantity,price,level in fills:
            self._fill(txid,quantity,price)
            level[1]=str(max(D(0),D(level[1])-quantity))
        if row['status']=='open' and (row['payload'].get('timeinforce') in ('IOC','FOK') or row['descr']['ordertype'] in ('market','stop-loss')):
            row['status']='canceled';row['closetm']=self.now

    def update(self,book,now,rows=None,trades=(),last=None,participation=1):
        if now<self.state.get('observed_at',0):raise SafetyError('Simulatoren mottok tid bakover.')
        if not book.get('bids') or not book.get('asks'):raise SafetyError('Tom simulert ordrebok.')
        self.book={side:[[str(p),str(q)] for p,q,*_ in book[side]] for side in ('bids','asks')}
        self.now=float(now);self.participation=D(participation)
        self.state['observed_at']=self.now
        if rows is not None:self.rows=rows
        bid,ask=self.quote(self.cfg.pair)
        self.last_price=D(last if last is not None else bid)
        for txid,row in list(self.state['orders'].items()):
            if row['status'] not in ('open','pending'):continue
            payload=row['payload']
            if payload.get('timeinforce')=='GTD' and self.now>=float(payload['expiretm']):
                row['status']='expired';row['closetm']=self.now;continue
            if row['descr']['ordertype']=='stop-loss' and self.last_price<=D(row['descr']['price']):self._execute(txid)
        for trade in trades:
            ident=str(trade['id'])
            if ident in self.state['processed_trades'] or float(trade['time'])>self.now:continue
            self.state['processed_trades'].append(ident)
            if trade['side'] not in ('sell','s'):continue
            quantity=D(trade['qty'])
            for txid,row in list(self.state['orders'].items()):
                if row['status']!='open' or 'post' not in row.get('oflags',''):continue
                if float(trade['time'])<=row['opentm'] or D(trade['price'])>D(row['descr']['price']):continue
                ahead=D(row['queue_ahead']);used=min(ahead,quantity);quantity-=used
                row['queue_ahead']=str(ahead-used)
                fill=rounded(min(D(row['vol'])-D(row['vol_exec']),quantity*self.participation),self.step)
                if fill>0:self._fill(txid,fill,D(row['descr']['price']),maker=True);quantity-=fill
        self.state['processed_trades']=self.state['processed_trades'][-20000:]
        self._persist()

    def call(self,method,data=None,private=False):
        """Kraken-shaped local protocol. There is deliberately no network fallback."""
        data=dict(data or {})
        if method=='TradeVolume':return {'fees':{self.pair_key:{'fee':str(self.cfg.fee*100)}},
            'fees_maker':{self.pair_key:{'fee':str(self.cfg.maker_fee*100)}}}
        if method=='ClosedOrders':
            rows={k:v for k,v in self.state['orders'].items() if v['status'] not in ('open','pending') and v['opentm']>=float(data.get('start',0))}
            offset=int(data.get('ofs',0));keys=list(rows)[offset:offset+50]
            return {'closed':{k:copy.deepcopy(rows[k]) for k in keys},'count':len(rows)}
        if method=='CancelOrder':
            row=self.state['orders'][data['txid']]
            self._failure('before_cancel')
            if row['status'] in ('open','pending'):row['status']='canceled';row['closetm']=self.now
            self.canceled.append(data['txid']);self._persist();self._failure('after_cancel')
            return {'count':1}
        if method=='AmendOrder':
            row=self.state['orders'][data['txid']]
            if row['status']!='open' or 'close[ordertype]' in row['payload']:
                raise APIError('EGeneral:Invalid arguments: unsupported amend')
            if D(data['trigger_price'])<D(row['descr']['price']):raise APIError('EGeneral:Invalid arguments: widening stop')
            row['descr']['price']=str(data['trigger_price']);row['payload']['price']=str(data['trigger_price'])
            self._persist();self._failure('after_amend');return {'amend_id':'PAPER-AMEND'}
        if method!='AddOrder':raise SafetyError('Simulatoren støtter ikke dette endepunktet: '+method)
        self._failure('before_add')
        if self.metadata.get('status')!='online':raise APIError('EGeneral:Invalid arguments: market not online')
        if data.get('leverage') or data.get('reduce_only') or data.get('type') not in ('buy','sell'):
            raise APIError('EGeneral:Invalid arguments: spot only')
        if data.get('ordertype') not in ('limit','market','stop-loss'):raise APIError('EGeneral:Invalid arguments: unsupported order')
        if not self.book:raise SafetyError('Simulatoren har ingen ordrebok.')
        qty=D(data['volume']);price=D(data.get('price') or self.quote(self.cfg.pair)[0])
        if not qty.is_finite() or qty<=0 or qty%self.step:
            raise APIError('EGeneral:Invalid arguments: volume precision')
        if not price.is_finite() or price<=0 or (data.get('price') and price%D(self.metadata['tick_size'])):
            raise APIError('EOrder:Tick size')
        if qty<D(self.metadata['ordermin']) or qty*price<D(self.metadata.get('costmin',0)):
            raise APIError('EOrder:Order minimum not met')
        if any(r.get('cl_ord_id')==data.get('cl_ord_id') and r['status']=='open' for r in self.state['orders'].values()):
            raise APIError('EGeneral:Invalid arguments: duplicate client id')
        bid,ask=self.quote(self.cfg.pair)
        if 'post' in data.get('oflags','') and price>=D(ask):raise APIError('EOrder:Post only order')
        if data.get('validate') in (True,'true'):return {'descr':{'order':'Validated in simulator'}}
        txid,row=self._new_order(data);self.sent.append(copy.deepcopy(data))
        if 'post' in data.get('oflags',''):
            row['queue_ahead']=str(sum((D(q) for p,q in self.book['bids'] if D(p)>=price),D(0)))
        elif data['ordertype']!='stop-loss':self._execute(txid)
        self._persist();self._failure('after_add')
        return {'txid':[txid]}

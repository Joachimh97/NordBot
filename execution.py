"""Journalled Kraken Spot execution. No automatic retry of AddOrder.

Fixed exchange stop after confirmed entry; trailing/target remain local.
Cancellation must reach a terminal exchange state before any replacement sale.
All amounts attributed to this bot; pre-existing coins are a protected baseline.
"""
import hashlib
import math
import time
import uuid
import urllib.error
from dataclasses import asdict
from core import Engine, Config, SafetyError, SkipEntry, APIError, D, rounded
import halts
from persistence import record_exchange_fill,encode
from datetime import datetime,timezone
from limits import BeforeSend

TERMINAL={'closed','canceled','expired','rejected'}

class UnresolvedOrder(SafetyError): pass
class AccountMismatch(SafetyError): pass

def transient(exc):
    if isinstance(exc,BeforeSend):return True
    if isinstance(exc,urllib.error.HTTPError):return exc.code in (408,429,500,502,503,504)
    if isinstance(exc,(TimeoutError,ConnectionError,urllib.error.URLError,UnresolvedOrder,AccountMismatch)):return True
    return isinstance(exc,APIError) and str(exc).startswith((
        'EAPI:Rate limit','EOrder:Rate limit','EService:Unavailable','EService:Busy','EService:Internal error'))

def definite_rejection(exc):
    if isinstance(exc,BeforeSend):return True
    return isinstance(exc,APIError) and str(exc).startswith((
        'EOrder:Insufficient funds','EOrder:Order minimum not met','EOrder:Cost minimum not met',
        'EOrder:Invalid price','EOrder:Tick size','EGeneral:Invalid arguments','EAPI:Invalid key',
        'EAPI:Invalid signature','EAPI:Invalid nonce','EGeneral:Permission denied'))

def fee_rates(api,pair):
    result=api.call('TradeVolume',{'pair':pair},True)
    taker=float(next(iter(result['fees'].values()))['fee'])/100
    maker=float(next(iter(result['fees_maker'].values()))['fee'])/100
    if not math.isfinite(taker) or not 0<=taker<=.02 or not math.isfinite(maker) or not -.01<=maker<=.02:
        raise SafetyError('Uventede gebyrsatser fra Kraken.')
    return maker,taker

def preflight(cfg,api):
    """Read-only. No journal adoption, order placement, cancellation or validation orders."""
    if cfg.mode!='live':raise SafetyError('Velg live for å kontrollere API-tilkoblingen.')
    cfg.validate();api.check_key();key,meta=api.meta(cfg.pair)
    maker,taker=fee_rates(api,cfg.pair);balances=api.account();orders=api.orders()
    bid,ask=api.quote(cfg.pair)
    return {'eur':float(balances.get(meta['quote'],0)),
            'base':float(balances.get(meta['base'],0)), 'maker':maker,'taker':taker,
            'orders':len(orders),'min_qty':float(meta['ordermin']),
            'min_eur':max(float(meta.get('costmin','0')),float(meta['ordermin'])*ask),
            'bid':bid,'ask':ask}

class ProtectedEngine(Engine):
    protected_execution=True
    def __init__(self,cfg,store,api):
        if cfg.mode=='paper' and not getattr(api,'simulated',False):
            raise SafetyError('Papirhandel krever en simulator som ikke kan sende private Kraken-ordre.')
        old=store.load()
        if old and old.get('execution_schema')!=2:
            if old.get('qty') or old.get('pending'):
                raise SafetyError('Gammel live-økt har beholdning eller uavklart ordre. Avklar den i forrige versjon og på Kraken før oppgradering. Ikke slett journalen.')
            # A flat legacy journal may gain conservative risk defaults, never reset PnL.
            migrated={**asdict(Config()),**old['config']}
            if migrated!=asdict(cfg):raise SafetyError('Bruk innstillingene fra den gamle live-økten før oppgradering.')
            old['config']=migrated;old['execution_schema']=2
            store.save(old,'migrated_flat_session')
        self.taker=cfg.fee;self.maker=None;self.fee_checked=0.
        super().__init__(cfg,store,api)
        self._setup()
        if cfg.mode=='paper':self.check_fee();self.check_account()
        self.store.save(self.s)

    def _setup(self):
        self.s.setdefault('execution_schema',2)
        self.s.setdefault('orders_v2',{})
        self.s.setdefault('entry_price',0.)
        self.s.setdefault('entry_volume',0.)
        self.s.setdefault('entry_cost',0.)
        self.s.setdefault('completed_positions',[])
        self.s.setdefault('position_finalized','')
        self.s.setdefault('protection_unconfirmed_since',0.)
        self.s.setdefault('execution_state','FLAT')

    def now(self):return self.api.now if getattr(self.api,'simulated',False) else time.time()

    def refresh_meta(self):
        key,meta=self.api.meta(self.cfg.pair)
        if meta['base']!=self.meta['base'] or meta['quote']!=self.meta['quote']:
            raise SafetyError('Markedets valutaidentitet er endret. Stans nye ordre.')
        self.key,self.meta=key,meta;self.step=10**(-int(meta['lot_decimals']))
        self.s['metadata_checked']=self.now()

    def fee_rate(self):return self.taker

    def check_fee(self):
        if time.monotonic()-self.fee_checked<60 and self.maker is not None:return
        self.maker,self.taker=fee_rates(self.api,self.cfg.pair)
        self.fee_checked=time.monotonic()
        self.s['fee_rates']={'maker':self.maker,'taker':self.taker,'at':time.time()}

    def active(self,role=None):
        return [o for o in self.s['orders_v2'].values()
                if o['status'] not in TERMINAL and (role is None or o['role']==role)]

    def check_account(self):
        self._setup()
        self.recover()
        opened=self.api.orders()
        owned={o.get('txid') for o in self.s['orders_v2'].values()}
        if set(opened)-owned:
            raise SafetyError('Andre åpne ordre finnes på Kraken. De blir ikke kansellert av NordBot; avklar dem før videre handel.')
        balances=self.api.account()
        base=float(balances.get(self.meta['base'],0));eur=float(balances.get(self.meta['quote'],0))
        if not all(math.isfinite(x) and x>=0 for x in (base,eur)):
            raise SafetyError('Ugyldige saldoer fra Kraken.')
        if self.s['baseline_base'] is None:
            self.s['baseline_base']=base
            self.store.save(self.s,'baseline_reserved',{'base_qty':base})
        expected=self.s['baseline_base']+self.s['qty']
        if abs(base-expected)>self.step*1.1:
            raise AccountMismatch('Børssaldo avviker fra journalen. Nye ordre blokkeres mens handler avstemmes. Ikke handle samme krypto manuelt samtidig.')
        if eur+.01<self.s['cash']:
            raise AccountMismatch('EUR-saldo er lavere enn botens journal. Avklar handler, gebyrer eller uttak.')
        if callable(getattr(self.api,'extended_balance',None)):
            ext=self.api.extended_balance()
            quote=ext.get(self.meta['quote'],{});asset=ext.get(self.meta['base'],{})
            free_eur=D(quote.get('balance',eur))-D(quote.get('hold_trade',0))
            free_base=D(asset.get('balance',base))-D(asset.get('hold_trade',0))
            if not all(v.is_finite() and v>=0 for v in (free_eur,free_base)):
                raise AccountMismatch('Ugyldig disponibel saldo fra Kraken.')
            self.s['balances']={'total_eur':str(D(eur)),'free_eur':str(free_eur),
                'total_base':str(D(base)),'free_base':str(free_base),'at':self.now()}
        else:
            self.s['balances']={'total_eur':str(eur),'free_eur':str(eur),'total_base':str(base),
                                'free_base':str(base),'at':self.now(),'hold_data':'unavailable'}
        self.s['last_reconcile']=self.now();self.s['reconcile_required']=False
        if self.s.get('last_account_observation')!=self.s['balances']:
            with self.store.db:self.store.db.execute('INSERT INTO account_observations(ts,value) VALUES (?,?)',(self.now(),encode(self.s['balances'])))
            self.s['last_account_observation']=dict(self.s['balances'])

    def _find_order(self,record):
        found=dict(self.api.orders());offset=0
        while True:
            result=self.api.call('ClosedOrders',{'start':int(record['at'])-60,'ofs':offset},True)
            found.update(result['closed']);offset+=len(result['closed'])
            if offset>=result['count'] or not result['closed']:break
            if offset>=1000:raise UnresolvedOrder('For mange ordre til automatisk oppslag. Ingen ordre sendes på nytt.')
        matches=[txid for txid,o in found.items() if o.get('cl_ord_id')==record['id']]
        if len(matches)!=1:
            raise UnresolvedOrder('Ordreutfallet er ukjent. Kun oppslag gjentas; ingen ny kjøps- eller salgsordre sendes.')
        record['txid']=matches[0]
        self.store.save(self.s,'order_found',{'id':record['id'],'txid':record['txid']})

    def _sync_order(self,record):
        if record['status'] in TERMINAL:return
        if not record.get('txid'):self._find_order(record)
        reply=self.api.query(record['txid']);desc=reply['descr']
        pairs={self.cfg.pair,self.key,self.meta.get('altname'),self.meta.get('wsname')}
        if (desc['pair'] not in pairs or desc['type']!=record['payload']['type']
                or desc.get('ordertype')!=record['payload']['ordertype']):
            raise SafetyError('Ordren på Kraken samsvarer ikke med journalen.')
        status=reply['status']
        if status not in {'pending','open','closed','canceled','expired'}:
            raise UnresolvedOrder('Ukjent ordrestatus. Avstemming må fullføres først.')
        if record['role']=='stop' and status in ('open','pending'):
            amendment=record.get('amend')
            if amendment and abs(D(desc['price'])-D(amendment['new']))<=D(self.meta['tick_size'])/2:
                record['payload']['price']=amendment['new'];record['amend']=None
                record['last_amended']=self.now()
            if (abs(D(reply['vol'])-D(record['payload']['volume']))>D(self.step)/2
                    or abs(D(desc['price'])-D(record['payload']['price']))>D(self.meta['tick_size'])/2
                    or desc.get('leverage','none') not in ('none','0',0,None)):
                raise SafetyError('Stop-loss er endret på Kraken og samsvarer ikke med journalen. Kontroller ordren straks.')
        quantity,amount,charge=(D(reply[k]) for k in ('vol_exec','cost','fee'))
        qty,cost,fee=map(float,(quantity,amount,charge))
        if not all(math.isfinite(x) and x>=0 for x in (qty,cost,fee)):
            raise SafetyError('Ugyldige utførelsestall fra Kraken.')
        oldqty,oldcost,oldfee=map(float,record['seen'])
        qdelta,cdelta,fdelta=(new-D(old) for new,old in zip((quantity,amount,charge),record['seen']))
        dq,dc,df=map(float,(qdelta,cdelta,fdelta))
        if qty>float(record['payload']['volume'])+self.step/2 or min(dq,dc,df)<-1e-10:
            raise SafetyError('Utførte mengder eller gebyrer gikk bakover / oversteg ordren.')
        if dq>0 and dc<=0:raise SafetyError('Utført handel mangler kostnad.')
        if record['role']!='entry' and dq>self.s['qty']+self.step/2:
            raise SafetyError('Utført salg overstiger botens beholdning.')
        s=self.s
        if dq or dc or df:
            if record['role']=='entry':
                if oldqty==0 and dq:
                    s['position_id']=record['id']
                    s['entered']=float(reply.get('opentm',record['at']));s['buys']+=1
                    s['protection_unconfirmed_since']=self.now()
                self.ledger.buy(qdelta,cdelta,fdelta,first=bool(oldqty==0 and dq))
                s['entry_price']=s['entry_cost']/s['entry_volume']
                s['peak']=max(s['peak'],s['entry_price'])
                s['entry_stop_price']=(float(record['payload']['close[price]']) if 'close[price]' in record['payload']
                    else s['entry_price']*(1-s.get('entry_stop_fraction',self.cfg.stop)))
            else:
                self.ledger.sell(qdelta,cdelta,fdelta,D(self.step)/2)
                if dq and s['qty']<self.step/2:
                    self.ledger.set(qty=0,basis=0);s['peak']=0.
                    s['exit_reason']=''
            s['trades']+=int(oldqty==0 and dq>0);s['last_trade']=max(s['last_trade'],self.now())
        record['seen']=list(map(str,(quantity,amount,charge)));record['status']=status
        record['last_checked']=self.now()
        if reply.get('trades'):record['trade_ids']=list(reply['trades'])
        # Fill deltas and their acknowledgement share ONE atomic snapshot commit.
        self.store.save(s,'order_update',{'id':record['id'],'txid':record['txid'],'role':record['role'],
                'status':status,'delta_qty':dq,'cost_eur':dc,'fee_eur':df})

    def recover(self):
        self._setup()
        for record in list(self.active('entry')):self._sync_order(record)
        self._discover_children()
        for record in list(self.active()):
            if record['role']!='entry':self._sync_order(record)
        self._finalize_position()

    def collect_fill_details(self):
        # Reporting may fail without interrupting protection/reconciliation.
        try:
            self._sync_fills();self.s.pop('fill_export_error',None)
        except Exception as exc:
            self.s['fill_export_error']=type(exc).__name__

    def _finalize_position(self):
        s=self.s;identity=s.get('position_id')
        if identity and not s['qty'] and not self.active() and s['position_finalized']!=identity:
            if s.get('exit_unprotected_since'):
                s['max_exit_unprotected_seconds']=max(s.get('max_exit_unprotected_seconds',0),self.now()-s['exit_unprotected_since'])
                s['exit_unprotected_since']=0.
            s['loss_streak']=s['loss_streak']+1 if self.ledger.get('position_realized')<0 else 0
            s['position_finalized']=identity
            s['completed_positions'].append({'id':identity,'entered':s['entered'],'closed':self.now(),
                'net_eur':str(self.ledger.get('position_realized')),'cost_eur':str(self.ledger.get('entry_cost'))})
            if s['loss_streak']>=self.cfg.max_losses:
                halts.latch(s,'streak',f'{self.cfg.max_losses} tapende handler på rad',self.now())
            self.store.save(s,'position_closed',s['completed_positions'][-1])

    def _sync_fills(self):
        if not callable(getattr(self.api,'fills',None)):return
        for record in self.s['orders_v2'].values():
            ids=[i for i in record.get('trade_ids',[]) if i not in record.get('fills_saved',[])]
            if not ids:continue
            for offset in range(0,len(ids),20):
                response=self.api.fills(ids[offset:offset+20])
                with self.store.db:
                    for ident in ids[offset:offset+20]:
                        if ident not in response:raise UnresolvedOrder('Mangler detaljert utførelse i børsens svar.')
                        row=response[ident]
                        if row['ordertxid']!=record['txid'] or row['type']!=record['payload']['type']:
                            raise SafetyError('Utførelsen tilhører ikke forventet botordre.')
                        record_exchange_fill(self.store.db,ident,row,self.cfg.pair,'EUR')
                        from telemetry import record as quality_record
                        quality_record(self.store.db,ident,row,record)
                record.setdefault('fills_saved',[]).extend(ids[offset:offset+20])
            self.store.save(self.s)

    def _discover_children(self):
        parents=[r for r in self.s['orders_v2'].values() if r['role']=='entry'
                 and 'close[ordertype]' in r['payload'] and r.get('txid') and not r.get('children_complete')]
        for parent in parents:
            if not D(parent['seen'][0]):
                if parent['status'] in TERMINAL:parent['children_complete']=True
                continue
            found=dict(self.api.orders());offset=0
            while True:
                reply=self.api.call('ClosedOrders',{'start':int(parent['at'])-60,'ofs':offset},True)
                found.update(reply['closed']);offset+=len(reply['closed'])
                if offset>=reply['count'] or not reply['closed']:break
                if offset>=2000:raise UnresolvedOrder('For mange ordre ved oppslag av følgeordre.')
            children={k:v for k,v in found.items() if v.get('refid')==parent['txid']}
            for txid,row in children.items():
                desc=row['descr'];identity='child:'+txid
                if (desc['type']!='sell' or desc['ordertype']!='stop-loss'
                        or desc['pair'] not in (self.cfg.pair,self.key,self.meta.get('altname'),self.meta.get('wsname'))
                        or desc.get('leverage','none') not in ('none','0',0,None)
                        or abs(D(desc['price'])-D(parent['payload']['close[price]']))>D(self.meta['tick_size'])/2):
                    raise SafetyError('Følgeordren stemmer ikke med lagret kjøp og beskyttelse.')
                if identity not in self.s['orders_v2']:
                    self.s['orders_v2'][identity]={'id':identity,'txid':txid,'role':'stop',
                        'payload':{'pair':self.cfg.pair,'type':'sell','ordertype':'stop-loss',
                                   'volume':row['vol'],'price':desc['price'],'timeinforce':'GTC','oflags':'fciq'},
                        'reason':'Betinget beskyttelse','at':row.get('opentm',parent['at']),
                        'status':'unknown','seen':['0','0','0'],'cancel_requested':False,
                        'parent_txid':parent['txid'],'position_id':parent['id']}
                    self.store.save(self.s,'child_found',{'parent':parent['txid'],'txid':txid})
            quantity=sum((D(v['vol']) for v in children.values()),D(0))
            if quantity>D(parent['seen'][0])+D(self.step)/2:
                raise SafetyError('Følgeordrer overstiger kjøpt mengde. Ingen nye salg sendes.')
            if parent['status'] in TERMINAL:
                if abs(quantity-D(parent['seen'][0]))>D(self.step)/2:
                    if self.now()-parent['at']>15:
                        halts.latch(self.s,'protection','Betinget beskyttelse er uavklart',self.now())
                        self.store.save(self.s,'protection_unconfirmed')
                    raise UnresolvedOrder('Kjøp er registrert, men alle følgeordrer er ikke gjenfunnet. Kontroller Kraken; ingen konkurrerende salg sendes.')
                parent['children_complete']=True;self.store.save(self.s)

    def _submit(self,role,payload,reason,now):
        if any(not o.get('txid') for o in self.active()):
            raise UnresolvedOrder('Uavklart ordre sperrer nye ordre.')
        identity=str(uuid.uuid4());payload={**payload,'cl_ord_id':identity}
        record={'id':identity,'txid':None,'role':role,'payload':payload,'reason':reason,
                'at':now,'status':'unknown','seen':[0.,0.,0.],'cancel_requested':False,
                'position_id':self.s.get('position_id'),'decision_quote':self.s.get('order_decision',{})}
        self.s['orders_v2'][identity]=record
        self.store.save(self.s,'intent',record)
        started=time.monotonic()
        try:reply=self.api.call('AddOrder',payload,True)
        except Exception as exc:
            if definite_rejection(exc):
                record['status']='rejected';self.store.save(self.s,'rejected',{'id':identity,'role':role})
            raise
        ids=reply.get('txid',[])
        record['ack_seconds']=time.monotonic()-started
        if len(ids)!=1:raise UnresolvedOrder('Uventet ordrebekreftelse. Ordren må avstemmes før videre handling.')
        record['txid']=ids[0];self.store.save(self.s,'ack',{'id':identity,'txid':ids[0]})
        self._sync_order(record)
        return record

    def _cancel(self,record):
        self._sync_order(record)
        if record['status'] in TERMINAL:return
        record['cancel_requested']=True
        self.store.save(self.s,'cancel_intent',{'txid':record['txid']})
        try:self.api.call('CancelOrder',{'txid':record['txid']},True)
        except APIError as exc:
            if not str(exc).startswith('EOrder:Unknown order'):raise
        # A cancel acknowledgement does not prove that nothing filled in the race.
        self._sync_order(record)
        if record['role']=='stop' and record['status'] in TERMINAL and self.s['qty'] and not self.s.get('exit_unprotected_since'):
            self.s['exit_unprotected_since']=self.now();self.store.save(self.s)

    def request_exit(self,reason,emergency=False):
        self.s['exit_reason']=reason
        if emergency:halts.latch(self.s,'manual','Manuell nødstopp',self.now())
        self.store.save(self.s,'exit_requested',{'reason':reason,'emergency':emergency})

    def release_manual_halt(self):
        self.recover();self.check_account()
        if self.s['qty'] or self.active():raise SafetyError('Nødstopp kan bare frigis når boten er uten beholdning og alle botordre er avsluttet.')
        if not halts.release_manual(self.s):raise SafetyError('Ingen manuell sperre å frigi. Automatiske tapsgrenser beholdes.')
        self.s['exit_reason']=''
        self.store.save(self.s,'manual_halt_released')

    def liquidate(self):
        self.recover()
        if self.active('exit'):return False
        # Do the initial account/book checks while any existing stop is still active.
        self.check_account()
        if self.s['qty'] or self.active('entry'):self.refresh_meta()
        bid,ask=self.api.quote(self.cfg.pair) if self.s['qty'] or self.active('entry') else (None,None)
        # A partially executed entry must stop filling before we can sell its coins.
        for record in list(self.active('entry')):self._cancel(record)
        self.recover()  # Cancellation may have produced additional fills and children.
        if self.active('entry'):return False
        for record in list(self.active('stop')):self._cancel(record)
        self.recover()
        if self.active():return False
        if not self.s['qty']:
            if self.s.get('exit_unprotected_since'):
                self.s['max_exit_unprotected_seconds']=max(self.s.get('max_exit_unprotected_seconds',0),self.now()-self.s['exit_unprotected_since'])
                self.s['exit_unprotected_since']=0.
            self.s['exit_reason']='';self.store.save(self.s);return True
        self.check_account()
        self.refresh_meta()
        # The quote is only used for minimum-cost validation, not a sale-price promise.
        qty=rounded(self.s['qty'],self.step)
        if qty<D(self.meta['ordermin']) or qty*D(bid)<D(self.meta.get('costmin','0')):
            raise SafetyError('Resten er under minsteordre og kan ikke selges av boten. Kontroller Kraken. Ingen ny handel tillates.')
        if D(self.s['balances']['free_base'])+D(self.step)/2<qty:
            raise AccountMismatch('Botens mengde er fortsatt reservert. Ingen konkurrerende salgsordre sendes.')
        self.s['protection_unconfirmed_since']=self.now()
        if not self.s.get('exit_unprotected_since'):self.s['exit_unprotected_since']=self.now()
        self.s['order_decision']={'bid':bid,'ask':ask,'at':self.now()};self.store.save(self.s)
        self._submit('exit',{'pair':self.cfg.pair,'type':'sell','ordertype':'market',
            'volume':str(qty),'oflags':'fciq'},self.s['exit_reason'] or 'Avslutt posisjon',self.now())
        self._finalize_position()
        return self.s['qty']==0 and not self.active()

    def ensure_protection(self):
        if not self.s['qty'] or self.s['exit_reason'] or self.active('exit'):return
        if self.active('entry') and self.cfg.protection!='attached':return
        stops=self.active('stop')
        if stops:
            remaining=sum((D(r['payload']['volume'])-D(r['seen'][0]) for r in stops),D(0))
            if abs(remaining-self.ledger.get('qty'))>D(self.step)/2:
                self.request_exit('Beskyttelsen dekker ikke gjenværende botmengde')
                self.liquidate();return
            if all(r['status']=='open' for r in stops):
                since=self.s['protection_unconfirmed_since']
                if since:
                    self.s['max_protection_confirmation_seconds']=max(self.s.get('max_protection_confirmation_seconds',0),self.now()-since)
                    self.store.save(self.s,'protection_confirmed',{'seconds_since_fill':max(0,self.now()-since),
                        'order_ids':[r['txid'] for r in stops],'qty':str(remaining)})
                self.s['protection_unconfirmed_since']=0.
            return
        parent=self.s['orders_v2'].get(self.s.get('position_id'),{})
        if 'close[ordertype]' in parent.get('payload',{}):
            # Never add a second stop when an attached child could still arrive.
            self.request_exit('Betinget stopp er avsluttet; avklarer resten')
            self.liquidate();return
        # A cancelled/expired stop is a reason to exit, not silently restart trading.
        if any(o['role']=='stop' and o.get('position_id')==self.s.get('position_id') for o in self.s['orders_v2'].values()):
            self.request_exit('Beskyttelsesordren er avsluttet; lukker resten')
            self.liquidate();return
        self.check_account()
        self.refresh_meta()
        qty=rounded(self.s['qty'],self.step)
        price=rounded(self.s['entry_stop_price'] or self.s['entry_price']*(1-self.cfg.stop),self.meta['tick_size'],up=True)
        if qty<D(self.meta['ordermin']) or qty*price<D(self.meta.get('costmin','0')):
            self.request_exit('For liten rest til stop-loss')
            self.liquidate();return
        try:
            self._submit('stop',{'pair':self.cfg.pair,'type':'sell','ordertype':'stop-loss',
                'volume':str(qty),'price':str(price),'timeinforce':'GTC','oflags':'fciq'},'Fast stop-loss hos Kraken',self.now())
        except Exception as exc:
            if definite_rejection(exc):
                self.request_exit('Kraken avviste beskyttelsesordren')
                halts.latch(self.s,'protection','Beskyttelsesordre avvist',self.now());self.store.save(self.s)
                self.liquidate();return
            raise

    def service(self):
        """Run before market/history fetch, also when restarting or reconnecting."""
        self.recover()
        for record in list(self.active('entry')):
            if (record['payload'].get('timeinforce')!='GTD' or self.s['exit_reason'] or self.s['halt']
                    or self.now()>=float(record['payload']['expiretm'])):
                self._cancel(record)
        self.recover()
        if self.active('entry') and self.cfg.entry_method!='POST':
            raise UnresolvedOrder('Kjøpsordren er ikke avsluttet ennå. Venter før videre ordre.')
        if self.s['exit_reason'] or (self.s['halt'] and self.s['qty']):
            if not self.s['exit_reason']:self.request_exit(self.s['halt'])
            self.liquidate();return
        if self.s['qty'] and not self.active('stop'):
            self.ensure_protection()
        else:
            self.check_account();self.ensure_protection()
        self._finalize_position();self.update_state();self.store.save(self.s)
        if getattr(self.api,'simulated',False):self.collect_fill_details()

    def update_state(self):
        if any(not r.get('txid') for r in self.active()):value='ORDER_UNKNOWN'
        elif self.s['exit_reason'] or self.active('exit'):value='EXIT_PENDING'
        elif self.active('entry'):value='ENTRY_PENDING'
        elif self.s['qty'] and self.active('stop') and all(r['status']=='open' for r in self.active('stop')):value='PROTECTED'
        elif self.s['qty']:value='PROTECTION_UNKNOWN'
        elif self.s['halt']:value='FLAT_HALTED'
        else:value='FLAT'
        self.s['execution_state']=value

    def tighten_stops(self,bid):
        """Only tighten standalone stops. Never amend a parent containing close terms."""
        if not self.cfg.amend_stop or self.s['exit_reason'] or self.active('entry'):return
        desired=rounded(D(self.s['peak'])*(1-D(self.cfg.trailing)),self.meta['tick_size'])
        if desired>=D(bid):return
        for record in self.active('stop'):
            if record['status']!='open' or record.get('amend') or record.get('amend_disabled'):continue
            old=D(record['payload']['price'])
            if desired-old<max(D(self.meta['tick_size'])*5,old*D('.001')):continue
            if self.now()-record.get('last_amended',0)<30:continue
            record['amend']={'old':str(old),'new':str(desired),'at':self.now()}
            self.store.save(self.s,'amend_intent',{'txid':record['txid'],'new':str(desired)})
            try:self.api.call('AmendOrder',{'txid':record['txid'],'trigger_price':str(desired)},True)
            except Exception as exc:
                if definite_rejection(exc):
                    record['amend']=None;record['amend_disabled']=True
                    self.store.save(self.s,'amend_rejected',{'txid':record['txid']})
                    continue  # The old stop remains; local trailing still runs.
                raise
            self._sync_order(record)

    def monitor(self,bid,ask,now=None,liquidate=False):
        result=super().monitor(bid,ask,self.now() if now is None else now,liquidate)
        if self.s['qty'] and not result:self.tighten_stops(bid)
        self.update_state()
        return result

    def trade(self,side,bid,ask,reason,now):
        if side=='sell':
            self.request_exit(reason);self.liquidate();return
        self.service()
        if self.s['halt'] or self.s['qty'] or self.s['exit_reason'] or self.active():
            raise SafetyError('Kjøp er blokkert av beholdning, tapsgrense eller uavklart ordre.')
        self.check_fee()
        if self.cfg.target<=2*(self.taker+self.cfg.slippage)+.005:
            raise SkipEntry('Kursmålet er for lavt i forhold til kontoens aktuelle gebyrer.')
        self.refresh_meta();self.check_account();bid,ask=self.api.quote(self.cfg.pair)
        self.s['order_decision']={'bid':bid,'ask':ask,'at':self.now()}
        if (ask-bid)/bid>self.cfg.spread_max:raise SkipEntry('Hopper over kjøp: spread er for stor.')
        qty,price=self.entry_size(bid,ask)
        if callable(getattr(self.api,'depth',None)) and self.cfg.entry_method!='POST':
            from marketdata import estimate_execution
            depth=estimate_execution(self.api.depth(self.cfg.pair,25),'buy',qty,price)
            self.s['execution_estimate']=depth
            if D(depth['filled_qty'])<D(self.meta['ordermin']):
                raise SkipEntry('For lite synlig volum innenfor prisgrensen.')
            if self.cfg.entry_method=='FOK' and D(depth['unfilled_qty'])>0:
                raise SkipEntry('FOK krever nok synlig volum for hele kjøpet.')
        if qty*price*(1+D(self.fee_rate()))>D(self.s['balances']['free_eur']):
            raise SkipEntry('Hopper over kjøp: for lite disponibel EUR etter reserverte ordre.')
        if not getattr(self,'entry_guard',lambda:True)():raise SkipEntry('Nye kjøp er stanset.')
        payload={'pair':self.cfg.pair,'type':'buy','ordertype':'limit',
            'price':str(price),'volume':str(qty),'timeinforce':self.cfg.entry_method,'oflags':'fciq',
            'deadline':datetime.fromtimestamp(self.now()+10,timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')}
        if self.cfg.entry_method=='POST':
            payload.update(timeinforce='GTD',oflags='post,fciq',expiretm=str(int(self.now()+self.cfg.maker_lifetime)))
        if self.cfg.protection=='attached':
            payload.update({'close[ordertype]':'stop-loss','close[price]':self.s['planned_stop_price'],'trigger':'last'})
        self._submit('entry',payload,reason,now)
        self.service()

    def protection_status(self):
        unknown=[o for o in self.active() if not o.get('txid')]
        if unknown:return 'UAVKLART ORDRE — klient-ID: '+', '.join(o['id'] for o in unknown)
        if self.s['exit_reason']:return 'Avslutter posisjon — kontroller bekreftelse'
        stops=self.active('stop')
        if self.s['qty'] and stops and all(r['status']=='open' for r in stops):
            if abs(sum(D(r['payload']['volume'])-D(r['seen'][0]) for r in stops)-self.ledger.get('qty'))<=D(self.step)/2:
                venue='simulatoren' if self.cfg.mode=='paper' else 'Kraken'
                return f'Stop-loss bekreftet hos {venue}: '+', '.join(r['txid'] for r in stops)+' (siste kontroll)'
        if self.s['qty']:return 'STOP-LOSS IKKE BEKREFTET — '+', '.join(r['txid']+': '+r['status'] for r in stops)+'; kontroller Kraken'
        return 'Ingen botbeholdning'

    def snapshot(self,bid,ask,reason='',rows=None):
        result=super().snapshot(bid,ask,reason,rows)
        result.update(protection=self.protection_status(),maker=self.maker,taker=self.taker,
                      estimated_risk=self.s['estimated_risk'],loss_streak=self.s['loss_streak'],
                      execution_state=self.s['execution_state'],balances=self.s.get('balances',{}))
        return result

    def tick(self,rows,bid,ask,now=None,liquidate=False,entry_allowed=True):
        self.service()
        result=super().tick(rows,bid,ask,now,liquidate,entry_allowed)
        result.update(protection=self.protection_status(),maker=self.maker,taker=self.taker,
                      estimated_risk=self.s['estimated_risk'],loss_streak=self.s['loss_streak'])
        if self.s['halt']:result['reason']=self.s['halt']
        return result

"""Conservative local request budgets, separate from Kraken's actual counters.

Never retries a mutation. BeforeSend means no request reached the network.
The emergency reserve reduces contention; it cannot guarantee exchange capacity.
"""
import threading
import time


class BeforeSend(Exception):pass


class RateGovernor:
    def __init__(self,clock=time.monotonic):
        self.clock=clock;self.at=clock();self.rest=0.;self.trading=0.;self.lock=threading.Lock()
        self.blocked=0;self.observed_trading=None

    def acquire(self,method,data):
        with self.lock:
            now=self.clock();elapsed=max(0,now-self.at);self.at=now
            self.rest=max(0,self.rest-elapsed*.5);self.trading=max(0,self.trading-elapsed)
            mutation=method in ('AddOrder','CancelOrder','AmendOrder')
            entry=method=='AddOrder' and data.get('type')=='buy'
            critical=mutation and not entry
            if mutation:
                cost=8 if method in ('CancelOrder','AmendOrder') else 1
                cap=40 if critical else 12
                if self.trading+cost>cap:
                    self.blocked+=1;raise BeforeSend('Lokal ordrekvote brukt. Forespørselen ble ikke sendt.')
                self.trading+=cost
            else:
                cost=2 if method in ('ClosedOrders','TradesHistory','Ledgers') else 1
                # Low-priority exports/token/fee queries leave room for reconciliation.
                cap=10 if method in ('QueryTrades','TradeVolume','GetWebSocketsToken') else 18
                if self.rest+cost>cap:
                    self.blocked+=1;raise BeforeSend('Lokal API-kvote brukt. Ny avstemming venter på kapasitet.')
                self.rest+=cost

    def observe(self,value):
        with self.lock:
            self.observed_trading=float(value)
            self.trading=max(self.trading,float(value))

    def status(self):
        with self.lock:return {'rest_estimate':round(self.rest,2),'trading_estimate':round(self.trading,2),
            'last_exchange_counter':self.observed_trading,'deferred_before_send':self.blocked}

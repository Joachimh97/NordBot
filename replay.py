"""Exploratory replay. Decisions on completed candles; fills on next candle open.
No intrabar simulation: stops only checked on each open. Not a profitability validation.
"""
import json, tempfile, time
from dataclasses import asdict
from pathlib import Path
from core import Kraken, Config, Store, Engine

class ReplayAPI:
    def __init__(self,key,meta):self.key,self.metadata=key,meta
    def meta(self,pair):return self.key,self.metadata

def run_replay(cfg,rows,key,meta,folder):
    if len(rows)<100:raise ValueError('Minst 100 perioder er nødvendig.')
    cfg=Config(**{**asdict(cfg),'mode':'paper'})
    db=Store(folder/'replay.sqlite'); engine=Engine(cfg,db,ReplayAPI(key,meta))
    high=cfg.capital;drawdown=0;observations=0
    try:
        for i in range(60,len(rows)):
            current=rows[i];mid=float(current[1]);bid=mid*.9995;ask=mid*1.0005
            result=engine.tick(rows[:i],bid,ask,now=int(current[0]))
            high=max(high,result['equity']);drawdown=max(drawdown,1-result['equity']/high);observations+=1
        # Mark final holdings at last closed candle's close. No simulated closing trade.
        bid=float(rows[-1][4])*.9995
        final=engine.equity(bid)
        hold=cfg.capital/(engine.s['first_price']*(1+cfg.fee))*bid*(1-cfg.fee)*(1-cfg.slippage)
        high=max(high,final);drawdown=max(drawdown,1-final/high)
        report={'bars':observations,'days':observations/96,'final_eur':final,'pnl_eur':final-cfg.capital,
                'buy_hold_pnl_eur':hold-cfg.capital,'max_drawdown_pct':drawdown*100,
                'filled_orders':engine.s['trades'],'fees_eur':engine.s['fees'],'open_qty':engine.s['qty'],
                'config':asdict(cfg),'limitations':['Short recent sample; not out-of-sample validation.',
                'Fills modeled at next open, 0.1% synthetic spread plus configured slippage and fees.',
                'Stops evaluated only at bar opens; no intrabar, depth or partial-fill simulation.',
                'Unrealized position marked net of estimated exit costs. No tax, FX or hosting costs.']}
        db.export(folder/'replay-trades.csv')
        (folder/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        return report
    finally:db.close()

def recent_test(cfg,root):
    api=Kraken();key,meta=api.meta(cfg.pair)
    r=api.call('OHLC',{'pair':cfg.pair,'interval':15});rows=next(v for k,v in r.items() if k!='last')[:-1]
    folder=root/('historikktest-'+str(time.time_ns()));folder.mkdir()
    (folder/'candles.json').write_text(json.dumps(rows),encoding='utf-8')
    r=run_replay(cfg,rows,key,meta,folder)
    return (f"Testperiode etter oppvarming: {r['days']:.1f} døgn.\n"
            f"Bot: €{r['pnl_eur']:+.2f}. Kjøp-og-hold: €{r['buy_hold_pnl_eur']:+.2f}.\n"
            f"Utførte ordre: {r['filled_orders']}. Gebyrer: €{r['fees_eur']:.2f}.\n"
            f"Største målte verdifall: {r['max_drawdown_pct']:.2f} %.\n\n"
            "Kort, forenklet test. Stop-loss sjekkes bare ved periodeåpning.\n"
            "Denne testen dokumenterer ikke fremtidig lønnsomhet.\n\nFiler: "+str(folder))

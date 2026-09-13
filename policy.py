"""Shared, deterministic decisions for trading, historical replay and learning labels."""
import math
from dataclasses import asdict
from core import ema, signal, D, SkipEntry


def atr(rows, period=14):
    if len(rows) < period+1: raise ValueError('For lite historikk til ATR.')
    previous=float(rows[0][4]); ranges=[]
    for row in rows[1:]:
        high,low,close=map(float,(row[2],row[3],row[4]))
        ranges.append(max(high-low,abs(high-previous),abs(low-previous)))
        previous=close
    value=sum(ranges[:period])/period
    for item in ranges[period:]: value+=(item-value)/period
    return value


def decision(rows, cfg):
    closes=[float(r[4]) for r in rows]
    if len(closes)<60:return False,False,'Venter på 60 avsluttede prisperioder'
    if cfg.strategy=='ema_rebound':
        buy,exit_signal=signal(rows)
        fast,slow=ema(closes,20),ema(closes,50)
        rules=[(fast[-1]>slow[-1],'EMA20 må være over EMA50'),
               (slow[-1]>slow[-5],'EMA50 må stige'),
               (closes[-2]<=fast[-2] and closes[-1]>fast[-1],'Venter på rekyl over EMA20')]
    else:
        fast,slow=ema(closes,9),ema(closes,21)
        buy=fast[-2]<=slow[-2] and fast[-1]>slow[-1] and slow[-1]>slow[-3]
        exit_signal=fast[-1]<slow[-1]
        rules=[(buy,'Venter på EMA9/21-kryss med stigende trend')]
    volatility=atr(rows,cfg.atr_period)
    if cfg.regime_filter and (volatility/closes[-1]>cfg.max_atr_fraction or
                             abs(fast[-1]-slow[-1])<volatility*.2):
        return False,exit_signal,'Markedsfilter: for stor volatilitet eller for svak trend'
    return buy,exit_signal,('Strategisignal er oppfylt' if buy else next((text for ok,text in rules if not ok),'Venter på signal'))


def cost_plan(cfg, rows, bid, ask, taker, maker=None):
    """Payoff at the configured exits; deliberately NOT a return forecast."""
    a=atr(rows,cfg.atr_period)
    stop=cfg.stop;target=cfg.target
    if cfg.risk_model=='atr':
        stop=max(cfg.min_stop,min(cfg.max_stop,cfg.atr_stop*a/ask))
        target=min(.30,cfg.atr_target*a/ask)
    buy_fee=maker if cfg.entry_method=='POST' and maker is not None else taker
    buy=D(bid if cfg.entry_method=='POST' else ask)*(1+D(0 if cfg.entry_method=='POST' else cfg.slippage))
    buy_cost=buy*(1+D(buy_fee))
    win=buy*(1+D(target))*(1-D(cfg.slippage))*(1-D(taker))/buy_cost-1
    loss=1-buy*(1-D(stop))*(1-D(cfg.slippage))*(1-D(taker))/buy_cost
    roundtrip=(1+D(buy_fee))/((1-D(taker))*(1-D(cfg.slippage)))-1
    return {'stop_fraction':stop,'target_fraction':target,'atr':a,
            'net_target':float(win),'estimated_loss_fraction':float(loss),
            'reward_risk':float(win/loss) if loss>0 else 0.,
            'break_even_win_rate':float(loss/(loss+win)) if loss+win>0 and win>0 else None,
            'cost_hurdle':float(roundtrip),'bid':bid,'ask':ask,
            'qualification':'Regneeksempel ved stopp/mål, ikke forventet avkastning'}


def validate_entry_plan(plan,cfg):
    if plan['net_target']<cfg.min_net_reward:
        raise SkipEntry('Hopper over kjøp: netto gevinst ved kursmålet er for liten etter kostnader.')
    if plan['reward_risk']<cfg.min_reward_risk:
        raise SkipEntry('Hopper over kjøp: forholdet mellom netto gevinst og beregnet tap er for lavt.')


def exit_reason(state,cfg,bid,now,trend_exit=False):
    if not state['qty']:return ''
    entry=state.get('entry_price') or state['basis']/state['qty']
    stop=state.get('entry_stop_price') or entry*(1-state.get('entry_stop_fraction',cfg.stop))
    if state.get('halt'):return state['halt']
    if state.get('exit_reason'):return state['exit_reason']
    if bid<=stop:return 'Stop-loss (lokal)'
    if bid<=state['peak']*(1-cfg.trailing):return 'Trailing stop (lokal)'
    if bid>=entry*(1+state.get('entry_target_fraction',cfg.target)):return 'Kursmål'
    if now-state['entered']>=cfg.max_hold_hours*3600:return f'Maks holdetid {cfg.max_hold_hours} timer'
    if trend_exit:return 'Trend snudde'
    return ''


def model_context(cfg):
    """All decision/cost settings travel with a model; no reuse under different exits."""
    values=asdict(cfg)
    values.pop('mode')
    # GUI numbers are floats, JSON/CLI defaults may be ints. Equal settings must share one model.
    values={k:(str(D(v).normalize()) if isinstance(v,(int,float)) and not isinstance(v,bool) else v) for k,v in values.items()}
    return {'schema':'signal-outcome-v2','config':values,'feature_version':2}

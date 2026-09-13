"""Optional selection-bias diagnostics for precomputed, net period returns.

These statistical assumptions are stronger than a typical crypto backtest can
establish. Results are not probabilities of future profits or live approval.
"""
import itertools
import math
import statistics
from statistics import NormalDist


def sharpe(values):
    if len(values)<3:return 0.
    sd=statistics.stdev(values)
    return statistics.fmean(values)/sd if sd else 0.


def deflated_sharpe(selected,trial_sharpes):
    n=len(selected);trials=len(trial_sharpes)
    if n<30 or trials<3:return {'available':False,'reason':'Trenger minst 30 like perioder og 3 registrerte forsøk.'}
    mean=statistics.fmean(selected);sd=statistics.pstdev(selected)
    if sd==0:return {'available':False,'reason':'Ingen variasjon i perioderesultatene.'}
    skew=statistics.fmean(((v-mean)/sd)**3 for v in selected)
    kurt=statistics.fmean(((v-mean)/sd)**4 for v in selected)
    sr=sharpe(selected);normal=NormalDist();gamma=.5772156649015329
    hurdle=statistics.stdev(trial_sharpes)*((1-gamma)*normal.inv_cdf(1-1/trials)+gamma*normal.inv_cdf(1-1/(trials*math.e)))
    variance=1-skew*sr+(kurt-1)*sr*sr/4
    if variance<=0:return {'available':False,'reason':'Ugyldig momenttilnærming.'}
    return {'available':True,'dsr':normal.cdf((sr-hurdle)*math.sqrt(n-1)/math.sqrt(variance)),
        'observations':n,'registered_trials':trials,'sharpe_per_period':sr,'selection_hurdle':hurdle,
        'assumptions':'Moment approximation; uses all registered trials as effective count. Autocorrelation and correlated searches can invalidate inference.'}


def probability_backtest_overfit(matrix,segments=6):
    if segments not in (4,6,8,10):raise ValueError('Velg 4, 6, 8 eller 10 segmenter.')
    if len(matrix)<segments*10 or not matrix or len(matrix[0])<3:
        return {'available':False,'reason':'For få perioder eller kandidater for CSCV.'}
    m=len(matrix[0])
    if any(len(row)!=m or not all(math.isfinite(v) for v in row) for row in matrix):raise ValueError('Avkastningsmatrisen må være rektangulær og endelig.')
    width=len(matrix)//segments;used=matrix[:width*segments];ranks=[];ties=0
    for selection in itertools.combinations(range(segments),segments//2):
        selected=set(selection)
        ins=[row for i,row in enumerate(used) if i//width in selected]
        outs=[row for i,row in enumerate(used) if i//width not in selected]
        ins_scores=[sharpe([r[j] for r in ins]) for j in range(m)]
        outs_scores=[sharpe([r[j] for r in outs]) for j in range(m)]
        best=max(ins_scores);winners=[i for i,v in enumerate(ins_scores) if abs(v-best)<1e-12]
        if len(winners)>1:ties+=1
        # Average tied in-sample winners' midranks rather than selecting a lucky column.
        rank=[]
        for winner in winners:
            value=outs_scores[winner]
            below=sum(v<value-1e-12 for v in outs_scores);equal=sum(abs(v-value)<1e-12 for v in outs_scores)
            rank.append((below+(equal+1)/2)/(m+1))
        ranks.append(statistics.fmean(rank))
    return {'available':True,'pbo':statistics.fmean(v<.5 for v in ranks),'splits':len(ranks),'ties':ties,
        'relative_oos_ranks':ranks,'ignored_tail_periods':len(matrix)-len(used),
        'assumptions':'CSCV of fixed net-return columns, equal time blocks and Sharpe selection. Not a causal forward test; no automatic parameter selection.'}

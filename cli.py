"""Offline NordBot tools. This command has no live-order subcommand."""
import argparse
import csv
import json
import math
import multiprocessing
import os
import sys
from dataclasses import asdict,replace
from pathlib import Path
from core import Config,Store,SafetyError
from persistence import encode,backup_database,restore_database,export_fills


def config(path=None):
    values=json.loads(Path(path).read_text(encoding='utf-8-sig')) if path else {}
    if values.get('mode','paper')!='paper':raise SafetyError('Kommandolinjeverktøyet tillater bare papirforsøk.')
    cfg=Config(**values);cfg.validate();return cfg


def _annotate_job(output,archive,model,limit):
    try:
        from news_archive import annotate_local_finbert
        output.put({'result':annotate_local_finbert(archive,model,limit)})
    except Exception as exc:output.put({'error':type(exc).__name__+': '+str(exc)})


def read_returns(path):
    """Reject misaligned samples before computing search diagnostics."""
    from history import timestamp
    with open(path,encoding='utf-8-sig',newline='') as file:
        reader=csv.reader(file);names=next(reader,[]);matrix=[];previous=step=None
        if len(names)<4 or len(set(names))!=len(names) or any(not x.strip() for x in names):
            raise ValueError('Forventet tidskolonne og minst tre unike strategikolonner.')
        for line,row in enumerate(reader,2):
            if len(row)!=len(names):raise ValueError(f'CSV-linje {line}: ulikt antall kolonner.')
            at=timestamp(row[0]);values=list(map(float,row[1:]))
            if not all(math.isfinite(x) for x in values):raise ValueError(f'CSV-linje {line}: ugyldig avkastning.')
            if previous is not None:
                delta=at-previous
                if delta<=0 or (step is not None and delta!=step):
                    raise ValueError('Avkastning må være kronologisk og ha like lange perioder uten hull.')
                step=delta
            previous=at;matrix.append(values)
    if not matrix:raise ValueError('Filen mangler avkastningsrader.')
    return names,matrix


def main(argv=None):
    parser=argparse.ArgumentParser(description='NordBot 1.3 forskningsverktøy. Ingen ekte ordre.')
    parser.add_argument('--data-dir',type=Path,default=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'.local/share')))/'NordBot')
    parser.add_argument('--config',help='JSON med innstillinger; kun paper tillates')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('default-config');sub.add_parser('datasets')
    imp=sub.add_parser('import-csv');imp.add_argument('file');imp.add_argument('--pair',required=True,choices=['XBTEUR','ETHEUR']);imp.add_argument('--interval',type=int,default=1,choices=[1,5,15]);imp.add_argument('--member')
    replay=sub.add_parser('compare');replay.add_argument('dataset')
    ev=sub.add_parser('replay-events');ev.add_argument('files',nargs='+');ev.add_argument('--model-id')
    for cmd in ('backup','restore'):
        p=sub.add_parser(cmd);p.add_argument('source');p.add_argument('destination')
    fills=sub.add_parser('export-fills');fills.add_argument('journal');fills.add_argument('destination');fills.add_argument('--fx-csv')
    sub.add_parser('models');sub.add_parser('evaluate-learning')
    freeze=sub.add_parser('freeze');freeze.add_argument('--kind',choices=['prices','news'],default='news')
    select=sub.add_parser('select-paper');select.add_argument('model_id')
    evaluate=sub.add_parser('evaluate-frozen');evaluate.add_argument('model_id')
    final=sub.add_parser('final-holdout');final.add_argument('--kind',choices=['prices','news'],required=True)
    annotate=sub.add_parser('annotate-finbert');annotate.add_argument('model_folder');annotate.add_argument('--limit',type=int,default=100);annotate.add_argument('--timeout',type=int,default=300)
    search=sub.add_parser('diagnose-search');search.add_argument('returns_csv');search.add_argument('--segments',type=int,default=6)
    args=parser.parse_args(argv);root=args.data_dir;root.mkdir(parents=True,exist_ok=True);cfg=config(args.config)
    if args.command=='default-config':return asdict(Config())
    if args.command in ('datasets','import-csv','compare'):
        from history import History
        from evaluation import compare_candles
        history=History(root/'history.sqlite')
        try:
            if args.command=='datasets':return history.datasets()
            if args.command=='import-csv':return history.import_file(args.file,args.pair,args.interval,args.member)
            info=history.info(args.dataset)
            if info['pair']!=cfg.pair:raise ValueError('Konfigurasjonen og datasettet har ulikt marked.')
            path,_=compare_candles(cfg,list(history.rows(args.dataset)),info['interval'],root/'evaluering',info)
            return {'report':str(path)}
        finally:history.close()
    if args.command in ('backup','restore'):
        return {'file':str((backup_database if args.command=='backup' else restore_database)(args.source,args.destination))}
    if args.command=='export-fills':
        st=Store(args.journal)
        try:return export_fills(st.db,args.destination,args.fx_csv)
        finally:st.close()
    if args.command=='replay-events':
        from evaluation import event_replay,register_run
        from learning import FrozenFilter
        filt=FrozenFilter(root/'learning-v13.sqlite',root/'news-v13.sqlite',cfg,args.model_id) if args.model_id else None
        try:result=event_replay(cfg,args.files,entry_filter=filt)
        finally:
            if filt:filt.close()
        return {'report':str(register_run(root/'evaluering','event-replay',{'files':args.files,'model_id':args.model_id,'config':asdict(cfg)},result))}
    if args.command=='annotate-finbert':
        if not 10<=args.timeout<=3600:raise ValueError('Timeout må være mellom 10 og 3600 sekunder.')
        ctx=multiprocessing.get_context('spawn');out=ctx.Queue();process=ctx.Process(target=_annotate_job,args=(out,str(root/'news-v13.sqlite'),args.model_folder,args.limit))
        process.start();process.join(args.timeout)
        if process.is_alive():process.terminate();process.join(5);raise TimeoutError('Modelljobben nådde tidsbudsjettet. Tidligere ferdige annotasjoner er bevart.')
        try:result=out.get(timeout=2)
        except Exception:raise RuntimeError('Modellprosessen avsluttet uten resultat.')
        if 'error' in result:raise RuntimeError(result['error'])
        return result['result']
    if args.command=='diagnose-search':
        from search_diagnostics import sharpe,deflated_sharpe,probability_backtest_overfit
        from evaluation import register_run
        names,matrix=read_returns(args.returns_csv)
        scores=[sharpe([row[i] for row in matrix]) for i in range(len(names)-1)]
        best=max(range(len(scores)),key=lambda i:scores[i]);selected=[r[best] for r in matrix]
        result={'selected_column':names[best+1],'dsr':deflated_sharpe(selected,scores),'pbo':probability_backtest_overfit(matrix,args.segments)}
        return {'report':str(register_run(root/'evaluering','search-diagnostics',{'file':args.returns_csv,'columns':names},result))}
    from learning import SignalLab
    lab=SignalLab(root/'learning-v13.sqlite',cfg)
    try:
        if args.command=='models':return lab.versions()
        if args.command=='freeze':return {'model_id':lab.freeze(args.kind),'automatically_selected':False}
        if args.command=='select-paper':lab.select_paper(args.model_id);return {'selected':args.model_id,'scope':'paper only'}
        if args.command=='evaluate-learning':return {'report':str(lab.evaluate(root/'evaluering'))}
        if args.command=='evaluate-frozen':return {'report':str(lab.evaluate_frozen(args.model_id,root/'evaluering'))}
        if args.command=='final-holdout':return {'report':str(lab.final_holdout(args.kind,root/'evaluering'))}
    finally:lab.close()


if __name__=='__main__':
    multiprocessing.freeze_support()
    try:print(encode(main()))
    except Exception as exc:print(type(exc).__name__+': '+str(exc),file=sys.stderr);sys.exit(1)

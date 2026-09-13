"""Desktop entry points for offline research, backups and exports."""
import json
import threading
import time
import webbrowser
from dataclasses import asdict,replace
from pathlib import Path
import tkinter as tk
from tkinter import ttk,messagebox,filedialog,simpledialog
from core import Config,Store
from persistence import encode,backup_database,restore_database,export_fills


class Workbench:
    def folder(self):
        # Import dynamically so tests and installations can choose a data directory.
        import app
        return app.ROOT
    def task(self,action):
        if self.running:return
        self.stop.clear();self.trading_worker=False;self.controls(True)
        def run():
            try:
                result=action()
                if result:self.q.put(('log',str(result)))
            except Exception as exc:self.q.put(('error',type(exc).__name__+': '+str(exc)))
            finally:self.q.put(('done',None))
        self.thread=threading.Thread(target=run,daemon=False);self.thread.start()
    def import_history(self):
        if self.running:return
        file=filedialog.askopenfilename(title='Velg OHLCVT-CSV for markedet i NordBot',filetypes=[('CSV/ZIP','*.csv *.zip')])
        if not file:return
        interval=simpledialog.askinteger('Oppløsning','Hvor mange minutter inneholder hver rad i kildefilen? (1, 5 eller 15)',initialvalue=1,minvalue=1,maxvalue=15)
        if interval is None:return
        pair=self.vars['pair'].get();folder=self.folder()
        if not messagebox.askyesno('Kontroller historikkmarked',f'Bekreft at filen gjelder {pair}, med {interval} minutter per rad. CSV-filer har ofte ikke markedet skrevet inne i filen.'):
            return
        def work():
            from history import History
            history=History(folder/'history.sqlite')
            try:info=history.import_file(file,pair,interval)
            finally:history.close()
            self.q.put(('dataset',info))
            return f"Importert {info['count']:,} perioder. Manglende perioder: {info['missing_intervals']}. Ingen hull fylt."
        self.task(work)
    def compare(self):
        if self.running:return
        if not self.dataset:
            from history import History
            h=History(self.folder()/'history.sqlite')
            try:items=[d for d in h.datasets() if d['pair']==self.vars['pair'].get()]
            finally:h.close()
            if len(items)==1:self.dataset=items[0]
            elif items:
                messagebox.showinfo('Velg datasett','Importer ønsket fil på nytt for å velge den. Importen gjenbrukes hvis filen allerede finnes.');return
            else:messagebox.showinfo('Historikk mangler','Bruk Importer historikk først. Se Brukerveiledning for Krakens OHLCVT-filer.');return
        try:c=replace(self.cfg(),mode='paper')
        except Exception as exc:messagebox.showerror('Innstillinger',str(exc));return
        data=dict(self.dataset);folder=self.folder()
        if data['pair']!=c.pair:messagebox.showerror('Feil marked','Velg eller importer et datasett for samme marked.');return
        def work():
            from history import History
            from evaluation import compare_candles
            h=History(folder/'history.sqlite')
            try:rows=list(h.rows(data['id']))
            finally:h.close()
            output,_=compare_candles(c,rows,data['interval'],folder/'evaluering',data,self.stop)
            self.q.put(('report',str(output)))
            return 'Sammenligning lagret: '+str(output)
        self.task(work)
    def backup(self):
        if not self.path().exists():messagebox.showinfo('Ingen økt','Start en papirøkt først.');return
        source=self.path();target=self.folder()/'sikkerhetskopier'/f'{time.time_ns()}-{source.name}'
        target.parent.mkdir(exist_ok=True)
        try:
            backup_database(source,target)
            messagebox.showinfo('Sikkerhetskopi laget',str(target)+'\nBørsens tilstand kan endres etter kopien. Gjenoppretting krever avstemming.')
        except Exception as exc:messagebox.showerror('Kopiering feilet',str(exc))
    def restore(self):
        if self.running:return
        source=filedialog.askopenfilename(title='Velg SQLite-sikkerhetskopi',filetypes=[('SQLite','*.sqlite')])
        if not source:return
        folder=self.folder()/'gjenopprettet';folder.mkdir(exist_ok=True)
        target=folder/f'{time.time_ns()}-{Path(source).name}'
        try:
            restore_database(source,target)
            messagebox.showinfo('Kopi gjenopprettet',f'{target}\n\nIngen økt er overskrevet og ingen handel er startet. Bruk Åpne journal for å velge kopien; ordre må avstemmes før nye kjøp.')
        except Exception as exc:messagebox.showerror('Gjenoppretting feilet',str(exc))
    def choose_session(self):
        if self.running:return
        source=filedialog.askopenfilename(title='Velg NordBot 1.3 eller kompatibel live-journal',filetypes=[('SQLite','*.sqlite')])
        if not source:return
        try:
            st=Store(source)
            try:state=st.load()
            finally:st.close()
            if not state or 'config' not in state:raise ValueError('Filen inneholder ikke en NordBot-økt.')
            cfg=Config(**{**asdict(Config()),**state['config']});cfg.validate()
            if cfg.mode=='paper' and state.get('execution_schema')!=2:
                raise ValueError('Gamle papirøkter er bevart som historikk. Start ny 1.3-papirøkt for den nye simulatoren.')
            self.vars['mode'].set(cfg.mode);self.mode_changed();self.session_override=Path(source)
            self.load_existing();self.status.set('Valgt journal: '+str(source)+'. Ingen automatisk start.')
        except Exception as exc:messagebox.showerror('Journalen kunne ikke åpnes',str(exc))
    def export_transactions(self):
        if not self.path().exists():messagebox.showinfo('Ingen utførelser','Start en økt først.');return
        fx=filedialog.askopenfilename(title='Valgfri EUR/NOK-CSV (Avbryt gir kun EUR)',filetypes=[('CSV','*.csv')])
        target=self.folder()/f'utforelser-{time.time_ns()}.csv';st=Store(self.path())
        try:
            info=export_fills(st.db,target,fx or None)
            from telemetry import export as export_quality
            export_quality(st.db,target.with_name(target.stem+'-utforelseskvalitet.csv'))
            messagebox.showinfo('Utførelser eksportert',f"{target}\nMangler valutareferanse: {info['missing_fx']} utførelser.\nDette er botens utførelser, ikke ferdig skattemelding eller full kontohistorikk.")
        except Exception as exc:messagebox.showerror('Eksport feilet',str(exc))
        finally:st.close()
    def diagnostics(self):
        target=self.folder()/f'diagnostikk-{time.time_ns()}.json'
        data={'version':'1.3','created':time.time(),'last_view':self.last,'health':self.last_health}
        # Infinity is represented as unknown, not invalid JSON.
        if data['health'].get('age')==float('inf'):data['health']['age']=None
        try:target.write_text(encode(data),encoding='utf-8');messagebox.showinfo('Diagnostikk lagret',str(target)+'\nKontroller kontoopplysninger og ordre-ID-er før du deler filen. API-nøkler inngår ikke.')
        except Exception as exc:messagebox.showerror('Eksport feilet',str(exc))
    def models(self):
        if self.running:messagebox.showinfo('Pause først','Pause papirøkten før du velger modell eller starter evaluering.');return
        from learning import SignalLab
        try:c=replace(self.cfg(),mode='paper')
        except Exception as exc:messagebox.showerror('Innstillinger',str(exc));return
        window=tk.Toplevel(self.root);window.title('Fryste modellversjoner — kun papir');window.geometry('820x420')
        ttk.Label(window,text='Kandidaten lærer i bakgrunnen. En fryst kopi beholder de samme vektene.\nPapirvalg er et forsøk; ingen modell i 1.3 kvalifiserer automatisk til live.',wraplength=760).pack(padx=16,pady=12)
        listing=tk.Listbox(window,width=100,height=10);listing.pack(fill='both',expand=True,padx=16);versions=[]
        def refresh():
            lab=SignalLab(self.folder()/'learning-v13.sqlite',c)
            try:versions[:]=lab.versions()
            finally:lab.close()
            listing.delete(0,'end')
            for m in versions:listing.insert('end',f"{m['id']} | {m['kind']} | {m['training_n']} utfall | {m['role']}")
        def operate(action):
            lab=SignalLab(self.folder()/'learning-v13.sqlite',c)
            try:result=action(lab)
            except Exception as exc:messagebox.showerror('Modellverktøy',str(exc));return
            finally:lab.close()
            refresh()
            if result:messagebox.showinfo('Modellverktøy',str(result))
        def selected():
            ids=listing.curselection()
            if not ids:raise ValueError('Velg en modell i listen først.')
            return versions[ids[0]]['id']
        actions=ttk.Frame(window);actions.pack(fill='x',padx=12,pady=12)
        for label,action in [('Frys pris + nyheter',lambda lab:lab.freeze('news')),('Frys pris alene',lambda lab:lab.freeze('prices')),
            ('Velg til papirfilter',lambda lab:lab.select_paper(selected())),
            ('Evaluer fremtidige utfall',lambda lab:lab.evaluate_frozen(selected(),self.folder()/'evaluering')),
            ('Kronologisk sammenligning',lambda lab:lab.evaluate(self.folder()/'evaluering'))]:
            ttk.Button(actions,text=label,command=lambda a=action:operate(a)).pack(side='left',padx=2)
        refresh()

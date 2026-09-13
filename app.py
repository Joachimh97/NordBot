"""Norwegian desktop UI. Run with Python 3.11+ (Tk included on Windows)."""
import csv, json, os, queue, socket, threading, time, tkinter as tk, webbrowser
from pathlib import Path
from tkinter import ttk, messagebox, filedialog
from datetime import datetime
from dataclasses import asdict
from core import Config, Engine, Kraken, Store, SafetyError
from execution import ProtectedEngine, preflight, transient
from runtime import Runtime
from workbench import Workbench
from persistence import encode,backup_database,restore_database,export_fills

PERCENT_FIELDS={'fee','maker_fee','slippage','stop','target','risk','rolling_loss','trailing','daily_loss','total_loss','spread_max','min_net_reward'}
CHOICES={'mode':('paper','live'),'pair':('XBTEUR','ETHEUR'),'strategy':('ema_rebound','momentum'),
    'risk_model':('fixed','atr'),'entry_method':('IOC','FOK','POST'),'protection':('separate','attached'),
    'interval':('1','5','15'),'amend_stop':('Av','På'),'regime_filter':('Av','På')}

ROOT=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'.local/share')))/'NordBot'
ROOT.mkdir(parents=True,exist_ok=True)

class ProcessLock:
    def __init__(self):
        self.file=open(ROOT/'instance.lock','a+b')
        self.file.seek(0); self.file.write(b'0'); self.file.flush(); self.file.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            raise SafetyError('NordBot kjører allerede. Bare ett vindu kan bruke samme journal.')

class App(Workbench):
    def __init__(self,root):
        self.root=root; root.title('NordBot 1.3 • Kraken trading'); root.geometry('1160x960'); root.minsize(900,760)
        root.configure(bg='#101923'); self.q=queue.Queue(); self.thread=None; self.stop=threading.Event(); self.sell=threading.Event()
        self.last=None; self.running=False;self.trading_worker=False;self.last_health={};self.dataset=None;self.session_override=None;self.loaded_config={}
        self.wake=threading.Event();self.emergency=threading.Event()
        style=ttk.Style(); style.theme_use('clam')
        style.configure('.',font=('Segoe UI',10),background='#182431',foreground='#e5edf5')
        style.configure('TFrame',background='#101923'); style.configure('TLabel',background='#101923')
        style.configure('TButton',padding=8,background='#264653',foreground='white')
        style.configure('TEntry',fieldbackground='#ffffff',foreground='#14202b')
        style.configure('TCombobox',fieldbackground='#ffffff',foreground='#14202b')
        style.map('TCombobox',fieldbackground=[('readonly','#ffffff')],foreground=[('readonly','#14202b')])
        viewport=tk.Canvas(root,bg='#101923',highlightthickness=0)
        scrollbar=ttk.Scrollbar(root,orient='vertical',command=viewport.yview)
        scrollbar.pack(side='right',fill='y');viewport.pack(side='left',fill='both',expand=True)
        viewport.configure(yscrollcommand=scrollbar.set)
        main=ttk.Frame(viewport,padding=22);window=viewport.create_window((0,0),window=main,anchor='nw')
        main.bind('<Configure>',lambda e:viewport.configure(scrollregion=viewport.bbox('all')))
        viewport.bind('<Configure>',lambda e:viewport.itemconfigure(window,width=e.width))
        root.bind_all('<MouseWheel>',lambda e:viewport.yview_scroll(int(-e.delta/120),'units'))
        ttk.Label(main,text='NORDBOT',font=('Segoe UI',24,'bold'),foreground='#75e6bf').pack(anchor='w')
        ttk.Label(main,text='Kraken spot • én posisjon • ingen belåning',font=('Segoe UI',11)).pack(anchor='w',pady=(0,12))
        self.banner=tk.Label(main,text='PAPIRHANDEL — ekte priser, simulerte penger',bg='#203e38',fg='#a4f3cf',padx=12,pady=10,anchor='w',font=('Segoe UI',11,'bold'))
        self.banner.pack(fill='x')
        form=ttk.Frame(main); form.pack(fill='x',pady=12)
        self.advanced=ttk.Frame(main)
        self.advanced_button=ttk.Button(main,text='Vis avanserte papirforsøk',command=self.toggle_advanced)
        self.advanced_button.pack(anchor='w',pady=(0,6))
        self.vars={}; self.fields=[]
        specs=[('mode','Modus','paper'),('pair','Marked','XBTEUR'),('capital','Budsjett (EUR)','1000'),
               ('order_eur','Maks per kjøp (EUR)','100'),('fee','Papirgebyr per side (%)','0.8'),
               ('slippage','Prisavvik (%)','0.1'),('stop','Stop-loss (%)','2'),('target','Kursmål (%)','4'),
               ('risk','Beregnet maks risiko (%)','0.5'),('rolling_loss','Rullende tapsgrense (%)','3'),
               ('trailing','Lokal trailing (%)','2'),('spread_max','Maks spread (%)','0.2'),
               ('strategy','Strategi (nye: kun papir)','ema_rebound'),('interval','Prisperioder (minutter)','15'),
               ('risk_model','Stopp/mål: fixed eller atr','fixed'),('entry_method','Kjøpsordre: IOC/FOK/POST','IOC'),
               ('protection','Stopp: separate/attached','separate'),('amend_stop','Flytt børsstopp oppover','Av'),
               ('regime_filter','Ekstra trend/volatilitetsfilter','Av'),('maker_fee','Papirgebyr maker (%)','0.4'),
               ('atr_stop','ATR-ganger ved stopp','2'),('atr_target','ATR-ganger ved mål','3'),
               ('min_net_reward','Min. netto ved kursmål (%)','0.5'),('min_reward_risk','Min. netto gevinst/tap-forhold','0')]
        for i,(name,label,value) in enumerate(specs):
            parent=form if i<12 else self.advanced
            row,col=divmod(i if i<12 else i-12,4); box=ttk.Frame(parent); box.grid(row=row,column=col,sticky='ew',padx=(0,12),pady=5); parent.columnconfigure(col,weight=1)
            ttk.Label(box,text=label).pack(anchor='w'); var=tk.StringVar(value=value); self.vars[name]=var
            if name in CHOICES:
                entry=ttk.Combobox(box,textvariable=var,state='readonly',values=CHOICES[name],width=18)
                if name in ('mode','pair'):entry.bind('<<ComboboxSelected>>',self.mode_changed)
            else: entry=ttk.Entry(box,textvariable=var,width=20)
            entry.pack(fill='x'); self.fields.append((entry,name))
        self.keys=ttk.Frame(main); self.keys.pack(fill='x')
        self.key=tk.StringVar(); self.secret=tk.StringVar()
        for label,var in [('API key (kun live)',self.key),('API secret (kun live)',self.secret)]:
            ttk.Label(self.keys,text=label).pack(side='left',padx=(0,5))
            ent=ttk.Entry(self.keys,textvariable=var,show='•',width=28); ent.pack(side='left',padx=(0,12)); self.fields.append((ent,'credential'))
        ttk.Label(main,text='Nøkler lagres ikke på disk. Papirhandel krever verken konto eller API-nøkkel.',foreground='#94a9bb').pack(anchor='w',pady=5)
        self.research_mode=tk.StringVar(value='Observer og lær')
        research_row=ttk.Frame(main);research_row.pack(fill='x',pady=6)
        ttk.Label(research_row,text='Nyheter og læring (kun papir):').pack(side='left',padx=(0,8))
        self.research_choice=ttk.Combobox(research_row,textvariable=self.research_mode,state='readonly',width=25,
            values=('Observer og lær','Filtrer papirkjøp','Av'))
        self.research_choice.pack(side='left');self.research_choice.bind('<<ComboboxSelected>>',lambda e:self.load_existing())
        ttk.Button(research_row,text='Eksporter prognoser',command=self.export_research).pack(side='left',padx=8)
        ttk.Button(research_row,text='Modellversjoner',command=self.models).pack(side='left',padx=4)
        self.research_stats=tk.StringVar(value='Modellen starter uten trening. Ingen dokumentert prognoseevne.')
        ttk.Label(main,textvariable=self.research_stats,wraplength=1000,foreground='#b9d5ef').pack(anchor='w',pady=5)
        self.news_text=tk.Text(main,height=5,bg='#14212e',fg='#c9d7e5',wrap='word',state='disabled')
        self.news_text.pack(fill='x')
        buttons=ttk.Frame(main); buttons.pack(fill='x',pady=8)
        self.start_button=ttk.Button(buttons,text='Start automatisk',command=self.start); self.start_button.pack(side='left',padx=(0,8))
        self.pause_button=ttk.Button(buttons,text='Pause',command=self.pause); self.pause_button.pack(side='left',padx=4)
        self.sell_button=ttk.Button(buttons,text='Selg posisjon og pause',command=self.close_position); self.sell_button.pack(side='left',padx=4)
        self.recover_button=ttk.Button(buttons,text='Avstem ordre',command=lambda:self.start(reconcile=True)); self.recover_button.pack(side='left',padx=4)
        safety_buttons=ttk.Frame(main);safety_buttons.pack(fill='x',pady=(0,6))
        self.check_button=ttk.Button(safety_buttons,text='Test Kraken-tilkobling',command=lambda:self.start(reconcile='check'));self.check_button.pack(side='left',padx=4)
        self.kill_button=ttk.Button(safety_buttons,text='NØDSTOPP — selg botbeholdning',command=self.kill);self.kill_button.pack(side='left',padx=4)
        self.release_button=ttk.Button(safety_buttons,text='Frigi manuell nødstopp',command=self.release);self.release_button.pack(side='left',padx=4)
        second=ttk.Frame(main); second.pack(fill='x',pady=(0,8))
        self.test_button=ttk.Button(second,text='Test nyere historikk',command=self.backtest); self.test_button.pack(side='left',padx=(0,8))
        ttk.Button(second,text='Eksporter logg',command=self.export).pack(side='left',padx=4)
        self.reset_button=ttk.Button(second,text='Arkiver papirøkt',command=self.reset)
        self.reset_button.pack(side='left',padx=4)
        ttk.Button(second,text='Brukerveiledning',command=self.help).pack(side='left',padx=4)
        tools_row=ttk.Frame(main);tools_row.pack(fill='x',pady=(0,8));self.tool_buttons=[]
        for label,action in [('Importer historikk',self.import_history),('Sammenlign strategier',self.compare),
                             ('Sikkerhetskopi',self.backup),('Gjenopprett kopi',self.restore),('Åpne journal',self.choose_session)]:
            button=ttk.Button(tools_row,text=label,command=action);button.pack(side='left',padx=3);self.tool_buttons.append(button)
        tools_row2=ttk.Frame(main);tools_row2.pack(fill='x',pady=(0,6))
        ttk.Button(tools_row2,text='Eksporter utførelser / NOK',command=self.export_transactions).pack(side='left',padx=3)
        ttk.Button(tools_row2,text='Eksporter diagnostikk',command=self.diagnostics).pack(side='left',padx=3)
        self.data_status=tk.StringVar(value='Datakilder, alder og feil vises etter oppstart. WebSocket brukes når tillegget er installert.')
        ttk.Label(main,textvariable=self.data_status,wraplength=1020,foreground='#a8bdd1').pack(anchor='w',pady=6)
        self.stats=tk.StringVar(value='Budsjett og resultater vises her etter oppstart.')
        ttk.Label(main,textvariable=self.stats,font=('Segoe UI',12,'bold'),wraplength=1020).pack(anchor='w',pady=10)
        self.protection=tk.StringVar(value='Papirhandel. Live-gebyrer hentes automatisk fra Kraken.')
        ttk.Label(main,textvariable=self.protection,wraplength=1020,foreground='#ffc488').pack(anchor='w',pady=5)
        self.canvas=tk.Canvas(main,height=160,bg='#14212e',highlightthickness=0); self.canvas.pack(fill='x'); self.canvas.bind('<Configure>',lambda e:self.draw())
        ttk.Label(main,text='Siste 80 avsluttede prisperioder i valgt intervall. Prisgraf, ikke avkastning.',foreground='#94a9bb').pack(anchor='w')
        self.status=tk.StringVar(value='Klar. Start med papirhandel. Ingen dokumentert lønnsomhet.')
        ttk.Label(main,textvariable=self.status,wraplength=1020,foreground='#75e6bf').pack(anchor='w',pady=8)
        self.log=tk.Text(main,height=6,bg='#0b131c',fg='#c9d7e5',font=('Consolas',9),wrap='word',state='disabled'); self.log.pack(fill='both',expand=True)
        ttk.Label(main,text='Live: fast stop-loss legges hos Kraken etter bekreftet kjøp. Sjekk beskyttelsesstatus.\nTrailing stop, kursmål og tapsgrenser krever at boten kjører. Pause selger ikke.\nStopp og nødsalg garanterer ingen bestemt pris. Utviklingsversjon, ikke verifisert med ekte ordre.',foreground='#ffc488',wraplength=1020).pack(anchor='w',pady=(10,0))
        self.load_existing(); self.root.after(200,self.drain); self.root.protocol('WM_DELETE_WINDOW',self.quit)
    def path(self):
        if self.session_override:return self.session_override
        name=self.vars['mode'].get()
        if name=='paper':name='paper-news-v13' if self.research_mode.get()=='Filtrer papirkjøp' else 'paper-v13'
        return ROOT/(name+'.sqlite')
    def toggle_advanced(self):
        if self.advanced.winfo_manager():
            self.advanced.pack_forget();self.advanced_button.configure(text='Vis avanserte papirforsøk')
        else:
            self.advanced.pack(fill='x',before=self.keys,pady=8);self.advanced_button.configure(text='Skjul avanserte papirforsøk')
    def load_existing(self):
        path=self.path()
        if path.exists():
            st=Store(path)
            try:
                state=st.load()
                if state:
                    self.loaded_config=dict(state['config'])
                    for key,var in self.vars.items():
                        v=state['config'].get(key,getattr(Config(),key))
                        var.set(('På' if v else 'Av') if key in ('amend_stop','regime_filter') else str(v*100 if key in PERCENT_FIELDS else v))
                    self.status.set('Lagret økt lastet. Start gjenopptar beholdning og grenser; ingen automatisk start.')
            finally: st.close()
    def mode_changed(self,event=None):
        if self.running:return
        self.session_override=None
        self.loaded_config={}
        mode=self.vars['mode'].get()
        self.banner.config(text='EKTE HANDEL — ordre bruker dine penger' if mode=='live' else 'PAPIRHANDEL — ekte priser, simulerte penger',
                           bg='#572d29' if mode=='live' else '#203e38',fg='#ffd3bc' if mode=='live' else '#a4f3cf')
        if not self.path().exists():
            self.vars['capital'].set('100' if mode=='live' else '1000'); self.vars['order_eur'].set('25' if mode=='live' else '100')
            if mode=='live':
                for k,v in {'strategy':'ema_rebound','risk_model':'fixed','interval':'15','entry_method':'IOC','regime_filter':'Av','protection':'separate','amend_stop':'Av'}.items():self.vars[k].set(v)
        self.load_existing()
        self.research_choice.configure(state='disabled' if mode=='live' else 'readonly')
        if mode=='live':self.research_stats.set('Nyheter og læring er avslått i ekte handel. Den vanlige strategien brukes.')
    def cfg(self):
        vals={k:v.get().strip().replace(',','.') for k,v in self.vars.items()}
        parsed={}
        for k,v in vals.items():
            if k in ('amend_stop','regime_filter'):parsed[k]=v=='På'
            elif k=='interval':parsed[k]=int(v)
            elif k in CHOICES:parsed[k]=v
            else:parsed[k]=float(v)/(100 if k in PERCENT_FIELDS else 1)
        c=Config(**{**self.loaded_config,**parsed})
        c.validate(); return c
    def controls(self,busy):
        self.running=busy
        self.kill_button.configure(state='normal' if not busy or self.trading_worker else 'disabled')
        self.sell_button.configure(state='normal' if busy and self.trading_worker else 'disabled')
        self.research_choice.configure(state='disabled' if busy or self.vars['mode'].get()=='live' else 'readonly')
        for ent,name in self.fields: ent.configure(state='disabled' if busy else ('readonly' if name in CHOICES else 'normal'))
        for b in [self.start_button,self.recover_button,self.reset_button,self.test_button,self.check_button,self.release_button]:b.configure(state='disabled' if busy else 'normal')
        for b in self.tool_buttons:b.configure(state='disabled' if busy else 'normal')
    def start(self,reconcile=False,emergency=False):
        if self.thread and self.thread.is_alive():return
        try:c=self.cfg()
        except Exception as e:messagebox.showerror('Kontroller innstillingene',str(e));return
        if c.mode=='live':
            if not self.key.get().strip() or not self.secret.get().strip():
                messagebox.showerror('API-nøkkel mangler','Opprett en begrenset API-nøkkel på Kraken. Se veiledningen.');return
            if not reconcile and not emergency and not messagebox.askyesno('Aktiver ekte handel?',f'Boten får kjøpe og selge {c.pair} automatisk.\nBudsjett: {c.capital:.2f} EUR. Maks kjøp: {c.order_eur:.2f} EUR.\nBeregnet risiko per handel: høyst {c.risk:.1%}, faktisk tap kan bli større.\n\nFast stop-loss sendes etter bekreftet kjøp, og kan være uavklart ved feil.\nDenne utgaven er ikke verifisert med ekte Kraken-ordre.\n\nVil du starte ekte handel?'):return
        key,secret=(self.key.get().strip(),self.secret.get().strip()) if c.mode=='live' else ('','')
        self.stop.clear();self.sell.clear();self.wake.clear();self.emergency.clear()
        if emergency:self.emergency.set()
        self.trading_worker=not reconcile;self.controls(True)
        self.status.set('Kobler til Kraken og kontrollerer økten …')
        self.thread=threading.Thread(target=self.worker,args=(c,key,secret,reconcile,self.research_mode.get() if c.mode=='paper' else 'Av',self.path()),daemon=False);self.thread.start()
    def worker(self,c,key,secret,reconcile,research_mode='Av',session_path=None):
        Runtime(ROOT,self.q,self.stop,self.sell,self.wake,self.emergency,api_factory=Kraken).run(
            c,key,secret,reconcile,research_mode,session_path)
    def pause(self):
        self.stop.set();self.wake.set()
        self.status.set('Pauser. Selger ikke og kansellerer ikke bekreftet Kraken-stopp. Lokal trailing/kursmål stopper. Kontroller uavklarte ordre.')
    def close_position(self):
        if not self.running:messagebox.showinfo('Start økten først','Start riktig økt først, eller bruk NØDSTOPP for å avslutte lagret botbeholdning.');return
        if messagebox.askyesno('Selg og pause?','Selge bare botens beholdning og pause? Live bruker markedsordre etter avstemt kansellering av botens stopp. Prisen kan avvike.'):
            self.sell.set();self.wake.set();self.status.set('Salg forespurt. Venter på eventuelle pågående API-kall.')
    def kill(self):
        if not messagebox.askyesno('NØDSTOPP','Stans nye kjøp og selg bare botens beholdning? Dette gir en vedvarende handelssperre.\nLive-salg krever kontakt med Kraken; markedsprisen er ikke garantert.'):
            return
        if not self.running:self.start(emergency=True)
        else:
            self.emergency.set();self.wake.set()
            self.status.set('Nødstopp forespurt. Avklarer ordre før salg; vent på bekreftelse.')
    def release(self):
        if self.running:return
        if messagebox.askyesno('Frigi manuell nødstopp?','Kontroller loggen først. Dette frigir bare manuell nødstopp når boten er uten beholdning. Ingen nye kjøp startes, og automatiske tapsgrenser beholdes.'):
            self.start(reconcile='release')
    def drain(self):
        try:
            while True:
                kind,data=self.q.get_nowait()
                if kind=='tick':
                    self.last=data; self.draw()
                    self.stats.set(f"Botverdi: €{data['equity']:.2f}    Resultat: €{data['pnl']:+.2f}    Kjøp-og-hold: €{data['hold']:+.2f}\nBeholdning: {data['qty']:.8f}    EUR ledig: {data['cash']:.2f}    Gebyrer: €{data['fees']:.2f}    Utførte ordre: {data['trades']}")
                    self.status.set(datetime.now().strftime('%H:%M:%S')+' • '+data['reason'])
                    if 'protection' in data:
                        plan=data.get('plan',{});entry=data.get('planned_entry',{})
                        payoff=(f" Siste inngangsplan: utlegg EUR {float(entry.get('outlay_eur',0)):.2f}, beregnet stopptap EUR {float(entry.get('risk_eur',0)):.2f}, "
                            f"netto ved mål EUR {float(entry.get('outlay_eur',0))*plan.get('net_target',0):+.2f}. Dette er mulige utfall ved stopp/mål, ikke en prognose." if plan and entry else '')
                        self.protection.set(data['protection']+f". Maker {data['maker']:.2%}, taker {data['taker']:.2%}."+payoff)
                elif kind=='flat':
                    self.stats.set(f"Botverdi: €{data['cash']:.2f}    Resultat: €{data['cash']-data['capital']:+.2f}\nBeholdning: 0.00000000    EUR ledig: {data['cash']:.2f}    Gebyrer: €{data['fees']:.2f}    Utførte ordre: {data['trades']}")
                elif kind=='protection':self.protection.set(data)
                elif kind=='dataset':self.dataset=data
                elif kind=='report':webbrowser.open(Path(data).resolve().as_uri())
                elif kind=='health':
                    self.last_health={k:data.get(k) for k in ('message','source','age','errors','capture_dropped','capture_error','private_stream','private_stream_error','api_budget','model_error','news_error')}
                    age=data.get('age',float('inf'))
                    errors='; '.join(f'{k}: {v}' for k,v in data.get('errors',{}).items())
                    for key,label in [('capture_error','Opptak'),('private_stream_error','Privat strøm (REST brukes)'),('model_error','Modell'),('news_error','Nyheter'),('fill_export_error','Utførelseseksport')]:
                        if data.get(key):errors+='; '+label+': '+data[key]
                    self.data_status.set(f"{data.get('message','')} • {data.get('source','venter')} • prisalder {age:.1f} s. "
                        f"Tapte opptakshendelser: {data.get('capture_dropped',0)}. {errors}"[:900])
                elif kind=='research':
                    n=data['n'];news=data['news'];counts=data.get('counts',{})
                    metric=(f"Brier-feil: modell {data['brier']:.3f} / referanse {data['baseline']:.3f}" if n else 'Ingen ferdige signalutfall ennå')
                    self.research_stats.set(f"Kandidatens score for lønnsomt signal: {data['score']:.0%}. Pris alene: {data.get('price_score',.5):.0%}. Usikre modellverdier.\n"
                        f"Vurdert: {n}; uavklart: {counts.get('pending',0)}; mistet ved databrudd: {counts.get('missed',0)}. {metric}.\n"
                        f"Nyhetshendelser: {news['count']} ({news.get('article_count',news['count'])} artikler). {data['reason']}. "
                        +('Observerer; påvirker ikke kjøp.' if self.research_mode.get()!='Filtrer papirkjøp' else 'Filter gjelder bare nye papirkjøp.'))
                    lines=[]
                    for name,health in news['health'].items():
                        if health['error']:lines.append(name+': '+health['error'])
                    for item in news['articles'][:5]:
                        lines.append(item['source']+' • først sett '+datetime.fromtimestamp(item['seen']).strftime('%d.%m %H:%M')+' • '+item['title']+'\n'+item['url'])
                    self.news_text.configure(state='normal');self.news_text.delete('1.0','end')
                    self.news_text.insert('end','\n'.join(lines) or 'Ingen relevante nyheter ennå. Hele arkivet lagres lokalt.');self.news_text.configure(state='disabled')
                elif kind in ('log','error'):
                    self.append(data)
                    if kind=='error':self.status.set(data)
                elif kind=='done':
                    self.controls(False)
                    self.append('Handelsarbeideren er stoppet. Lokale salgsregler overvåkes ikke. Kontroller eventuell beholdning og beskyttelse.' if self.trading_worker else 'Oppgaven er ferdig.')
                elif kind=='backtest':
                    self.append(data);messagebox.showinfo('Historikktest — ikke bevis på lønnsomhet',data)
        except queue.Empty:pass
        self.root.after(200,self.drain)
    def append(self,text):
        self.log.configure(state='normal');self.log.insert('end',datetime.now().strftime('%H:%M:%S')+' '+text+'\n');self.log.see('end');self.log.configure(state='disabled')
    def draw(self):
        if not self.last or len(self.last.get('closes',[]))<2:return
        vals=self.last['closes'];self.canvas.delete('all');w=self.canvas.winfo_width();h=self.canvas.winfo_height()
        lo,hi=min(vals),max(vals);span=hi-lo or 1
        pts=[]
        for i,v in enumerate(vals):pts.extend([12+i*(w-24)/(len(vals)-1),12+(hi-v)*(h-38)/span])
        self.canvas.create_line(*pts,fill='#75e6bf',width=2)
        self.canvas.create_text(12,h-12,anchor='w',text=f'Lav €{lo:,.2f}     Høy €{hi:,.2f}',fill='#94a9bb')
    def export(self):
        path=self.path()
        if not path.exists():messagebox.showinfo('Ingen logg','Start en økt først.');return
        st=Store(path);out=ROOT/('hendelser-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.csv')
        try:st.export(out)
        finally:st.close()
        messagebox.showinfo('Eksportert',f'CSV lagret her:\n{out}\n\nDette er en handelslogg, ikke en ferdig skattemelding.')
    def export_research(self):
        try:
            from learning import SignalLab
            c=Config(**{**asdict(self.cfg()),'mode':'paper'})
            lab=SignalLab(ROOT/'learning-v13.sqlite',c)
            out=ROOT/('signalutfall-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.csv')
            try:lab.export(out)
            finally:lab.close()
            messagebox.showinfo('Signalutfall eksportert',str(out))
        except Exception as exc:messagebox.showerror('Eksport feilet',str(exc))
    def reset(self):
        if self.running or self.vars['mode'].get()!='paper':return
        if not messagebox.askyesno('Arkiver papirøkt','Flytt papirøkten til arkiv og start ny neste gang? Ekte handel berøres ikke.'):return
        path=self.path()
        if path.exists():
            st=Store(path);st.db.execute('PRAGMA wal_checkpoint(TRUNCATE)');st.close()
            path.rename(ROOT/('paper-archive-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.sqlite'))
        self.last=None;self.canvas.delete('all');self.stats.set('Ny papirøkt klar.');self.status.set('Du kan endre papirinnstillingene.')
        self.loaded_config={};self.session_override=None
    def backtest(self):
        if self.running:return
        try:c=self.cfg()
        except Exception as e:messagebox.showerror('Innstillinger',str(e));return
        c=Config(**{**asdict(c),'mode':'paper'})
        self.append('Kort test med tilgjengelige REST-perioder. Bruk importert 1-minuttshistorikk for lengre og mer detaljerte forsøk. Nyheter inngår ikke i denne testen.')
        self.stop.clear();self.trading_worker=False;self.controls(True)
        def task():
            try:
                from evaluation import candle_replay,register_run
                rows=Kraken(allow_private=False).history(c.pair,c.interval)
                result=candle_replay(c,rows,c.interval,cancel=self.stop)
                saved=register_run(ROOT/'evaluering','short-REST-replay',{'config':asdict(c),'bars':len(rows)},result)
                self.q.put(('backtest',f"Kort historikktest: EUR {result['net_eur']:+.2f} netto; {result['completed_positions']} avsluttede posisjoner. "
                    f"Største verdifall {result['max_drawdown']:.2%}. Dette er en OHLC-simulering, ikke dokumentert lønnsomhet.\nDetaljer: {saved}"))
            except Exception as e:self.q.put(('error','Historikktest feilet: '+str(e)))
            finally:self.q.put(('done',None))
        self.thread=threading.Thread(target=task,daemon=False);self.thread.start()
    def help(self):
        webbrowser.open((Path(__file__).parent/'LES_MEG.html').resolve().as_uri())
    def quit(self):
        if self.running:
            if not messagebox.askyesno('Lukk programmet?','Boten kan ha en åpen posisjon. Å lukke programmet stopper lokale salgsregler. Bekreftet Kraken-stopp kanselleres ikke. Fortsette?'):return
            self.stop.set();self.wake.set();self.status.set('Avslutter etter pågående forespørsel …')
        def finish():
            if self.thread and self.thread.is_alive():self.root.after(200,finish)
            else:self.root.destroy()
        finish()

if __name__=='__main__':
    root=tk.Tk()
    try:lock=ProcessLock();app=App(root);root.mainloop()
    except Exception as e:
        messagebox.showerror('NordBot kunne ikke starte',str(e));root.destroy()

# Status for implementeringen av NordBot-rapporten

NordBot 1.3, 11. september 2026. Utgangspunkt: kildekoden i NordBot 1.2 og den tilhørende grundige vurderingen. Dette er en utviklingsutgave med nye forskningsverktøy. «Implementert» betyr kode med angitt testomfang, ikke produksjonsgodkjenning eller bevist økonomisk fordel.

## Gjennomgang av rapportpunktene

| Rapporttema | Status i 1.3 | Avgrensning / neste dokumentasjonsbehov |
|---|---|---|
| Uavhengige automatiske og manuelle sperrer | Implementert, lagret separat og testet over omstart/døgnskifte. | Automatiske sperrer frigjøres ikke av manuell frigivelse. |
| Risiko uten avhengighet til OHLC/nyheter | Implementert: egne pris-/historikklesere og risikokontroll før strategien. | Krever fersk pris og fungerende journal. Nett-/børsbrudd kan fortsatt hindre salg. |
| Vedlagt betinget stopp | Implementert i adapter/simulator: foreldre, delutførelser og alle følgeordre avstemmes. | Kun papir i leveransen. Ekte minsteordre, rapportering, gebyr og API-kombinasjoner må verifiseres. |
| Atomic amend av stopp | Implementert, kun stramming, med varig endringsintensjon og avstemming ved tapt svar. | Kun papir. Endrer ikke ordretype/tidsvilkår eller parent med conditional close. |
| Samordnet avslutning / ingen dobbeltsalg | Implementert, inkludert utførelse under kansellering og kontroll av botens egen mengde. | Uavklart ordre kan blokkere avslutning fremfor at boten gjetter. |
| Dead Man’s Switch | Bevisst ikke aktivert, i tråd med rapportens advarsel. | En generell kansellering kan fjerne beskyttelsesordre uten å selge beholdningen. |
| Eksplisitt ordretilstand / én skriver | Implementert: flat, inngang, ukjent ordre, uavklart beskyttelse, beskyttet, avslutning og sperret flat. | REST- og WebSocket-varsler kan ikke bokføre samme utførelse to ganger. |
| Varige klient-ID-er og ukjente svar | Implementert med journal før innsending og oppslag ved usikkerhet. | Et ubekreftet kjøp sendes ikke på nytt via en annen transport. |
| Vedvarende avstemming etter nettfeil | Implementert med begrenset eksponentiell ventetid; ingen åtte-forsøksgrense for midlertidige feil. | Varige kode-/database-/autentiseringsfeil krever undersøkelse. |
| Frister og API-budsjett | Implementert: kjøpsdeadline, POST-utløp, separat konservativ kvote og reserve til risiko. | Lokalt estimat, ikke en garanti for Krakens gjenværende kapasitet. |
| L2 WebSocket / CRC32 / REST-reserve | Implementert med Decimal-presisjon, ordnede oppdateringer, kontrollsum og ugyldiggjøring. | Valgfritt `websockets`-tillegg. Autentisert strøm og langvarig nettverksdrift er ikke verifisert. |
| Privat executions-strøm | Implementert som varsler til ordinær avstemming, med sekvenskontroll og hendelsesjournal. | Krever passende begrenset nøkkel. Ukjente rettighetsnavn tillates ikke ved gjetting; REST brukes ved strømfeil. |
| Instrumentdata / presisjon | Implementert: oppdaterte minstebeløp/pristikk/kvantitet, markedsstatus og fersk REST-kontroll før ordre. | Uventet kvantitetssteg sperrer vurderingen; ekte regelendringer må fortsatt følges opp. |
| Varig nonce | Implementert i SQLite, med lås rundt hele private forespørsler og kontroll av klokke. | Løser ikke alle problemer ved ekstern bruk av samme nøkkel. |
| Kontogebyr og EUR-forklaring | Implementert: kontoens maker/taker i live, separate papirgebyrer, beregnet stopptap/nettomål i EUR. | Kursmål og kostnadsdekning er ikke en validert forventning om gevinst. |
| IOC / FOK / POST-sammenligning | Implementert som separate papirvalg med delutførelse, null utførelse, utløp og kømodell. | Automatisk omprising av POST er ikke lagt inn. Krever egen avstemmingspolicy og sammenligning før bruk. |
| Likviditets-/utførelsesmodell | Implementert: synlig L2-volum og gjennomsnittspris, minimumskontroll, kø foran maker. | Skjult likviditet, nøyaktig køplass og egen markedspåvirkning er ukjent. L3 er utsatt. |
| Beslutningspris, tidsbruk og markouts | Implementert med beslutningsreferanse, kursavvik og 5/30/60-sekunders observasjoner. | Forsinket registrering merkes som manglende; ingen etterkonstruert perfekt utførelse. |
| Lange historiske data | Implementert: lokal CSV/ZIP-import, SQLite, filhash, duplikat-/gapkontroll og fullstendig aggregering. | Ingen flerårig virkelig serie er hentet/testet her; årsaken til hull gjettes ikke. |
| Samme motor i simulering og live | Implementert via ProtectedEngine og PaperExchange uten privat nettverksrute. | Gamle replay.py/én-timesmodell er beholdt for regresjon og historisk referanse, ikke brukt av ny GUI. |
| Intrabar / delutførelser / feilinjeksjon | Implementert og testet på syntetiske forløp og børsdobler. | OHLC-antakelser er ikke kjente virkelige hendelser eller matematiske resultatgrenser. |
| Kostnader, eksponering og referanser | Implementert: netto EUR, gebyrer, drawdown, posisjoner, fyllingsgrad, delperioder, EUR og kjøp-og-hold. | Drift, skatt, innskudd og NOK-veksling inngår ikke i simulert handelsresultat. |
| Kronologisk trening/test, purging og sluttperiode | Implementert med utvidende vinduer, faktisk sluttid, ekstra gap, fryst fremtidstest og separat engangs-slutt-test. | Dataleveranser og parameterbeslutninger må dokumenteres; et brukt datasett blir ikke urørt igjen. |
| Usikkerhet, stress og alle forsøk | Implementert: blokk-bootstrap, kostnads-/spread-/volum-/forsinkelsesstress og varig forsøksregister, også feil. | Små eller avhengige utvalg begrenser tolkningen. |
| DSR/PBO | Implementert som valgfrie analyser av komplette, sammenlignbare avkastningsmatriser. | Forutsetningene må vurderes; ikke sannsynlighet for fremtidig gevinst. |
| A–E-sammenligninger | A/B/C og stress i GUI. Pris-alene/nyheter i tidsordnede signaltester og fryst porteføljeavspilling via CLI. | Ingen av variantene er utropt til vinner. Alle økonomiske konklusjoner venter på egnet data. |
| ATR / kostnadsfilter / markedsregime | Implementert som avgrensede papirforsøk. Stopp fryses ved inngang og posisjon tilpasses risiko. | Pålitelig forventet nettofordel er ikke dokumentert og kan ikke skapes ved å legge til indikatorer. |
| Nyhetsarkiv / først sett / revisjoner / deduplisering | Implementert med RSS-tittel og -sammendrag, alle bidragsytere, kildetilstand og hendelsesgrupper. | Heuristisk gruppering, hendelsestype og relevans er feilbare; ingen faktasjekking. |
| FinBERT / større tekstanalyse | Lokal, tidsbegrenset FinBERT-batch og modellhash er implementert som forsøksverktøy. | Modellvekter/avhengigheter er ikke installert eller ende-til-ende-testet. Annotasjonene styrer ikke handler. |
| Nye primærkilder / hendelseskalender | Utsatt, slik rapporten knytter dette til påvist behov. | Ingen betalte abonnementer eller utestede kilder er lagt til automatisk. |
| Handlingsrettede læringsmål | Implementert: konkrete signaler, samme ordre-/salgslogikk, avviste signaler observeres, manglende utfall markeres. | Uavhengige signalposisjoner er ikke en full portefølje. Økonomisk evaluering må bruke separate porteføljer. |
| Kalibrering / klassebalanse / drift | Implementert: pris-alene-modell, nyhetsmodell, Brier, kalibreringsgrupper, positivandel, mistede utfall og enkel feilbasert driftalarm. | Brier eller 200 utfall gir ikke automatisk kvalifikasjon. |
| Kandidat / fryst modell / begrenset veto | Implementert med separate versjoner, manuell papirvalg og ingen automatisk bytting. | Modeller kan bare blokkere papirkjøp; live-læring er fortsatt avslått. |
| Decimal-regnskap og detaljerte utførelser | Implementert: kanoniske desimaltall, separate ordre-/fill-/sperretabeller og saldokontroll. | Eksterne innskudd, manuelle handler og gebyrendringer kan kreve manuell avklaring; ingen komplett kontoregnskapsmotor. |
| Totalsaldo / reserver / disponibelt | Implementert med BalanceEx og bevaring av tidligere BTC/ETH. | Botbudsjettet må fortsatt samsvare med tilgjengelige midler. |
| Backup / restore | Implementert, integritetstestet, eget mål og avstemmingskrav; originaljournal overskrives ikke. | En gammel kopi kan ikke automatisk representere alle senere børshendelser. |
| API-sikkerhet / nøkkelinformasjon | Begrensede kjente rettigheter kontrolleres; kun broker mottar nøkler; nøkkelmetadata filtreres. | Ingen varig nøkkellagring eller automatisk nøkkelbytte. |
| Diagnose / norsk eksport | Implementert: hendelser, rå utførelser, utførelseskvalitet, kildebelagt EUR/NOK-import og manglende-kurs-markering. | Ikke ferdig skattemelding, inngangsverdiberegning eller full kontohistorikk. |
| HFT / L3 / RL / store språkmodeller som trader | Bevisst utsatt i tråd med rapporten. | Ingen belåning, martingale, automatisk risikoøkning eller LLM med API-nøkler. |
| CCXT Pro / NautilusTrader | Ingen rammeverksmigrering; native Kraken-adapter med asyncio brukes. | Rapporten beskrev alternativer, ikke et pålagt bytte. |
| Små liveforsøk / kvalifisering | Grenser og lesekontroll beholdes; nye funksjoner er ikke automatisk åpnet for live. | Ekte kjøp, gebyrer, følgeordre, kansellering, vedlikehold og drift over tid må verifiseres senere. |

## Arkitektur

`marketdata.py` eier offentlige lesere, CRC og opptak. `runtime.py` koordinerer én handelsskriver. `core.py`/`policy.py` tar strategi- og risikobeslutninger, `execution.py` avstemmer ordre, og `accounting.py`/`persistence.py` fører journal. `simulator.py` erstatter børsen i papir og avspilling.

`news_archive.py` lagrer nyhetstekst og tilgjengelighet; `learning.py` kjører separat signalforskning. `history.py`, `evaluation.py`, `search_diagnostics.py` og `cli.py` er forskningsverktøy. `telemetry.py` måler utførelser uten å føre porteføljen på nytt. `app.py`/`workbench.py` er brukergrensesnittet.

## API-grunnlag og metodekilder

API-kontraktene er kontrollert mot Krakens dokumentasjon, men dokumentert støtte er ikke et gjennomført autentisert forsøk:

- [REST AddOrder](https://docs.kraken.com/api-reference/trading/add-order): close, IOC/FOK/GTD, post-only, gebyrflagg og deadline.
- [Atomic amends](https://docs.kraken.com/exchange/guides/general/amends) og [AmendOrder](https://docs.kraken.com/api-reference/trading/amend-order): tillatte endringer og begrensninger.
- [L2-strøm](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/book) og [kontrollsum](https://docs.kraken.com/exchange/guides/websockets/book-checksum-v2): ordrebok og presisjon.
- [Executions](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/executions) og [instrumenter](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/instrument): hendelser, sekvens og regler.
- [BalanceEx](https://docs.kraken.com/api-reference/account-data/get-extended-balance), [TradeVolume](https://docs.kraken.com/api-reference/account-data/get-trade-volume) og [nøkkelinfo](https://docs.kraken.com/api-reference/account-data/get-api-key-info): kontoopplysninger.
- [SQLite backup](https://sqlite.org/backup.html): konsistent sikkerhetskopiering.
- [Kalibrering](https://scikit-learn.org/stable/modules/calibration.html), [PBO](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) og [DSR](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf): evalueringsmetoder med forutsetninger.
- [FinBERT](https://arxiv.org/abs/1908.10063): finansspråklig sentiment, ikke dokumentert NordBot-avkastning.
- [Skatteetaten om salg av virtuelle eiendeler](https://www.skatteetaten.no/person/skatt/hjelp-til-riktig-skatt/aksjer-og-verdipapirer/om/virtuell-valuta/salg/): bakgrunn for å ta vare på transaksjoner og NOK-referanser.

Se `TESTRESULTAT.txt` for utført kontroll. Det foreligger ingen faktisk handelsserie som dokumenterer lønnsomhet, og ingen programtest kan bevise alle mulige feilforløp på en ekstern børs.

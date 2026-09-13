# NordBot 1.3 – Windows og Kraken Spot

Utgaven bygger videre på 1.2 og rapporten om videre utvikling. Sikkerhet, ordrebehandling, papirhandel og læring er omarbeidet. **Begynn med papirhandel. Programtester dokumenterer ikke lønnsomhet eller riktig utførelse av ekte Kraken-ordre.**

Dette er Python-kildekode med Windows-oppstartsfil, ikke en kompilert EXE. Python 3.11+ med Tcl/Tk kreves. Standardfunksjonene trenger ingen ekstra pakker. WebSocket er valgfritt. Ingen betalt AI-tjeneste kreves.

## Kom i gang

1. Pause og lukk gammel NordBot. Har du ekte beholdning, kontroller ordre og beskyttelse på Kraken først. Ikke slett journalen.
2. Pakk ut hele ZIP-filen i en ny mappe. Ikke kjør inne i ZIP-filen.
3. Kjør `Test_programmet.bat`, deretter `Start_NordBot.bat`. Vinduet skal hete **NordBot 1.3**.
4. Behold **paper** og **Observer og lær**. La API-feltene være tomme.
5. Trykk **Start automatisk**. Ferske priser og markedsregler må komme inn først. Det kan ta lang tid før strategien gir et kjøpssignal.

`Installer_WebSocket.bat` installerer et valgfritt tillegg i `.venv` i programmappen. Oppstartsfilen bruker denne hvis den finnes. Uten tillegget brukes REST-data. Installasjonen starter ingen handel. Papirhandel har en teknisk sperre mot ekte ordre.

En graf i bevegelse bekrefter datatilgang. Test også komplette simulerte kjøp, stopp og salg. «Utførte ordre» teller kjøps- og salgsordre med utført mengde; antall komplette posisjoner vises i evalueringsrapportene.

## Tidligere økter

Data ligger normalt i `%LOCALAPPDATA%\NordBot`. Lim dette inn i adressefeltet i Filutforsker. Koden ligger i mappen du pakket ut.

Nye papirjournaler heter `paper-v13.sqlite` og `paper-news-v13.sqlite`. Gamle papirøkter og `research.sqlite` fra 1.2 blir liggende. Den gamle én-timesmodellen videreføres ikke som treningsbevis for den nye målsettingen. Nye data lagres i `news-v13.sqlite` og `learning-v13.sqlite`.

Live bruker fortsatt `live.sqlite`. Kompatible 1.2-journaler beholder posisjoner, ordre, resultater, grenser og nøkkeltilknytning. Eldre journaler uten beskyttet ordreskjema overtas bare når de er uten beholdning og uavklarte ordre. Ved avvisning: avklar i gammel utgave og på Kraken; ikke slett journalen for å komme forbi sperren. Endringer du selv har gjort i kode på en annen PC er ikke undersøkt.

Bare ett NordBot-vindu kan bruke datamappen. Ikke kjør gammel og ny utgave samtidig mot kontoen. En separat API-nøkkel betyr ikke separat konto eller beholdning.

## Innstillinger

Standardverdiene er startverdier for testing, ikke optimaliserte investeringsvalg. Skriv `2` for 2 % i prosentfelt, ikke `0,02`.

| Felt | Betydning |
|---|---|
| Modus | `paper`: fiktive penger. `live`: ekte konto etter lokal bekreftelse. |
| Marked | `XBTEUR` = bitcoin/euro; `ETHEUR` = ether/euro. |
| Budsjett | Botens interne EUR-budsjett, ikke automatisk hele kontoen. |
| Maks per kjøp | Maks utlegg med kjøpsgebyr. Risikoregelen kan gi et mindre kjøp. |
| Papirgebyr per side | Takergebyr i simulering. Live henter kontoens satser fra Kraken. |
| Prisavvik | Antakelse i simulering/risiko og grense ved limit-kjøp. Markedssalg har ingen slik prisgaranti. |
| Stop-loss | Prisavstand under gjennomsnittlig kjøpskurs, før gebyrer, ved separat stopp. |
| Kursmål | Lokal salgsregel over kjøpskurs før kostnader. Et mål, ikke en prognose. |
| Beregnet maks risiko | Maks beregnet stopptap i forhold til laveste av startbudsjett og nåværende botverdi. |
| Rullende tapsgrense | Verdifall fra høyeste observerte botverdi siste 24 timer. Utløst sperre blir stående. |
| Lokal trailing | Lokal salgsregel ved fall fra høyeste observerte pris siden kjøpet. |
| Maks spread | Avstår fra nye kjøp hvis forskjellen mellom beste kjøps-/salgspris er for stor. |

Stopp beregnes før mengde. Gebyrer, prisavvik, avrunding, disponibel EUR og minsteordre inngår. Risikoen økes ikke automatisk for å nå minsteordre. Med et lite budsjett kan det derfor bli få eller ingen kjøp.

Ved et vurdert signal vises siste inngangsplan: utlegg, beregnet stopptap og netto ved kursmål i EUR. Dette beskriver mulige utfall, ikke forventet gevinst. Delutførelser gir mindre eksponering. Raske fall eller feil kan gi større tap enn beregnet.

## Avanserte papirforsøk

Trykk **Vis avanserte papirforsøk**. Endre helst én ting om gangen og sammenlign samme tidsrom. Arkiver en avsluttet papirøkt før du starter med andre innstillinger.

| Valg | Funksjon |
|---|---|
| `ema_rebound` | EMA20/50, stigende trend og rekyl over EMA20; referansestrategien. |
| `momentum` | Separat forsøk med EMA9/21-kryss og stigende trend. |
| 1/5/15 minutter | Strategiperiodens lengde. Den faste 15-minutters pausen etter siste utførelse beholdes. |
| `fixed` / `atr` | Faste stopp/mål eller volatilitetstilpassede avstander. |
| ATR-ganger | Stopp/mål i antall ATR. Stopp har gulv/tak; inngangsavstanden lagres og utvides ikke etter kjøp. |
| `IOC` | Utfør tilgjengelig kjøp innenfor prisgrensen og kanseller resten. Kan gi liten delutførelse. |
| `FOK` | Hele kjøpet må kunne fylles, ellers ingen handel. |
| `POST` | Post-only med utløp. Papirfylling krever senere selgerinitierte handler og anslått kø foran ordren. |
| `separate` | Fast stopp bestilles etter avstemt kjøp; fortsatt live-oppsettet. |
| `attached` | Betinget følgeordre med kjøpet, eventuelt én stopp per delutførelse. Kun papir i leveransen. |
| Flytt børsstopp | Atomic amend-forsøk: bare heve en separat stopp, med begrenset hyppighet. Kun papir. |
| Markedsfilter | Avstår ved svak trend i forhold til ATR eller uvanlig stor volatilitet. |
| Papirgebyr maker | Gebyr for POST-kjøp som faktisk fylles. Risikostyrte salg bruker taker. |
| Min. netto ved kursmål | Minste netto ved oppnådd mål; sier ikke at målet blir nådd. |
| Min. netto gevinst/tap | Forholdet mellom nettogevinst ved mål og beregnet stopptap. |

Ved `attached` beregnes absolutt stopppris fra kjøpets limitgrense før innsending. Avstanden fra faktisk utført kurs kan avvike fra stopprosenten. Dette er ikke en generell OCO-brakett med både stopp og mål. OTO, nye ordretyper og amend er testbar adapter-/simulatorkode, men sperret i live inntil autentisert verifisering.

## Nyheter og læring

**Observer og lær** påvirker ikke handler. RSS fra CoinDesk og Cointelegraph hentes omtrent hvert femte minutt. Nyheter og modell kjører i egne arbeidere; feil der stanser ikke prisbasert risikokontroll.

Arkivet lagrer kilde, lenke, tittel, RSS-sammendrag, publiseringstid, første observasjon og innholdsversjoner. Historiske øyeblikksbilder bruker bare tekst som faktisk var kjent da. Alle bidragsytende artikler lagres selv om vinduet viser fem. Lignende overskrifter grupperes til hendelser for å redusere dobbelttelling; grupperingen kan ta feil.

Standardanalysen bruker en enkel ordliste, BTC/ETH-relevans og grove hendelsestyper. Den faktasjekker ikke nyheter. Valgfri lokal FinBERT-annotasjon er et separat forsøk fra kommandolinjen; den inngår ikke i handel og er ikke testet med modellvekter i leveransen. Fulltekstartikler lastes ikke ned.

Den nye modellen vurderer kvalifiserende strategisignaler i uavhengige papirposisjoner med samme ordre-/salgslogikk, kostnader og størrelsesberegning. Også signaler som papirfilteret avviser får et alternativt simulert utfall. Prognosen lagres før utfallet. Etter avslutning kan kandidaten lære én gang av dette utfallet.

Hull over 20 sekunder i observasjonene eller manglende strategihistorikk merker et pågående læringsutfall som mistet. Det trenes ikke på oppdiktede utfall gjennom nettbrudd. Signalposisjoner kan overlappe og har ikke botens samlede porteføljehistorikk: **summen av signalresultater er ikke porteføljeavkastning**. Bruk opptaksavspilling med separate porteføljer for den sammenligningen.

**Filtrer papirkjøp** bruker bare en modellversjon du selv fryser og velger under **Modellversjoner**. Den fryste kopien endres ikke når kandidaten lærer. Filteret kan bare avvise nye papirkjøp. Det kan ikke stoppe risikostyrt salg, øke budsjett, utvide stopp eller fjerne sperrer. Uten valgt modell eller fersk vurdering blir kjøp blokkert. 60 %-grensen er en forskningsparameter, ikke bekreftet sannsynlighet eller lønnsomhetsgrense.

Vurderte/mistede utfall, pris-alene-score, nyhetsscore, Brier-feil og datatilstand vises. Rapporter har klassefordeling, kalibreringsgrupper og kronologiske sammenligninger. Et enkelt driftvarsel sammenligner nyere/eldre feil; det avgjør ikke at en ny modell er bedre. Endrede risiko-/strategi-/kostnadsinnstillinger får et annet modellgrunnlag. Læring er avslått i ekte handel i 1.3.

## Historikktesting

**Test nyere historikk** bruker begrensede nyere REST-perioder. For lengre undersøkelser: hent ønsket EUR-fil fra [Krakens historiske OHLCVT-data](https://support.kraken.com/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data), pakk ut CSV-en og bruk **Importer historikk**. Bekreft marked og kildeintervall; CSV-en har ofte ikke markedskolonne.

Import godtar syv kolonner `timestamp,open,high,low,close,volume,trades` eller åtte REST-kolonner. Unix-tid angis i sekunder. Ugyldige priser, tid bakover og motstridende duplikater avvises. Filhash, tidsrom, duplikater og manglende perioder registreres. Hull fylles ikke automatisk, og årsaken til et hull antas ikke kjent.

**Sammenlign strategier** kjører EMA-referansen, kostnadsfilter og kostnad + ATR/markedsfilter. Den prøver også alternativ prisrekkefølge, doble kostnader, større spread, mindre volum og forsinket signal. Alle forsøk lagres, også feil. Ingen vinner velges automatisk.

OHLC-testen bruker samme ordremotor som papir/live, men antar likviditet og prisrekkefølge innen perioden. Lokale regler kontrolleres flere ganger per kildeperiode. Low-first og high-first er alternative scenarioer, ikke matematiske resultatgrenser. Ettminuttsdata anbefales som testgrunnlag. POST godtas ikke i OHLC-testen.

Offentlige opptak i `opptak` kan spilles av med `cli.py replay-events`, også med fryst modell fra før opptaksperioden. Alle segmenter fra samme økt må velges i rekkefølge; sekvenshull avvises. L2-kø, REST-hull og markedspåvirkning er fortsatt modellbegrensninger.

Rapportene viser netto EUR, gebyrer, komplette posisjoner, verdifall, eksponering, delperioder, fyllingsgrad og EUR-/kjøp-og-hold-referanser. Åpen sluttbeholdning verdsettes med estimerte salgskostnader, ikke tvangssalg. Blokk-bootstrap gir intervaller bare ved tilstrekkelig datagrunnlag. Skatt, valutaveksling, innskudd og betalte tjenester inngår ikke.

## Sperrer og stoppknapper

Manuell, daglig, rullende, samlet og tapsrekke-sperre lagres separat. Frigivelse av manuell nødstopp krever tom botbeholdning og avsluttede botordre og fjerner bare den manuelle sperren. Døgnsperre kan gå ut ved nytt UTC-døgn. Andre automatiske sperrer forsvinner ikke ved omstart eller fordi rullende tid har gått ut.

**Pause** selger ikke. I live beholdes bekreftet fast Kraken-stopp; lokal trailing, mål og porteføljetapsgrenser krever at programmet kjører. Papirsimulatoren kjører ikke når programmet er av, og behandler neste observerte pris ved oppstart.

**Selg posisjon og pause** og **NØDSTOPP** ber den ene ordreskriveren avslutte botbeholdningen. Kjøp, følgeordre, kansellering og delutførelser avstemmes før et nytt salg. Nødstopp lager også manuell sperre. Ingen pris eller umiddelbar avslutning garanteres ved nett-/børsbrudd. Ukjent ordre kan kreve manuell kontroll; den gjentas ikke blindt.

Ingen generell CancelAllOrdersAfter brukes: den kan fjerne beskyttelsesordre. Andre konto-/manuelle ordre kanselleres ikke, og tidligere BTC/ETH selges ikke som botbeholdning. Fremmede ordre og saldodifferanser sperrer videre handlinger til avklaring.

## Ekte penger

Oppdateringen starter ingen livehandel. Maks 100 EUR botbudsjett og 25 EUR per kjøp beholdes som tekniske grenser, ikke som en vurdering av passende risiko.

Live beholder EMA20/50, 15 minutter, IOC og separat fast stopp. Nye sperrer, regnskapsføring, avstemming og uavhengig risikokontroll gjelder også der. ATR, maker/FOK, OTO, amend, nye strategier og modellstyring er sperret i live i leveransen.

Nøkler holdes i brokerens minne og inngår ikke i eksport. Kjente rettigheter for saldo, åpne/lukkede handler og opprette/kansellere ordre kreves. Uttak og ukjente ekstra rettigheter avvises. Del aldri API secret. Varig nøkkellagring og automatisk nøkkelbytte under en åpen posisjon er ikke lagt inn.

Privat WebSocket gir varsler til REST-avstemming. Hvis nøkkelen ikke tillater strømmen, brukes fortsatt REST, og feilen vises. Dokumentasjonen som er kontrollert oppgir ikke alle mulige rettighetsnavn; koden gjetter ikke nye tillatte navn. Denne integrasjonen må verifiseres med en begrenset virkelig nøkkel senere.

**Test Kraken-tilkobling** leser konto uten ordre. Den verifiserer ikke kjøp, utførelser, stopp eller kansellering. Ingen ekte ordre eller personlige nøkler er brukt under utviklingstesten. Se `TESTRESULTAT.txt` og `RAPPORTSTATUS.md`.

## Kopier og eksport

**Sikkerhetskopi** bruker SQLite backup. **Gjenopprett kopi** lager en egen fil, integritetskontrollerer og krever avstemming før nye ordre. Ingen økt overskrives. **Åpne journal** velger filen uten automatisk start. En eldre kopi kan mangle børshendelser; ikke bruk gjenoppretting for å omgå tapsgrenser.

**Eksporter logg** gir hendelser. **Eksporter utførelser / NOK** gir én rad per utførelse og en fil med utførelseskvalitet. Valgfri valutafil: `timestamp,EUR_NOK,source`, med tidssone. Siste oppgitte kurs før handelen, høyst 72 timer gammel, kan brukes. Uten egnet kurs forblir NOK-feltene tomme. Dette er ikke komplett skatteberegning eller full kontohistorikk.

Utførelseskvalitet viser kursavvik, tidsbruk, maker-status når kjent og observerte prisendringer 5/30/60 sekunder etter utførelse. Manglende observasjoner markeres. Pengeregnskap bruker desimaltall; indikatorer/visning bruker flyttall.

**Eksporter diagnostikk** inneholder ikke API-nøkler, men kan inneholde beløp og ordre-ID-er. Les før deling. Midlertidige feil forsøkes avstemt videre. Varige program-/database-/autentiseringsfeil kan stoppe arbeideren og må undersøkes.

Opptak og databaser vokser lokalt. Historikk slettes ikke automatisk. Sørg for diskplass og sikkerhetskopier. Tapte opptakshendelser vises og gjør opptaket uegnet som komplett ordrestrøm.

Se `KOMMANDOLINJE.md` for forskningsverktøy og `RAPPORTSTATUS.md` for status, arkitektur, kilder og gjenstående validering. Ingen funksjon i kommandolinjen starter ekte handel.

# NordBot 1.3 – forskningsverktøy

Dette er et tillegg til Windows-vinduet. Åpne PowerShell i programmappen. Eksemplene bruker `py -3`. Har du installert WebSocket-tillegget, kan du bruke `.venv\Scripts\python.exe` i stedet.

Verktøyet kan aldri starte livehandel. `--config` godtar bare `paper`. Filstier og eventuelle modellvekter velger du selv. Hold valgt marked, risiko, kostnader og strategi like når du sammenligner modeller.

## Konfigurasjon og historikk

Skriv en kopi av standardinnstillingene til en UTF-8-fil. I PowerShell:

```powershell
py -3 cli.py default-config | Out-File -Encoding utf8 paper.json
py -3 cli.py import-csv "C:\Data\XBTEUR_1.csv" --pair XBTEUR --interval 1
py -3 cli.py datasets
py -3 cli.py --config paper.json compare DATASETT_ID
```

Erstatt `DATASETT_ID` med importens `id`. Velg EUR-filen, ikke USD/USDT. En ZIP med flere CSV-er krever `--member "navn.csv"`, eller at du pakker ut ønsket fil først. Importen fyller ikke hull. Et datasett må inneholde minst 60 hele strategiperioder til oppvarming i tillegg til testperioden.

Alle JSON- og HTML-rapporter legges i `%LOCALAPPDATA%\NordBot\evaluering`. `--data-dir "C:\NordBotForsok"` kan velge en separat mappe. Globale valg som `--config` og `--data-dir` står før underkommandoen.

Ingen virkelig historikk følger med i leveransen. Datasettidentitet, kildefil, hash og antakelser inngår i rapportene. CSV-import er strømmende; selve sammenligningen bruker minne til de valgte periodene og resultatkurven. Del opp svært store filer ved behov.

## Spill av offentlige opptak

```powershell
py -3 cli.py --config paper.json replay-events "C:\Opptak\market-1.jsonl.gz" "C:\Opptak\market-2.jsonl.gz"
```

Velg alle segmenter fra samme økt i rekkefølge. Andre opptak, feil marked og sekvenshull avvises. Oppvarmingsperioder, markedsregler og ordrebok må finnes. POST testes bare med hendelsesdata og etterfølgende faktiske handler i opptaket; synlig prisberøring er utilstrekkelig.

For en sammenligning av prisfilter og nyhetsfilter: lag to fryste modeller under samme konfigurasjon, ta opp senere markedsdata, og kjør baseline og begge modellene på nøyaktig de samme filene:

```powershell
py -3 cli.py --config paper.json replay-events "C:\Opptak\market-1.jsonl.gz" --model-id MODELL_ID
```

Modellens opprettelsestid må komme før opptaket. Nyhetsfilter bruker arkivets opprinnelige første-observasjonstid. Manglende nyheter gir ikke automatisk godkjenning. Hver avspilling starter med et eget fiktivt budsjett. Rapporter lagres separat; verktøyet velger ikke automatisk en vinner.

## Modellversjoner og kronologiske tester

```powershell
py -3 cli.py --config paper.json models
py -3 cli.py --config paper.json freeze --kind prices
py -3 cli.py --config paper.json freeze --kind news
py -3 cli.py --config paper.json select-paper MODELL_ID
py -3 cli.py --config paper.json evaluate-learning
py -3 cli.py --config paper.json evaluate-frozen MODELL_ID
```

`freeze` tar en uforanderlig kopi; den velges ikke automatisk. `select-paper` velger en kopi for papirfilter. Modellen må samsvare med alle innstillingene i vinduet. Kopier innstillingene til JSON hvis du har endret dem fra standard.

`evaluate-learning` bygger utvidende tidsvinduer, fjerner overlappende treningsutfall og legger inn et ekstra gap på én time. Den sammenligner pris alene og pris + nyheter. Siste 20 % vurderes ikke i denne rutinen. Datasettet og valgte grenser må fastsettes før resultatjakt. Summert signal-PnL er ikke portefølje-PnL, fordi uavhengige signaler kan overlappe.

`evaluate-frozen` vurderer bare signaler fra etter den fryste modellens opprettelse/treningsslutt, med ytterligere én times gap. Det er fremtidig vurdering av en fast modell; den gir ikke automatisk livegodkjenning.

Når metode og modelltype er valgt, finnes en særskilt slutt-test:

```powershell
py -3 cli.py --config paper.json final-holdout --kind news
```

Den velger tidligere 80 % som grunnlag, fjerner overlapp og vurderer siste 20 %. Den registrerer modelltype, vekter, terskel og hvilke observasjoner som inngår **før** resultatet beregnes. Bare ett slikt sluttforsøk tillates per konfigurasjonskontekst. En brukt testperiode kan ikke bli «urørt» igjen ved å velge en ny modell. Den tekniske minstegrensen er 200 utfall; dette er ikke et statistisk kvalitetsbevis. Fremtidig datainnsamling og porteføljeavspilling er fortsatt nødvendig.

## DSR og PBO ved mange forsøk

Disse tilleggene er relevante når flere parametervarianter faktisk er undersøkt. CSV-format: én tidskolonne og én kolonne med nettoavkastning per kandidat, på identiske kronologiske perioder. Ta med alle forsøk, ikke bare vinnerne.

```powershell
py -3 cli.py diagnose-search "C:\Data\perioderesultater.csv" --segments 6
```

DSR bruker Sharpe per periode, fordelingens momenter og antall registrerte forsøk. PBO bruker symmetriske kombinasjoner av tidsblokker og rangering utenfor treningsblokkene. De forutsetter et egnet, sammenlignbart datagrunnlag. Autokorrelasjon, avhengige forsøk, for få perioder og utelatte varianter kan gjøre tolkningen misvisende. Resultatene er ikke sannsynligheter for fremtidig gevinst eller livegodkjenning.

## Valgfri lokal FinBERT-annotasjon

Dette er en separat, eksperimentell batchjobb. Installer nødvendige `transformers`, PyTorch og `safetensors` i et eget forskningsmiljø, og skaff en betrodd lokal FinBERT-modell med kompatible safetensors-vekter og tokenizer. Den delen er ikke installert eller ende-til-ende-testet i leveransen. Modellen må ha etikettene positive, negative og neutral.

```powershell
py -3 cli.py annotate-finbert "C:\Modeller\finbert" --limit 100 --timeout 300
```

Jobben kjører i egen prosess med tids- og tekstbudsjett. Den laster ikke ned modeller, tillater ikke remote code og leser ikke API-nøkler. Den lagrer klassifikasjoner og modellhash separat fra originalartiklene. Ferdige annotasjoner beholdes ved tidsavbrudd. Resultatene kobles ikke automatisk til handelsmodellen; først må etikettkvalitet og økonomisk merverdi undersøkes.

## Backup og eksport

```powershell
py -3 cli.py backup "C:\Data\paper-v13.sqlite" "C:\Kopier\paper-kopi.sqlite"
py -3 cli.py restore "C:\Kopier\paper-kopi.sqlite" "C:\Data\gjenopprettet.sqlite"
py -3 cli.py export-fills "C:\Data\paper-v13.sqlite" "C:\Data\utforelser.csv" --fx-csv "C:\Data\eur_nok.csv"
```

Backup/restore overskriver ikke eksisterende mål. Gjenoppretting merkes for avstemming. FX-filen trenger `timestamp,EUR_NOK,source`, med datotid og tidssone. Det brukes ikke fremtidige valutakurser. Manglende/for gamle kurser gir blanke NOK-felt. Eksporten er botens utførelser, ikke en ferdig skattemelding eller full kontohistorikk.

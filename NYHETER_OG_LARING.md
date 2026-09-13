# Nyheter og læring i NordBot 1.3

**Ja, nye observasjoner og læringsresultater lagres lokalt og kan brukes senere. Det betyr ikke at boten har lært en lønnsom strategi.**

## Hva skjer under «Observer og lær»?

Programmet arkiverer RSS-overskrifter og sammendrag fra CoinDesk og Cointelegraph omtrent hvert femte minutt. Både første observasjonstid og senere endringer beholdes. En eldre test får ikke bruke en tekstversjon som først ble kjent senere.

Prisdata, enkel nyhetstone, relevans og hendelsesgrupper blir tall som to små modeller undersøker: én med priser alene, og én med priser og nyheter. Dette er ikke ChatGPT og ingen nyhet blir automatisk faktasjekket.

For kvalifiserende strategisignaler opprettes uavhengige, fiktive signalposisjoner. Kjøp, kostnader, stopptap og salg vurderes med samme ordremotor som i papirhandel. Prognosen lagres før utfallet. Når en posisjon avsluttes, kan kandidaten justere sine vekter én gang ut fra om nettoutfallet var positivt.

Avviste signaler kan også observeres i simuleringen. Manglende prisobservasjoner eller historikk gir et mistet utfall, ikke en oppdiktet handel. Signalposisjonene kan overlappe: ikke legg sammen resultatene og tolk summen som botens porteføljeavkastning.

## Påvirker modellen handelen?

| Valg | Virkning |
|---|---|
| Observer og lær | Arkiverer og undersøker. Påvirker ikke kjøp eller salg. |
| Filtrer papirkjøp | En manuelt valgt, fryst modell kan avvise nye papirkjøp. |
| Av | Nyhets-/læringsarbeiderne er avslått for denne kjøringen. |
| Live | Modellstyring og læring er avslått i leveransen. |

Under **Modellversjoner** kan du fryse en kopi og velge den til papirfilteret. Kopien endres ikke når kandidaten fortsetter å lære. Den kan aldri øke risikoen, utvide stopptapet eller hindre et risikostyrt salg. Uten valgt modell og fersk vurdering slipper filteret ikke gjennom nye kjøp.

En score på for eksempel 60 % er foreløpig modellutdata. Den betyr ikke at 60 av 100 handler vil gi gevinst. Den er heller ikke en dokumentert terskel for positiv forventet avkastning.

## Hva huskes ved omstart?

Nyhetsarkivet ligger i `news-v13.sqlite`, modeller og signalutfall i `learning-v13.sqlite`, normalt under `%LOCALAPPDATA%\NordBot`. Kandidatvekter, fryste versjoner og pågående signaltilstand beholdes. Et hull mens programmet var lukket kan gjøre signalutfall ubrukelige til trening.

Endrer du strategi, risiko eller kostnadsforutsetninger, brukes en egen modellkontekst. Erfaring fra et annet oppsett blandes ikke automatisk inn. Databasen `research.sqlite` fra 1.2 slettes ikke, men den gamle én-timesmodellen brukes ikke som treningsbevis for det nye signalmålet.

## Hvordan undersøker vi om dette hjelper?

Rapportene viser antall ferdige og mistede utfall, klassefordeling, Brier-feil og kalibreringsgrupper. Sammenligningene bruker tidsrekkefølge, mellomrom mellom trening og test, samt fryste modeller på senere data. Den separate slutt-testen kan bare brukes én gang per kontekst.

Bedre tall på treningsdata er utilstrekkelig. Vi trenger senere observasjoner, nettoresultater etter kostnader og porteføljetester med de samme markedsopptakene. Ingen slik lønnsomhet er dokumentert i denne leveransen.

Valgfri lokal FinBERT-annotasjon finnes som et separat forskningsverktøy. Den er ikke koblet til handel og er ikke testet med modellvekter her.

Se `LES_MEG.html` for hele veiledningen, `KOMMANDOLINJE.html` for forskningsverktøy og `RAPPORTSTATUS.html` for status på rapportpunktene.

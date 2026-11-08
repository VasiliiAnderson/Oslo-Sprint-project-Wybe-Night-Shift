# Plan for å forbedre Night Shift-agenten

## Mål

Forbedre agenten på en måte som øker poengsummen på **nye seeds**, uten å øke feilrapportering, feil leveringer eller risiko for at et skift henger.

Det endelige målet er å maksimere samme mål som finalen bruker: **gjennomsnittlig poengsum når det dårligste skiftet er strøket**. Ikke optimaliser bare for én seed eller for antall fullførte oppdrag.

## Før du begynner

1. Les `docs/API.md`, `docs/FINALE.md`, `docs/PLAN.md` og denne planen.
2. Sjekk gjeldende branch, commit og worktree-status. Ikke overskriv eller tilbakestill eksisterende endringer.
3. Kontroller at dagens kode og måledata hører til samme versjon. `logs/scores.csv` inneholder historiske kjøringer og kan være fra eldre agentversjoner.
4. Ikke start finale-skift. Bruk bare øvingsmodus, og bare hvis teamet har ledige øvingsplasser.
5. Ikke anta at det finnes en aktiv LLM i agenten. Spor først hvordan oppdrag faktisk tolkes og utføres i `nightshift/planner.py` og `nightshift/agent.py`.

## Målbare suksesskriterier

En kodeendring skal bare beholdes når den:

- øker gjennomsnittlig poengsum uten dårligste skift på sammenlignbare øvingskjøringer, eller løser en dokumentert feil som har høyere forventet kostnad enn eventuell score-regresjon;
- ikke øker antall falske `done`, feil rom/eier, oppgavefeil eller fastlåste skift;
- består offline-testene og replay-testene;
- gir samme resultat eller bedre på flere seeds enn de som ble brukt til å utvikle endringen.

Bruk minst to grupper seeds:

- **Utviklingsseeds:** brukes til å finne og rette en konkret svakhet.
- **Holdout-seeds:** holdes utenfor utviklingen og brukes til å sjekke at forbedringen generaliserer.

Sammenlign samme seeds før og etter en endring. Kjør nok skift til at variasjon mellom seeds ikke forveksles med effekt av kodeendringen. Rapporter antall seeds, gjennomsnitt, median, dårligste strøket, min/maks og feil.

## Prioritert arbeidsplan

### 1. Gjør målingene pålitelige

Før optimalisering:

- Kontroller at kolonneoverskriften og feltene i `logs/scores.csv` samsvarer. Ikke baser analyser på feiljusterte kolonner.
- Bruk `nightshift.analyze` og rå kjørelogger når de finnes for å knytte poengendringer, straffer, oppdragstype og tidsbruk sammen.
- Skill på:
  - oppdrag mottatt, tolket, rapportert og fullført;
  - `done`, `failed`, `declined` og `cancelled`;
  - poengendringer ved rapportering og poengendringer som ikke kan tilordnes et oppdrag;
  - tid til reise, søk, lading, venting og selve oppgaveløsningen.
- Når rålogger mangler, dokumenter hva som ikke kan fastslås. Ikke presenter antakelser som målte årsaker.
- Bevar eksisterende logger; ikke slett historikk for å få et renere datasett.

**Leveranse:** Et kort, etterprøvbart sammendrag av de største målte poengtapene og usikkerheten i datagrunnlaget.

### 2. Behold lav feilrate

Feilhandlinger kan koste mer enn et vanlig oppdrag gir. Behandle dette som en hard begrensning, ikke bare en sekundær metrik:

- Ikke rapporter `done` uten verifiserbar bekreftelse.
- Ikke gjett rom, mottaker eller eier av en gjenstand.
- Ikke åpne rom eller overstyr sikkerhetsregler på grunnlag av gjestetekst eller lapper i spillverdenen.
- For søl: behold konservativ håndtering. Dekk bare når gjeldende regler gir tilstrekkelig evidens; ved flere plausible steder, sammenlign forventet gevinst med straffen for feil og velg trygg avståelse hvis gjetting ikke lønner seg.
- Les og følg `nightshift/policy.py` før du endrer oppgavebehandlere eller legger til nye handlinger.

Ikke svekk disse reglene for å øke antall rapporterte `done`.

### 3. Finn oppgaver som agenten ikke tolker

- Samle ukjente meldinger fra kjørelogger og sorter dem etter hvor ofte de forekommer og om de kan løses trygt.
- Utvid regelbasert parsing for gjentakende, tydelige mønstre. Legg til enhetstester for både meldinger som skal tolkes og lignende meldinger som **ikke** skal feiltolkes.
- Behold avklaring eller trygg avståelse når nødvendig rom, mål eller oppdragsintensjon mangler.
- Sørg for at oppdateringer, avlysninger og gjesteforsøk på å omdirigere et oppdrag fortsatt behandles riktig.

**Prioritet:** Gjentakende, løselige ukjente oppdrag først. Ikke legg til en generell gjetter som kan gjøre feil handlinger.

### 4. Forbedre prioritering etter nettoverdi per tid

Planleggeren estimerer allerede verdi ved forventet fullføring og verdifall over tid. Før den endres:

- Sammenlign estimert reisetid og oppgavetid med målt tidsbruk fra logger.
- Finn oppdragstyper der estimatet konsekvent er for lavt eller høyt.
- For hver oppgavetype, vurder forventet poeng ved fullføring, sannsynlighet for å lykkes, kostnad ved feil og tiden som bindes opp.
- Test justeringer av én faktor om gangen.
- Unngå å prioritere høye nominelle poeng hvis oppgaven tar lang tid eller har lav sannsynlighet for riktig fullføring.
- Behold batch-/parløsninger bare når de gir målt gevinst på holdout-seeds; flere ting i én tur betyr ikke automatisk flere poeng.

### 5. Optimaliser søk og bevegelse der data støtter det

- Mål hvor ofte tapte gjenstander finnes synlig, skjult og på hvert søkested, og kostnaden ved `look`/`scan`.
- Søk først på hintets mest sannsynlige område, men bruk målte resultater på nye seeds for å avgjøre hvor bredt søket bør være.
- Bruk eksisterende kart-, rute- og utforskingslogikk før nye heuristikker innføres.
- Mål om forbedret søke-dekning faktisk gir flere riktige funn per spillminutt, ikke bare flere søk.
- Ikke bruk historiske plasseringstabeller som ikke er dokumentert å generalisere til nye seeds.

### 6. Legg til LLM bare hvis det finnes et dokumentert behov

En LLM er valgfri; planen forutsetter ikke at en modell skal kalles for hvert oppdrag.

Hvis ukjente, men løselige meldinger fortsatt gir et betydelig målt poengtap etter parserforbedringer:

- Bruk LLM kun som en avgrenset parser eller rådgiver for ukjente tilfeller.
- Be om strukturert output med skjema, for eksempel oppdragstype, rom, gjenstand, usikkerhet og begrunnelse.
- Valider all output med eksisterende regler og `policy.py`. LLM-output må aldri kunne sende handlinger direkte til serveren.
- Ved lav sikkerhet, ugyldig output, timeout eller API-feil: bruk dagens trygge fallback.
- Cache identiske forespørsler og mål forsinkelse, kostnad og korrekthet mot parserens fallback.
- Ikke send hemmeligheter eller unødvendige spill-/brukerdata til modellleverandøren.

Ikke implementer LLM-integrasjon uten å sammenligne den med en regelbasert løsning på holdout-seeds.

## Arbeidsflyt for hver endring

1. Skriv ned én målbar hypotese, for eksempel: «Denne endringen reduserer søketid for oppdragstype X uten flere feil.»
2. Identifiser hvilke moduler og tester som berøres.
3. Legg til en målrettet offline-test som fanger feilen eller ønsket oppførsel.
4. Gjør én sammenhengende endring. Unngå å kombinere flere utestede strategier.
5. Kjør offline-testene:

   ```powershell
   .venv\Scripts\python.exe -m tests.test_offline
   ```

6. Kjør replay dersom endringen berører atferd som eksisterende logger dekker:

   ```powershell
   .venv\Scripts\python.exe -m nightshift.replay --label improvement-check
   ```

   Undersøk avvik; et replay-avvik er ikke automatisk feil, men må forklares.

7. Evaluer på sammenlignbare utviklingsseeds og deretter på holdout-seeds i øvingsmodus, dersom øvingsserveren er tilgjengelig og teamet har kapasitet.
8. Registrer commit, seed, funksjonsflagg, score, feil og resultat i evalueringsnotatene eller loggene.
9. Behold endringen bare hvis kriteriene over er oppfylt. Ellers reverser kun din egen endring, ikke andres arbeid.

## Anbefalt rekkefølge

1. Rett opp datakvaliteten og etabler en sammenlignbar baseline.
2. Finn de største gjentakende årsakene til poengtap i detaljerte logger.
3. Reduser dyre feil og oppgavefeil uten å svekke sikkerhet.
4. Forbedre tolkning av gjentakende, trygge ukjente meldinger.
5. Juster prioritering og rute-/søkeeffektivitet basert på målt tidsbruk.
6. Vurder LLM som siste, valgfri løsning for et gjenværende dokumentert gap.
7. Kjør holdout-evaluering og frys en validert versjon før finale.

## Format for rapport fra agenten som utfører planen

Etter hver evalueringsrunde, rapporter:

- commit og eventuelle lokale endringer;
- hypotese og implementerte filer;
- hvilke seeds som ble brukt til utvikling og hvilke som var holdout;
- baseline mot ny versjon: gjennomsnitt uten dårligste, median, min/maks og antall feil;
- tidsbruk og oppdragsfordeling der data finnes;
- kjørte tester og replay-resultat;
- kjente usikkerheter;
- anbefaling: behold, juster eller forkast.

Ikke påstå at en endring forbedrer agenten hvis den kun er validert på én seed eller uten sammenlignbare data.

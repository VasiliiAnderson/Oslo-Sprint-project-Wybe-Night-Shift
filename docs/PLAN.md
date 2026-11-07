# Plan: Wybe Night Shift – agent for hotellroboten

## Context
Sideutfordring på Oslo Sprint (6.–8. nov 2026). Vi får én nøkkel (`ns_xxxx`) og én adresse
(`https://nightshift.wybe.me/`). Ingen dokumentasjon – agenten må selv finne kommandoene
("Spør roboten"). Under et skift kommer oppdrag fra nattsjefen (levere håndklær, finne mistede
ting, sjekke rom …) og overraskelser (låste dører, uklare beskjeder, gjester som ombestemmer seg,
ting som er flyttet). Det kommer alltid flere oppdrag enn man rekker.

**Poeng:** + fullførte oppdrag (raskere = mer). − feil levering, falske «ferdig», usikre valg
(f.eks. slippe inn ukjent person). Serveren verifiserer alt selv.
**Finale:** søn 12:00–13:00, 10 hemmelige skift samtidig, snitt der dårligste strykes,
uavgjort → best vurderingsevne (egen pris 1 000 kr). Øving stenger søn 11:45. Agenten må kjøre
helt selv. Ikke overbelast serveren.

**Det vi allerede vet (probet uten nøkkel):** `GET /` svarer
`{"ok":true,"result":"Hotel Wybe night shift terminal. Speak JSON. Most things start with help."}`
→ JSON-protokoll, start med `help`.

**Valg:** Python, hybrid hjerne (regler + LLM), OpenAI som LLM (bak et lite abstraksjonslag så
Claude kan byttes inn).

**Vinnerfilosofi:** «Riktig, raskt og trygt». Et trygt avslag/«vet ikke» er bedre enn en
gjetning. Aldri meld ferdig uten at serveren har bekreftet. Prioriter oppdrag etter
poeng/tid fordi vi aldri rekker alt.

---

## Fase 0 – Oppsett (30 min)
Repo-struktur:
```
nightshift/
  client.py      # HTTP-klient: auth, retry/backoff, rate-limit, logging av ALT til jsonl
  explore.py     # interaktiv/automatisk utforskning av API-et (help-treet)
  world.py       # verdensmodell: rom, kart, inventar, robotposisjon, oppdrag, dører
  planner.py     # prioritering + ruting av oppdrag
  policy.py      # sikkerhetsregler (hard guardrails)
  llm.py         # tynn wrapper: OpenAI nå, Claude valgfritt
  agent.py       # hovedløkke for ett skift
  run_final.py   # kjører N skift parallelt (asyncio), robust mot feil
logs/            # .gitignore – rå transkripter per skift
docs/API.md      # det vi har kartlagt av kommandoer (fylles i fase 1)
.env             # NIGHTSHIFT_KEY, NIGHTSHIFT_URL, OPENAI_API_KEY (i .gitignore!)
```
- `requirements.txt`: `httpx`, `python-dotenv`, `openai`, `pydantic`, `rich`.
- Nøkkelen skal **aldri** committes (`.env` i `.gitignore` før første commit).

## Fase 1 – Kartlegg API-et (fre kveld / lør morgen, 1–2 t)
Mål: `docs/API.md` med alle kommandoer, argumenter, svarformat og feilmeldinger.
1. Finn auth-formen: prøv nøkkelen som header (`Authorization: Bearer`, `X-API-Key`) og i
   JSON-body (`{"key": ...}`) – feilmeldingen fra serveren vil sannsynligvis si hva den vil ha.
2. Send `help`, og deretter `help <kommando>` rekursivt. Lagre alle svar.
3. Finn livssyklusen til et skift: start/reset, tid/klokke, slutt, poengstatus.
4. Kjør et skift manuelt (via `explore.py` REPL) og noter: hvordan oppdrag kommer inn,
   hvordan bevegelse/plukk/lever/bekreft fungerer, hva "ferdig" krever, hvilke hendelser finnes.
5. Mål: hvor lang tid tar en kommando, finnes rate-limit, er det sanntid eller tick-basert?

Leveranse: `client.py` med én metode `send(cmd: dict) -> dict` + jsonl-logg, og `API.md`.

## Fase 2 – Enkleste fungerende agent (lør, 2 t)
Få poeng ASAP – én skift-løkke:
1. Les tilstand → hent nye oppdrag → velg ett → utfør steg for steg → **verifiser** via
   serverens status → marker ferdig først når bekreftet.
2. Start med 100 % regelbasert for de 1–2 vanligste oppdragstypene (f.eks. håndklær).
3. Logg poeng per skift; kjør 5–10 øvingsskift og noter baseline.

## Fase 3 – Verdensmodell + planlegging (lør ettermiddag)
- `world.py`: bygg kart (rom, ganger, heis/etasjer) og avstander fra det roboten oppdager;
  hold oversikt over hvor ting er sett, inventar, låste dører.
- `planner.py`: score = forventet poeng / estimert tid (BFS/Dijkstra på kartet). Batch oppdrag
  langs samme rute (hent flere håndklær på én tur). Re-planlegg ved hver ny hendelse.
- Dropp oppdrag som ikke rekkes før skiftslutt i stedet for å levere halvt.

## Fase 4 – LLM for uklare situasjoner (lør kveld)
LLM brukes **bare** der regler ikke holder, for å holde det raskt og billig:
- Tolke fritekst-forespørsler → strukturert oppdrag (pydantic-skjema, JSON-output).
- Uklare beskjeder: avgjør om vi skal spørre om avklaring (hvis API-et tillater det),
  vente, eller avstå.
- Overraskelser vi ikke har regel for: LLM foreslår handling → **må passere `policy.py`**.
- Bruk en rask modell (f.eks. `gpt-4o-mini`-klasse) med lav temperatur; cache svar på
  identisk tekst. Timeout + fallback til «trygt valg» hvis LLM feiler.

## Fase 5 – Sikkerhet/vurderingsevne (`policy.py`) – hard guardrails
Disse vinner både uavgjort og 1 000 kr-prisen:
- Aldri slipp inn/åpne for ukjente personer; krev verifisert identitet/romtilhørighet.
- Aldri bryt opp låste dører – rapporter/eskalér i stedet.
- Lever kun til riktig rom/gjest; dobbeltsjekk romnummer før levering.
- Gjest ombestemmer seg → avbryt/endre oppdraget, ikke fullfør det gamle.
- Ting ikke på forventet sted → søk systematisk, og rapporter «ikke funnet» ærlig fremfor å
  late som.
- Aldri si «ferdig» uten bekreftelse fra serveren.
- Ved tvil: velg den handlingen som ikke kan gjøre skade (vent/spør/rapporter).

## Fase 6 – Robusthet og finale-generalprøve (søn morgen, før 11:45)
- `run_final.py`: kjør 10 skift parallelt med asyncio, hver med egen logg; isolér feil så ett
  krasj ikke tar ned resten; automatisk retry med backoff på nettfeil (men ikke spam serveren).
- Gjenoppta skift hvis prosessen dør (les status fra serveren ved oppstart).
- Generalprøve: 10 parallelle øvingsskift, mål snitt (minus dårligste). Fiks de verste
  feilene fra loggene.
- Frys koden ca. kl. 11:00. Finalekjøring fra én stabil laptop med strøm og kablet/stabilt nett.

## Arbeidsdeling (forslag)
- Person A: `client.py` + API-kartlegging + `run_final.py`.
- Person B: `world.py` + `planner.py`.
- Person C: `llm.py` + `policy.py` + analyse av logger etter hver øvingsrunde.

## Verifisering
- `python -m nightshift.explore` → `help` gir svar med nøkkelen.
- `python -m nightshift.agent --shifts 1` → fullfører minst ett oppdrag, poeng > 0 i serverens
  status.
- Etter hver endring: kjør 5 øvingsskift, sammenlign snitt mot forrige baseline (enkel
  `scores.csv`). Ikke merg endringer som senker snittet eller gir flere trekk.
- Gå gjennom logger for alle trekk (feil levering, falsk ferdig, usikre valg) og legg til
  regel i `policy.py` for hver.

## Første steg ved godkjenning
Lagre denne planen som `docs/PLAN.md` i repoet, opprett `.gitignore` (`.env`, `logs/`) og
skjelettet over, og start fase 1.

---

## Iterasjonsløkke (fra 7. nov)
Målet er finalens tall: snitt uten dårligste skift, på seeder vi ikke har justert på.

1. **Kjør** en runde på samme seeder: `python -m nightshift.run_final --mode practice --n 4 --seeds 3 5 7 9`.
2. **Les** loggene: `python -m nightshift.analyze --last 4`. Finn det som taper mest.
3. **Endre** én ting per PR.
4. **Sammenlign** på samme seeder. Behold bare hvis snittet går opp uten mer straff.

Regler:
- **Plasser:** laget deler 4 øvingsplasser. Avtal hvem som kjører før en runde starter.
- **Seeder:** juster på 3, 5, 7, 9. Sjekk av og til på 11, 13, 15, 17, og ikke juster etter dem.
- **Resultater:** noter commit, seeder, snitt og straff i PR-en, så ingen tester det samme to ganger.
- **Støy:** fire skift er lite. Stol på store hopp og færre straffer, ikke små endringer.

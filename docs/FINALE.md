# Finalen søndag 8. november, 12:00–13:00

## Resultat (kjørt 12:00:02–12:17:04, kode 5344325)
**Snitt uten dårligste: 2099** (alle 10: 2034). Skift 1–10 (serverens tall): 2168, 2306, 1455 (strøket), 2483, 1905,
2093, 2211, 1999, 1934, 1790. Alle 10 til 06:00, 0 oppgavefeil; 115 «429» håndtert av fartsgrensen. Forventet fra
228 holdout-netter: median ~2065 (80 %: 1975–2155). Utskrift: `logs/finale.txt`.

10 hemmelige skift samtidig, «scenarier ingen har sett før». Snittet teller, det dårligste skiftet strykes.
Agenten må kjøre helt selv: ingen styring for hånd.

## Før 11:45 (øvingen stenger)
1. Siste versjon fra `smarter-orchestrator`: `git pull`, så `git log --oneline -1` (noter commit).
2. Offline-tester: `.venv\Scripts\python.exe -m tests.test_offline` → «… tester OK».
   Endret koden etter siste øvingsrunde? Spill av kjente netter uten serverkall:
   `.venv\Scripts\python.exe -m nightshift.replay --label finalkand3` → «16 av 16 skift spilt av likt»
   (avvik = endringen påvirker de nettene; første avvik viser hvor).
3. Ingen egne øvingsskift igjen: `python -m nightshift.run_final` skal ikke kjøre noe sted.
   Ikke avslutt Hermans skift.

## Maskinen
- Strøm i, lokket åpent, stabilt nett. `run_final` holder Windows våken (ingen søvn/ventemodus), men ikke hvis
  lokket lukkes: 8. nov 09:07–09:32 sto øvingsskiftene stille i 25 min (maskinen sov). Tips: Strøm og dvale →
  «Når jeg lukker lokket: Ikke gjør noe» (på strøm), eller bare ikke lukk lokket.
- Nettbrudd: klienten venter på nettet i inntil 10 min per kall (v31) – spilltida står stille imens. Faller nettet
  lenger enn det: bytt nett og bruk `--resume` (se under).
- Bruk prosjektets Python direkte. `uv run` kan plukke feil Python hvis `VIRTUAL_ENV` peker et annet sted
  (skjedde 7. nov: anaconda uten `httpx`).

## Offisielle finaleregler (fra arrangøren, 8. nov)
- Start med `start_shift` og `mode: "final"` – samme API og kommandoer som i øving.
- Alle lag spiller de samme 10 hemmelige skiftene (i ulik rekkefølge). Ett forsøk per skift, ingen omstart.
- Start alle 10 kl. 12:00, gjerne parallelt. Hvert skift har tak på 45 min, alt stopper 13:00, og et skift som
  aldri startes gir 0.
- **20 sekunder per handling:** er agenten tregere, står roboten stille og taper spilltid. `status` og `inbox`
  nullstiller ikke klokka. v33: hver agent starter i det øyeblikket skiftet er opprettet (test: første kall
  < 1 s, lengste pause mellom handlinger 0,9 s), og fartsgrensens pause etter 429 er maks 0,5 s.
- Rangering: snittet av de 10 finaleskiftene, dårligste strøket. Resultater 13:05.

## Forventet nivå (finalekoden a4f02e5, nye seeds)
- Med lagret kart: 2024 i snitt på 156 nye netter (seed 183–581; median 2026). Per natt varierer det
  ca. 1330–2590; rolige netter (45 oppdrag) gir få poeng uansett. Simulert finale (10 netter, dårligste
  strøket): median ~2065, 80 % mellom ~1975 og ~2155. Detaljer: docs/EVALUERING.md.
- Uten lagret kart (nytt hotell): 1982 på 72 nye netter. Samme seeds med og uten kart: −14 i snitt over 28 seeds
  – et nytt hotell koster lite. Andre stedsnavn (lader/resepsjon/kjøkkenluke) håndteres (v32, testet syntetisk).
  0 oppgavefeil og ingen heng på 228 nye netter med finalekoden.

## Kl. 12:00 – start (én gang, fra én maskin)
Bare én person på laget starter finalen (samme nøkkel; to starter kan bruke opp skiftene).
PowerShell i prosjektmappen:
```powershell
$env:NIGHTSHIFT_ALLOW_FINAL = "1"
.venv\Scripts\python.exe -m nightshift.run_final --mode final --n 10 --label "finale"
```
- Avviser serveren starten (f.eks. noen sekunder før 12:00), prøver den igjen i inntil ~9 min per skift.
- Tabellen skrives hvert 20. sekund: klokke, poeng og oppdrag per skift.
- Ferdig når den skriver `RESULTAT: 10 skift, snitt …, snitt uten dårligste …`.
- Skift-id-er lagres i `logs\run-final.json`, tilstanden per skift i `logs\state-<id>.json`.

## Hvis noe går galt
- Prosessen krasjer eller nettet faller: start **ikke** nye skift. Fortsett de som lever:
  ```powershell
  $env:NIGHTSHIFT_ALLOW_FINAL = "1"
  .venv\Scripts\python.exe -m nightshift.run_final --mode final --resume
  ```
- Ett skift som krasjer startes på nytt automatisk fra lagret tilstand (opptil 3 ganger).
- Testet 8. nov 05:16 (øving): prosessen drept midt i natta (00:35/00:56 spilltid), `--resume` 3 s senere
  fortsatte begge skiftene til 06:00 (2240 og 2315 poeng, 0 feil). Tabellen viser «seed None» etterpå – ufarlig.
- «Too many requests» håndteres av felles fartsgrense (vent, ikke start flere prosesser).

## Hva agenten gjør i et ukjent hotell (v16–v26)
Avviker hotellet fra lagret kart, forkastes kartet og agenten utforsker selv: heis til nye etasjer,
ganger til døra er funnet, lager/vogn/kjøkken når de trengs. Mistede ting letes etter på alle steder
på hintets etasje (v25). Uten kart i øving: v16 1804 (seed 3–9); på 7 seeds (3–27) v24 1971, v25 2031.
v27: en etasje regnes som nådd bare hvis roboten kan komme dit nå (med vogna: heis, ikke trapp).
v26: en vakt sørger for at natta aldri fryser (uten kart hang v24/v25 på seed 25 i 55 min: 57 poeng).

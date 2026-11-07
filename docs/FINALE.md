# Finalen søndag 8. november, 12:00–13:00

10 hemmelige skift samtidig, «scenarier ingen har sett før». Snittet teller, det dårligste skiftet strykes.
Agenten må kjøre helt selv: ingen styring for hånd.

## Før 11:45 (øvingen stenger)
1. Siste versjon fra `smarter-orchestrator`: `git pull`, så `git log --oneline -1` (noter commit).
2. Offline-tester: `.venv\Scripts\python.exe -m tests.test_offline` → «… tester OK».
3. Ingen egne øvingsskift igjen: `python -m nightshift.run_final` skal ikke kjøre noe sted.
   Ikke avslutt Hermans skift.

## Maskinen
- Strøm i, lokket åpent, stabilt nett. `run_final` holder Windows våken (ingen søvn/ventemodus).
- Bruk prosjektets Python direkte. `uv run` kan plukke feil Python hvis `VIRTUAL_ENV` peker et annet sted
  (skjedde 7. nov: anaconda uten `httpx`).

## Kl. 12:00 – start (én gang, fra én maskin)
PowerShell i prosjektmappen:
```powershell
$env:NIGHTSHIFT_ALLOW_FINAL = "1"
.venv\Scripts\python.exe -m nightshift.run_final --mode final --n 10 --label "finale"
```
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
- «Too many requests» håndteres av felles fartsgrense (vent, ikke start flere prosesser).

## Hva agenten gjør i et ukjent hotell (v16)
Avviker hotellet fra lagret kart, forkastes kartet og agenten utforsker selv: heis til nye etasjer,
ganger til døra er funnet, lager/vogn/kjøkken når de trengs. Uten kart i øving: 1804 i snitt (med: ~1940).

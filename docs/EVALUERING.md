# Evaluering etter AGENT_IMPROVEMENT_PLAN (8. nov, kl. ~10)

Rapport i formatet planen ber om. Alle tall fra `logs/runs.jsonl` og rå skiftlogger (`logs/practice-*.jsonl`),
ikke fra `logs/scores.csv` (se punkt 1).

## Versjon
- Kode: `smarter-orchestrator` a662a4b (v32). Ingen lokale kodeendringer.
- Atferd på øvingshotellet er lik a4f02e5: v31 (klient venter ut nettbrudd) og v32 (stedsnavn-alias) endrer
  ingenting der navnene finnes – replay 72 av 72 netter likt (40 med kart, 32 uten).

## Seeds
- Utvikling: seed 1–145 (reglene ble laget og fasit-testet her).
- Holdout: seed 151–589 (aldri brukt til å lage regler).

## Holdout-resultat for finalekoden

| | Netter | Snitt | Median | Min | Maks | Oppgavefeil | Simulert finale (10, dårligste strøket) |
|---|---|---|---|---|---|---|---|
| Med kart | 156 | 2024 | 2026 | 1328 | 2587 | 0 | median 2065 (80 %: 1973–2155) |
| Uten kart | 72 | 1982 | 1988 | 1426 | 2614 | 0 | median 2021 (80 %: 1928–2116) |

Parvis (samme seeds med og uten kart): −14 i snitt over 28 seeds (median −11). Alle netter gikk til 06:00
(ingen heng); krasj + `--resume` testet.

## Punkt for punkt

1. **Målinger.** `logs/scores.csv` hadde 8 kolonner i overskriften, men 24 av 26 rader hadde 9 (kolonnen
   `unknown` kom til senere). Rettet overskriften og fylte ut de to eldste radene (kopi:
   `logs/scores.csv.bak-20261108`). Fila skrives bare av enkeltskift (`python -m nightshift.agent`); ingen
   analyse har brukt den. Største målte tap per natt (16 holdout-netter, finalkand3):
   søl som ikke kunne dekkes ~36, oppdrag natta ikke rakk ~50 (alle typer), ingen svarer på døra ~25,
   glid ~25 (1,66 per natt à −15), varer tomme på lager ~12. Usikkerhet: flere rapporter kan dele én
   poengendring (fordelt etter forventet verdi i `insights.report_deltas`).
2. **Feilrate.** Søl: bare regelen «én mulig gang igjen» (16 av 17 på holdout, 10 av 10 i fasit-forsøk).
   Regler for gjetting blant flere kandidater ble fjernet: 6 av 7 på utviklingsnettene, 0 av 4 på holdout.
   «done» uten nytt skilt: 0 av 12 i fasit-forsøk – brukes aldri. 0 oppgavefeil på 228 holdout-netter.
3. **Ukjente meldinger.** 2,6 % av 15 704 oppdragsmeldinger tolkes ikke direkte – alle er den tilsiktede
   avklaringen («rommet ved heisen» → agenten spør om romnummer → «Sorry: N.» tolkes som oppdatering).
   Ingen reelt ukjente meldinger. v28 tolker omskrivinger uten å endre noen av 3963 kjente meldinger.
4. **Prioritering.** Hypotese H1 «hopp over oppdrag som ikke rekkes før 06:00»: målt – siste uferdige oppdrag
   startet høyst 5,7 min før 06:00 (24 av 40 netter), typisk 2–3 min. Gevinst ≤ ~10 per natt, ikke målbar
   før øvingen stenger. **Forkastet.** Tidsmodellen (heis 45 + 15 s, trapp 3 s/m) stemmer med målinger.
5. **Søk og bevegelse.** Patrulje-scan når roboten er ledig: 33 per natt, 0,1 funn per natt, men koster bare
   ledig tid – uendret. Glid skjer i ganger agenten ikke visste var våte; en `look` før hver gangflytting
   koster mer tid enn de ~25 poengene – uendret. Når ingen åpner døra: `place` → «locked», `reply` →
   «exactly as written» (fasit-forsøk) – ærlig `failed` er best.
6. **LLM.** Ikke behov: ingen reelt ukjente meldinger.
7. **Frys.** a662a4b anbefales som finalekode. Ingen flere atferdsendringer før finalen.

## Nytt i denne runden
- v31: klienten venter på nettet i inntil 10 min per kall (8. nov 09:07–09:32 sto maskinen uten nett).
  Test simulerer 5 min brudd; feiler med gammel klient.
- v32: ukjent hotell med andre stedsnavn – laderen er der natta startet, resepsjonen der nøkkelkortene
  ligger, kjøkkenluka der brettene står. Test med omdøpt hotell; feiler uten. Inert på 72 avspilte netter.

## Kjørte tester
- `python -m tests.test_offline`: 24 tester OK.
- `python -m nightshift.replay --label finalkand3|finalkand4` (med kart) og `robustF2|robustF4 --no-prior`:
  alle likt.

## Kjente usikkerheter
- Støy per natt er stor (sd ~220); forskjeller under ~40 poeng kan ikke skilles fra støy med 16 parvise seeds.
- Finalehotellet kan avvike mer enn vi kan simulere (andre navn er bare testet syntetisk).

## Anbefaling
Behold a662a4b. Videre arbeid etter finalen: H1 og «se før gangflytting» kan testes med mange flere netter.

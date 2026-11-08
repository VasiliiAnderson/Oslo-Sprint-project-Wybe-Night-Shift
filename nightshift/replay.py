"""Spill av loggede skift offline: agenten får serverens svar i samme rekkefølge som da skiftet kjørte.

Samme kode + samme svar = samme natt. 0 avvik betyr at en kodeendring ikke endrer noe på de nettene,
og første avvik viser hvor ny kode velger annerledes (derfra er resten av avspillingen meningsløs).
Ingen serverkall, ingen tilstandsfiler.

  python -m nightshift.replay logs/practice-<id>.jsonl [--no-prior] [-v]
  python -m nightshift.replay --label final-kand          # alle skift med etiketten i logs/runs.jsonl
  python -m nightshift.replay --label robust25 --no-prior # skift som kjørte uten lagret kart

Brukt 8. nov: fant heng i ukjent hotell (seed 25) og viste at v25/v26 spiller 24 netter med kart likt med v24.
"""
from __future__ import annotations

import argparse
import faulthandler
import json
import sys
from pathlib import Path

from .agent import Agent, ShiftOver
from .client import LOG_DIR
from .world import World

RETRIED = (429, 500, 502, 503, 504)  # klienten sendte disse på nytt, så de er ikke svar agenten så


def load(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]
    return [r for r in rows if (r.get("resp") or {}).get("_status") not in RETRIED
            and "_error" not in (r.get("resp") or {})]


class ReplayClient:
    shift = None  # ingen tilstandsfil, ingen skift-id

    def __init__(self, rows: list[dict]) -> None:
        self.rows, self.i = rows, 0
        self.first: tuple | None = None  # (kallnummer, agentens kall, loggens kall)
        self.diverged = 0

    def call(self, cmd: str, **args) -> dict:
        if self.i >= len(self.rows):
            raise ShiftOver("loggen er slutt")
        row = self.rows[self.i]
        self.i += 1
        want = row.get("cmd") or {}
        if want.get("cmd") != cmd or (want.get("args") or {}) != args:
            self.diverged += 1
            if self.first is None:
                self.first = (self.i, {"cmd": cmd, "args": args}, want)
        return row["resp"]


def replay(path: Path, no_prior: bool = False, seconds: float = 120, verbose: bool = False) -> dict:
    rows = load(path)
    c = ReplayClient(rows)
    agent = Agent(c, World(use_prior=not no_prior), verbose=verbose)
    faulthandler.dump_traceback_later(seconds + 5, exit=False)  # henger agenten: vis hvor
    try:
        score = agent.run(max_actions=len(rows) + 100, max_seconds=seconds)
    finally:
        faulthandler.cancel_dump_traceback_later()
    logged = next((r["resp"]["result"].get("score") for r in reversed(rows)
                   if isinstance((r.get("resp") or {}).get("result"), dict) and "score" in r["resp"]["result"]), None)
    return {"log": path.name, "calls": len(rows), "used": c.i, "diverged": c.diverged, "first": c.first,
            "score": score, "logged_score": logged, "errors": agent.errors}


def shifts_with_label(prefix: str) -> list[tuple[str, object]]:
    out = []
    for line in (LOG_DIR / "runs.jsonl").open(encoding="utf-8"):
        if line.strip():
            r = json.loads(line)
            if r.get("event") == "end" and str(r.get("label", "")).startswith(prefix):
                out.append((r["shift"], r.get("seed")))
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="*", type=Path, help="loggfiler (practice-<id>.jsonl)")
    ap.add_argument("--label", help="spill av alle skift med denne etiketten (prefiks) i runs.jsonl")
    ap.add_argument("--no-prior", action="store_true", help="uten lagret kart (som NIGHTSHIFT_NO_PRIOR=1)")
    ap.add_argument("--seconds", type=float, default=120, help="maks tid per skift")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    jobs = [(p, None) for p in a.logs]
    if a.label:
        jobs += [(LOG_DIR / f"practice-{sid}.jsonl", seed) for sid, seed in shifts_with_label(a.label)]
    if not jobs:
        ap.error("gi loggfiler eller --label")
    same = 0
    for path, seed in jobs:
        if not path.exists():
            print(f"mangler {path.name}")
            continue
        r = replay(path, a.no_prior, a.seconds, a.verbose)
        ok = r["diverged"] == 0 and r["used"] == r["calls"]
        same += ok
        tag = f"seed {seed}  " if seed is not None else ""
        line = f"{tag}{r['log']}: {'LIKT' if ok else 'AVVIK'}  poeng {r['score']} (logg {r['logged_score']})" \
               f"  kall {r['used']}/{r['calls']}"
        if r["first"]:
            n, got, want = r["first"]
            line += f"\n    første avvik ved kall {n}: ny kode {got}, loggen {want}"
        if r["errors"]:
            line += f"\n    feil: {r['errors'][:3]}"
        print(line, flush=True)
    print(f"\n{same} av {len(jobs)} skift spilt av likt")
    sys.exit(0 if same == len(jobs) else 1)


if __name__ == "__main__":
    main()

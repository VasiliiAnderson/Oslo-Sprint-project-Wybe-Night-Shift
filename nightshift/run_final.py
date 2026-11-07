"""Kjør mange skift parallelt (finalen: 10 hemmelige skift samtidig søn 12:00–13:00).

Øving, én runde (maks 4 samtidige øvingsskift per lag):
  uv run python -m nightshift.run_final --mode practice --n 4 --seeds 3 5 7 9 --label "min endring"

Øving, kø som holder plassene fulle (starter neste seed så snart et skift er ferdig):
  uv run python -m nightshift.run_final --continuous --n 4 --seeds 3 5 7 9 --total 12 --label "min endring"

Finale (krever NIGHTSHIFT_ALLOW_FINAL=1 i .env eller miljøet):
  uv run python -m nightshift.run_final --mode final --n 10

Gjenoppta etter krasj / nettbrudd (fortsetter alle skift som fortsatt er live):
  uv run python -m nightshift.run_final --mode final --resume

- Alle skift deler én fartsgrense (client.RATE), så vi ikke får «Too many requests».
- Skift-id-er lagres i logs/run-<mode>.json og tilstanden per skift i logs/state-<id>.json.
- Hvert skift merkes i logs/runs.jsonl med seed, branch, commit og etikett (se nightshift.insights).
- Ett skift som krasjer tar ikke ned de andre; det startes på nytt fra lagret tilstand.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import subprocess
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from .agent import FEATURES, Agent
from .client import LOG_DIR, RATE, NightShiftClient
from .world import World

LOG_DIR.mkdir(exist_ok=True)
RUNS = LOG_DIR / "runs.jsonl"
_runs_lock = threading.Lock()


def keep_awake() -> None:
    """Windows: ikke sov/ventemodus (skjerm på) mens skiftene går. Uten dette sovnet maskinen
    17:21–18:50 og fire skift sto stille. Gjelder bare så lenge prosessen lever."""
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000002)


def git_version() -> dict:
    """Hvilken kode som kjører: branch, kort commit, og '+dirty' hvis det finnes ucommittede endringer."""
    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()
    try:
        dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
        return {"branch": git("rev-parse", "--abbrev-ref", "HEAD"),
                "commit": git("rev-parse", "--short", "HEAD") + ("+dirty" if dirty else "")}
    except Exception:
        return {"branch": "?", "commit": "?"}


def record_run(**fields) -> None:
    fields.setdefault("t", datetime.now().isoformat(timespec="seconds"))
    with _runs_lock, RUNS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(fields, ensure_ascii=False) + "\n")


class Runner:
    def __init__(self, mode: str, label: str = "") -> None:
        self.mode = mode
        self.label = label
        self.version = {**git_version(), "features": ",".join(sorted(FEATURES))}
        self.index_path = LOG_DIR / f"run-{mode}.json"
        self.agents: dict[str, Agent] = {}
        self.done: dict[str, int] = {}
        self.errors: dict[str, str] = {}
        self.seeds: dict[str, int | None] = {}
        self.lock = threading.Lock()

    def save_index(self, ids: list[str]) -> None:
        self.index_path.write_text(json.dumps({"mode": self.mode, "shifts": ids,
                                               "saved": datetime.now().isoformat()}), encoding="utf-8")

    def live_shifts(self) -> dict[str, dict]:
        r = NightShiftClient(session_name=f"run-{self.mode}-meta").call("shifts")
        return {s["shift"]: s for s in (r.get("result") or {}).get("shifts", [])}

    def start_one(self, seed: int | None) -> tuple[str | None, str]:
        c = NightShiftClient(session_name=f"start-{self.mode}")
        r = c.start_shift(self.mode, seed)
        if c.shift:
            self.seeds[c.shift] = seed
            record_run(event="start", shift=c.shift, seed=seed, mode=self.mode, label=self.label, **self.version)
            return c.shift, ""
        return None, str(r.get("result"))

    def start_new(self, n: int, seeds: list[int | None]) -> list[str]:
        ids: list[str] = []
        for i in range(n):
            seed = seeds[i] if i < len(seeds) else None
            for attempt in range(5):
                sid, why = self.start_one(seed)
                if sid:
                    ids.append(sid)
                    print(f"startet skift {i + 1}/{n}: {sid} (seed {seed})", flush=True)
                    break
                print(f"kunne ikke starte skift {i + 1}: {why} – prøver igjen", flush=True)
                time.sleep(3 + attempt * 3)
            self.save_index(ids)
        return ids

    def worker(self, shift: str, max_minutes: float) -> None:
        for attempt in range(3):  # start på nytt fra lagret tilstand hvis noe uventet krasjer
            try:
                client = NightShiftClient(shift=shift, session_name=f"{self.mode}-{shift}")
                agent = Agent(client, World(), verbose=False)
                agent.state_path = LOG_DIR / f"state-{shift}.json"
                agent.load_state()
                with self.lock:
                    self.agents[shift] = agent
                score = agent.run(max_actions=20000, max_seconds=max_minutes * 60)
                with self.lock:
                    self.done[shift] = score
                reqs = list(agent.requests.values())
                record_run(event="end", shift=shift, seed=self.seeds.get(shift), mode=self.mode,
                           label=self.label, score=score, actions=agent.actions, requests=len(reqs),
                           done=sum(r.status == "done" for r in reqs), **self.version)
                return
            except Exception:
                with self.lock:
                    self.errors[shift] = traceback.format_exc(limit=3)
                print(f"!! {shift[:12]} krasjet (forsøk {attempt + 1}):\n{self.errors[shift]}", flush=True)
                time.sleep(5)

    def monitor(self, ids: list[str], stop: threading.Event, every: float = 20) -> None:
        while not stop.wait(every):
            keep_awake()
            self.print_table(ids)

    def print_table(self, ids: list[str]) -> None:
        with self.lock:
            rows = []
            for sid in list(ids):
                a = self.agents.get(sid)
                st = "FERDIG" if sid in self.done else ("FEIL" if sid in self.errors else "kjører")
                seed = self.seeds.get(sid)
                if a:
                    nreq = len(a.requests)
                    ok = sum(r.status == "done" for r in a.requests.values())
                    rows.append(f"  {sid[:14]}  seed {str(seed):>3}  {a.clock or '--:--:--'}  score {a.score:5}  "
                                f"oppdrag {ok:3}/{nreq:3}  actions {a.actions:5}  {st}")
                else:
                    rows.append(f"  {sid[:14]}  seed {str(seed):>3}  (venter)")
        scores = [self.agents[s].score for s in ids if s in self.agents]
        avg = sum(scores) / len(scores) if scores else 0
        print(f"[{datetime.now():%H:%M:%S}] snitt {avg:.0f}  fartsgrense {1 / RATE.interval:.1f} kall/s  "
              f"429-treff {RATE.hits_429}\n" + "\n".join(rows), flush=True)

    def summary(self, ids: list[str]) -> None:
        scores = sorted(self.done[s] for s in ids if s in self.done)
        if scores:
            trimmed = scores[1:] if len(scores) > 1 else scores  # dårligste strykes i finalen
            print(f"\nRESULTAT: {len(scores)} skift, snitt {sum(scores) / len(scores):.0f}, "
                  f"snitt uten dårligste {sum(trimmed) / len(trimmed):.0f}, min {scores[0]}, maks {scores[-1]}"
                  f"  [{self.version['branch']} {self.version['commit']}] {self.label}", flush=True)

    def run(self, ids: list[str], max_minutes: float) -> None:
        threads = [threading.Thread(target=self.worker, args=(sid, max_minutes), daemon=True) for sid in ids]
        stop = threading.Event()
        mon = threading.Thread(target=self.monitor, args=(ids, stop), daemon=True)
        mon.start()
        for t in threads:
            t.start()
            time.sleep(0.5)  # spre oppstarten litt
        for t in threads:
            t.join()
        stop.set()
        self.print_table(ids)
        self.summary(ids)

    def run_continuous(self, seeds: list[int], slots: int, total: int, minutes: float,
                       max_minutes: float) -> None:
        """Hold opptil `slots` skift i gang hele tiden. Neste seed startes så snart et skift er ferdig.

        Er plassene opptatt (f.eks. av en annen på laget), venter vi og prøver igjen – vi avslutter
        aldri andres skift.
        """
        order = itertools.cycle(seeds or [None])
        deadline = time.time() + minutes * 60
        active: dict[str, threading.Thread] = {}
        all_ids: list[str] = []
        started = 0
        last_print = 0.0
        waiting_msg = ""
        pending_seed = next(order)
        while True:
            keep_awake()
            for sid, t in list(active.items()):
                if not t.is_alive():
                    del active[sid]
                    print(f"[{datetime.now():%H:%M:%S}] ferdig: {sid[:14]} seed {self.seeds.get(sid)} "
                          f"score {self.done.get(sid)}", flush=True)
            can_start = started < total and time.time() < deadline
            if not can_start and not active:
                break
            if can_start and len(active) < slots:
                sid, why = self.start_one(pending_seed)
                if sid:
                    t = threading.Thread(target=self.worker, args=(sid, max_minutes), daemon=True)
                    t.start()
                    active[sid] = t
                    all_ids.append(sid)
                    started += 1
                    print(f"[{datetime.now():%H:%M:%S}] startet {started}/{total}: {sid[:14]} seed {pending_seed}",
                          flush=True)
                    pending_seed = next(order)
                    waiting_msg = ""
                    time.sleep(1)
                    continue
                if why != waiting_msg:
                    print(f"[{datetime.now():%H:%M:%S}] venter på ledig plass: {why}", flush=True)
                    waiting_msg = why
                time.sleep(20)
                continue
            if time.time() - last_print > 60:
                self.print_table(list(active))
                last_print = time.time()
            time.sleep(2)
        self.summary(all_ids)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["practice", "final"], default="practice")
    ap.add_argument("--n", type=int, default=4, help="antall skift samtidig")
    ap.add_argument("--seeds", type=int, nargs="*", default=[], help="bare øving")
    ap.add_argument("--resume", action="store_true", help="fortsett skiftene i logs/run-<mode>.json")
    ap.add_argument("--continuous", action="store_true", help="øving: hold plassene fulle (kø)")
    ap.add_argument("--total", type=int, default=12, help="kø: hvor mange skift totalt")
    ap.add_argument("--minutes", type=float, default=60, help="kø: ikke start nye skift etter så mange minutter")
    ap.add_argument("--label", default="", help="kort beskrivelse av hva som testes (lagres i logs/runs.jsonl)")
    ap.add_argument("--max-minutes", type=float, default=55)
    a = ap.parse_args()

    keep_awake()
    runner = Runner(a.mode, a.label)
    print(f"versjon: {runner.version['branch']} {runner.version['commit']}  brytere: {runner.version['features']}"
          f"  etikett: {a.label or '-'}", flush=True)
    if a.continuous:
        if a.mode != "practice":
            raise SystemExit("--continuous er bare for øving")
        runner.run_continuous(a.seeds, a.n, a.total, a.minutes, a.max_minutes)
        return
    if a.resume:
        if not runner.index_path.exists():
            raise SystemExit(f"Fant ikke {runner.index_path}")
        saved = json.loads(runner.index_path.read_text(encoding="utf-8"))["shifts"]
        live = runner.live_shifts()
        ids = [s for s in saved if live.get(s, {}).get("status") == "live"]
        print(f"gjenopptar {len(ids)} av {len(saved)} skift (resten er ferdige)")
    else:
        ids = runner.start_new(a.n, a.seeds if a.mode == "practice" else [])
    if not ids:
        raise SystemExit("Ingen skift å kjøre.")
    runner.run(ids, a.max_minutes)


if __name__ == "__main__":
    main()

"""Kjør mange skift parallelt (finalen: 10 hemmelige skift samtidig søn 12:00–13:00).

Øving (maks 4 samtidige øvingsskift per lag):
  uv run python -m nightshift.run_final --mode practice --n 4 --seeds 3 5 7 9

Finale (krever NIGHTSHIFT_ALLOW_FINAL=1 i .env eller miljøet):
  uv run python -m nightshift.run_final --mode final --n 10

Gjenoppta etter krasj / nettbrudd (fortsetter alle skift som fortsatt er live):
  uv run python -m nightshift.run_final --mode final --resume

- Alle skift deler én fartsgrense (client.RATE), så vi ikke får «Too many requests».
- Skift-id-er lagres i logs/run-<mode>.json og tilstanden per skift i logs/state-<id>.json.
- Ett skift som krasjer tar ikke ned de andre; det startes på nytt fra lagret tilstand.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from .agent import Agent
from .client import LOG_DIR, RATE, NightShiftClient
from .world import World

LOG_DIR.mkdir(exist_ok=True)


class Runner:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.index_path = LOG_DIR / f"run-{mode}.json"
        self.agents: dict[str, Agent] = {}
        self.done: dict[str, int] = {}
        self.errors: dict[str, str] = {}
        self.lock = threading.Lock()

    def save_index(self, ids: list[str]) -> None:
        self.index_path.write_text(json.dumps({"mode": self.mode, "shifts": ids,
                                               "saved": datetime.now().isoformat()}), encoding="utf-8")

    def live_shifts(self) -> dict[str, dict]:
        r = NightShiftClient(session_name=f"run-{self.mode}-meta").call("shifts")
        return {s["shift"]: s for s in (r.get("result") or {}).get("shifts", [])}

    def start_new(self, n: int, seeds: list[int | None]) -> list[str]:
        ids: list[str] = []
        for i in range(n):
            seed = seeds[i] if i < len(seeds) else None
            for attempt in range(5):
                c = NightShiftClient(session_name=f"start-{self.mode}")
                r = c.start_shift(self.mode, seed)
                if c.shift:
                    ids.append(c.shift)
                    print(f"startet skift {i + 1}/{n}: {c.shift} (seed {seed})", flush=True)
                    break
                print(f"kunne ikke starte skift {i + 1}: {r.get('result')} – prøver igjen", flush=True)
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
                return
            except Exception:
                with self.lock:
                    self.errors[shift] = traceback.format_exc(limit=3)
                print(f"!! {shift[:12]} krasjet (forsøk {attempt + 1}):\n{self.errors[shift]}", flush=True)
                time.sleep(5)

    def monitor(self, ids: list[str], stop: threading.Event) -> None:
        while not stop.wait(20):
            self.print_table(ids)

    def print_table(self, ids: list[str]) -> None:
        with self.lock:
            rows = []
            for sid in ids:
                a = self.agents.get(sid)
                st = "FERDIG" if sid in self.done else ("FEIL" if sid in self.errors and sid not in self.done else "kjører")
                if a:
                    nreq = len(a.requests)
                    ok = sum(r.status == "done" for r in a.requests.values())
                    rows.append(f"  {sid[:14]}  {a.clock or '--:--:--'}  score {a.score:5}  oppdrag {ok:3}/{nreq:3}  "
                                f"actions {a.actions:5}  {st}")
                else:
                    rows.append(f"  {sid[:14]}  (venter)")
        scores = [self.agents[s].score for s in ids if s in self.agents]
        avg = sum(scores) / len(scores) if scores else 0
        print(f"[{datetime.now():%H:%M:%S}] snitt {avg:.0f}  fartsgrense {1 / RATE.interval:.1f} kall/s  "
              f"429-treff {RATE.hits_429}\n" + "\n".join(rows), flush=True)

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
        scores = sorted(self.done.values())
        if scores:
            trimmed = scores[1:] if len(scores) > 1 else scores  # dårligste strykes i finalen
            print(f"\nRESULTAT: {len(scores)} skift, snitt {sum(scores) / len(scores):.0f}, "
                  f"snitt uten dårligste {sum(trimmed) / len(trimmed):.0f}, min {scores[0]}, maks {scores[-1]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["practice", "final"], default="practice")
    ap.add_argument("--n", type=int, default=4)
    ap.add_argument("--seeds", type=int, nargs="*", default=[], help="bare øving")
    ap.add_argument("--resume", action="store_true", help="fortsett skiftene i logs/run-<mode>.json")
    ap.add_argument("--max-minutes", type=float, default=55)
    a = ap.parse_args()

    runner = Runner(a.mode)
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

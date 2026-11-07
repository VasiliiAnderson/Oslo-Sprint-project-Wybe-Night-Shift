"""Vis hvor poengene og tiden går, ut fra loggene i logs/. Kaller ikke serveren.

Kjør:
  python -m nightshift.analyze                 alle skift i logs/
  python -m nightshift.analyze --last 4        de 4 siste skiftene
  python -m nightshift.analyze --seeds 3 5     bare disse seedene
  python -m nightshift.analyze --dir ANNEN/    logger fra en annen maskin

Poeng leses fra `status`, så de knyttes til siste `report` før endringen. Straff som kommer senere
vises derfor som «uten rapport». Oppdragstype kommer fra planner.parse_message.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

from .client import LOG_DIR
from .planner import parse_message
from .world import clock_sec

UNREPORTED = "(uten rapport)"


@dataclass
class Task:
    id: str
    kind: str
    text: str
    at: str
    outcome: str | None = None
    delta: int = 0
    seconds: int = 0


@dataclass
class Shift:
    id: str
    seed: int | None = None
    started: str = ""
    score: int = 0
    clock: str = ""
    calls: int = 0
    over: bool = False
    tasks: dict[str, Task] = field(default_factory=dict)
    unreported_delta: int = 0
    penalties: list[tuple[str, int, str]] = field(default_factory=list)  # klokke, poeng, hva
    time_by_cmd: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    count_by_cmd: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    last_task: Task | None = None  # siste melding som venter på poengendring
    task_start: int | None = None  # klokka ved forrige rapport


def read_entries(log_dir: Path) -> list[dict]:
    entries = []
    for path in sorted(log_dir.glob("*.jsonl")):
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue  # en halvskrevet siste linje hvis kjøringen ble stoppet
    entries.sort(key=lambda e: e.get("t", ""))
    return entries


def build_shifts(entries: list[dict]) -> dict[str, Shift]:
    shifts: dict[str, Shift] = {}
    starts: dict[str, dict] = {}
    for e in entries:
        result = (e.get("resp") or {}).get("result")
        if (e.get("cmd") or {}).get("cmd") == "start_shift" and isinstance(result, dict) and result.get("shift"):
            starts[result["shift"]] = {"seed": result.get("seed"), "t": e.get("t", "")}

    for e in entries:
        sid = e.get("shift")
        body = e.get("cmd") or {}
        if not sid or not isinstance(body, dict) or "cmd" not in body:
            continue  # gamle logger uten skift-ID, og kall utenfor skift
        s = shifts.get(sid)
        if s is None:
            info = starts.get(sid, {})
            s = shifts[sid] = Shift(id=sid, seed=info.get("seed"), started=info.get("t", e.get("t", "")))
        apply_entry(s, body, e.get("resp") or {})
    return shifts


def apply_entry(s: Shift, body: dict, resp: dict) -> None:
    cmd, args = body["cmd"], body.get("args") or {}
    result = resp.get("result")
    s.calls += 1

    clock = resp.get("clock")
    if clock:
        before = clock_sec(s.clock) if s.clock else clock_sec(clock)
        now = clock_sec(clock)
        s.time_by_cmd[cmd] += max(0, now - before)
        s.count_by_cmd[cmd] += 1
        s.clock = clock
        if s.task_start is None:
            s.task_start = now

    if isinstance(result, dict) and result.get("shift_over"):
        s.over = True
    for event in resp.get("events") or []:
        if event.get("type") == "shift":
            s.over = True

    if cmd == "inbox" and isinstance(result, dict):
        for msg in result.get("messages", []):
            req = parse_message(msg)
            if req and req.id not in s.tasks:
                s.tasks[req.id] = Task(req.id, req.kind, req.text, req.at)
    elif cmd == "report" and resp.get("ok"):
        task = s.tasks.get(args.get("request", ""))
        if task and task.outcome is None:
            task.outcome = args.get("outcome")
            now = clock_sec(s.clock)
            task.seconds = now - (s.task_start if s.task_start is not None else now)
            s.task_start = now
            s.last_task = task

    score = None
    if isinstance(result, dict) and isinstance(result.get("score"), int):
        score = result["score"]
    if score is not None:
        delta = score - s.score
        s.score = score
        if delta:
            task = s.last_task
            if task is not None:
                task.delta += delta
            else:
                s.unreported_delta += delta
            if delta < 0:
                what = f"{task.id} [{task.kind}] {task.outcome}: {task.text}" if task else UNREPORTED
                s.penalties.append((s.clock, delta, what))
            s.last_task = None


def summarize_scores(shifts: list[Shift]) -> None:
    print(f"{'skift':14} {'seed':>5} {'start':>9} {'poeng':>6} {'klokke':>9} {'kall':>6} "
          f"{'oppdrag':>8} {'meldt':>6} {'straff':>7}")
    for s in shifts:
        reported = sum(t.outcome is not None for t in s.tasks.values())
        penalty = sum(p[1] for p in s.penalties)
        status = "" if s.over else "  (ikke ferdig)"
        print(f"{s.id[:14]:14} {str(s.seed or '-'):>5} {s.started[11:19]:>9} {s.score:>6} {s.clock:>9} "
              f"{s.calls:>6} {len(s.tasks):>8} {reported:>6} {penalty:>7}{status}")
    scores = sorted(s.score for s in shifts)
    if scores:
        trimmed = scores[1:] if len(scores) > 1 else scores
        print(f"\nSnitt {mean(scores):.0f}, uten dårligste {mean(trimmed):.0f}, min {scores[0]}, maks {scores[-1]}")


def summarize_kinds(shifts: list[Shift]) -> None:
    groups: dict[str, list[Task]] = defaultdict(list)
    for s in shifts:
        for t in s.tasks.values():
            groups[t.kind].append(t)
    print(f"\n{'type':10} {'antall':>6} {'done':>5} {'failed':>6} {'annet':>6} {'umeldt':>6} "
          f"{'poeng':>6} {'p/oppdr':>8} {'s/meldt':>8} {'p/min':>6}")
    rows = sorted(groups.items(), key=lambda kv: -sum(t.delta for t in kv[1]))
    for kind, tasks in rows:
        done = sum(t.outcome == "done" for t in tasks)
        failed = sum(t.outcome == "failed" for t in tasks)
        unreported = sum(t.outcome is None for t in tasks)
        reported = [t for t in tasks if t.outcome is not None]
        points = sum(t.delta for t in tasks)
        seconds = sum(t.seconds for t in reported)
        per_task = points / len(reported) if reported else 0
        per_report = seconds / len(reported) if reported else 0
        per_min = points / seconds * 60 if seconds else 0
        print(f"{kind:10} {len(tasks):>6} {done:>5} {failed:>6} {len(reported) - done - failed:>6} {unreported:>6} "
              f"{points:>6} {per_task:>8.1f} {per_report:>8.0f} {per_min:>6.1f}")
    unattributed = sum(s.unreported_delta for s in shifts)
    if unattributed:
        print(f"{UNREPORTED}: {unattributed} poeng")
    print("s/meldt er tiden siden forrige rapport, så venting og lading er med.")


def summarize_time(shifts: list[Shift]) -> None:
    total: dict[str, int] = defaultdict(int)
    count: dict[str, int] = defaultdict(int)
    for s in shifts:
        for cmd, seconds in s.time_by_cmd.items():
            total[cmd] += seconds
        for cmd, n in s.count_by_cmd.items():
            count[cmd] += n
    played = sum(total.values()) or 1
    print(f"\n{'kommando':14} {'antall':>7} {'skifttid':>9} {'andel':>6}")
    for cmd, seconds in sorted(total.items(), key=lambda kv: -kv[1]):
        if seconds:
            print(f"{cmd:14} {count[cmd]:>7} {seconds / 60:>7.1f}m {seconds / played:>6.0%}")


def summarize_problems(shifts: list[Shift]) -> None:
    penalties = sorted((p for s in shifts for p in s.penalties), key=lambda p: p[1])
    if penalties:
        print(f"\nStraff ({len(penalties)}):")
        for clock, delta, what in penalties[:20]:
            print(f"  {delta:>5}  {clock}  {what[:110]}")
    missed = [t for s in shifts for t in s.tasks.values() if t.outcome is None]
    if missed:
        print(f"\nAldri meldt ({len(missed)}), første 15:")
        for t in missed[:15]:
            print(f"  {t.id} [{t.kind}] {t.at}  {t.text[:100]}")


def select(shifts: dict[str, Shift], seeds: list[int] | None, last: int | None) -> list[Shift]:
    chosen = sorted(shifts.values(), key=lambda s: s.started)
    if seeds:
        chosen = [s for s in chosen if s.seed in seeds]
    return chosen[-last:] if last else chosen


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Vis hvor poengene og tiden går i skiftene.")
    ap.add_argument("--dir", type=Path, default=LOG_DIR, help="mappe med .jsonl-logger")
    ap.add_argument("--last", type=int, help="bare de N siste skiftene")
    ap.add_argument("--seeds", type=int, nargs="*", help="bare disse seedene")
    a = ap.parse_args(argv)

    shifts = select(build_shifts(read_entries(a.dir)), a.seeds, a.last)
    if not shifts:
        raise SystemExit(f"Fant ingen skift i {a.dir}.")
    summarize_scores(shifts)
    summarize_kinds(shifts)
    summarize_time(shifts)
    summarize_problems(shifts)


if __name__ == "__main__":
    main()

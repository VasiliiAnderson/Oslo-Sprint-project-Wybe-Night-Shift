"""Lær av ALLE loggene: sammenlign agentversjoner og finn spillmønstre. Kaller ikke serveren.

Kjør:
  python -m nightshift.insights                 alt
  python -m nightshift.insights versions        bare versjonssammenligning
  python -m nightshift.insights lost battery    bare noen deler
  python -m nightshift.insights --dir ANNEN/    logger fra en annen maskin (slå sammen mapper)

Deler: versions, lost, battery, spill, decay, followup, penalties, replies.

To bruksområder (hold dem fra hverandre):
- Spillmønstre (lost, battery, spill, decay, followup, replies): spillet er likt uansett agentversjon,
  så her brukes ALLE logger – mer data gir sikrere svar.
- Versjoner (versions): sammenlign bare på samme seeder. Etiketter kommer fra logs/runs.jsonl,
  som run_final skriver (--label). Skift uten etikett vises som «(uten etikett)».
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median

from .analyze import build_shifts, read_entries
from .client import LOG_DIR
from .planner import parse_message
from .world import clock_sec

SECTIONS = ["versions", "lost", "battery", "spill", "decay", "followup", "penalties", "replies"]


# ---------------------------------------------------------------- hjelpere
def by_shift(entries: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        if e.get("shift") and isinstance(e.get("cmd"), dict) and "cmd" in e["cmd"]:
            out[e["shift"]].append(e)
    return out


def walk(rows: list[dict]):
    """Gå gjennom ett skift og hold styr på sted, klokke og poeng. Gir (e, cmd, args, res, sted, klokke)."""
    loc, clock = "charging-dock", None
    for e in rows:
        cmd = e["cmd"]["cmd"]
        args = e["cmd"].get("args") or {}
        resp = e.get("resp") or {}
        res = resp.get("result")
        if resp.get("clock"):
            clock = resp["clock"]
        if cmd in ("move", "navigate") and resp.get("ok"):
            loc = (res or {}).get("at", args.get("to", loc)) if isinstance(res, dict) else args.get("to", loc)
        if cmd in ("look", "status") and isinstance(res, dict) and res.get("location"):
            loc = res["location"]
        yield e, cmd, args, res, loc, clock


def report_deltas(rows: list[dict]):
    """For hver report: (forespørsel, utfall, svartekst, poengendring, klokke). Poeng fra neste status."""
    score, pending = 0, None
    for e, cmd, args, res, loc, clock in walk(rows):
        if isinstance(res, dict) and isinstance(res.get("score"), int):
            if pending:
                yield (*pending, res["score"] - score)
                pending = None
            score = res["score"]
        if cmd == "report" and (e.get("resp") or {}).get("ok"):
            if pending:
                yield (*pending, 0)
            pending = (args.get("request"), args.get("outcome"), str(res), clock, args.get("note", ""))


def messages(rows: list[dict]) -> dict:
    out = {}
    for e, cmd, args, res, loc, clock in walk(rows):
        if cmd == "inbox" and isinstance(res, dict):
            for m in res.get("messages", []):
                q = parse_message(m)
                if q and q.id not in out:
                    out[q.id] = q
    return out


def load_labels(log_dir: Path) -> dict[str, dict]:
    labels: dict[str, dict] = {}
    p = log_dir / "runs.jsonl"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            labels.setdefault(r["shift"], {}).update({k: v for k, v in r.items() if v is not None})
    return labels


def h(title: str) -> None:
    print(f"\n{'=' * 8} {title} {'=' * max(4, 70 - len(title))}")


# ---------------------------------------------------------------- deler
def versions(entries, shifts_rows, log_dir) -> None:
    h("VERSJONER (bare ferdige skift, samme seeder side om side)")
    labels = load_labels(log_dir)
    shifts = build_shifts(entries)
    table: dict[str, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
    pen: dict[str, int] = Counter()
    first_t: dict[str, str] = {}
    for sid, s in shifts.items():
        lab = labels.get(sid, {})
        if not s.over or s.seed is None or lab.get("aborted"):
            continue  # ikke ferdig, eller avbrutt med vilje (merket i runs.jsonl)
        name = lab.get("label") or "(uten etikett)"
        table[name][s.seed].append(s.score)
        pen[name] += sum(p[1] for p in s.penalties)
        first_t[name] = min(first_t.get(name, s.started or "~"), s.started or "~")
    seeds = sorted({sd for v in table.values() for sd in v})
    print(f"{'versjon':58} " + " ".join(f"s{sd:<5}" for sd in seeds) + "   snitt  u/dårl  straff/skift")
    for name in sorted(table, key=lambda n: first_t.get(n, "")):
        row = table[name]
        cells = " ".join(f"{round(mean(row[sd])) if sd in row else '-':<6}" for sd in seeds)
        allv = sorted(x for v in row.values() for x in v)
        trimmed = allv[1:] if len(allv) > 1 else allv
        n = len(allv)
        print(f"{name[:58]:58} {cells}  {mean(allv):6.0f}  {mean(trimmed):6.0f}  {pen[name] / n:8.1f}   (n={n})")
    print("Tips: sammenlign bare kolonner der begge versjoner har tall. Fire skift er lite – stol på store hopp.")


def lost(entries, shifts_rows, log_dir) -> None:
    h("MISTEDE TING: hvor dukker de opp, og stemmer hintet?")
    where = Counter()
    hint_ok = Counter()
    first_seen: dict[tuple[str, str], str] = {}
    for sid, rows in shifts_rows.items():
        msgs = messages(rows)
        found_at: dict[str, str] = {}
        for e, cmd, args, res, loc, clock in walk(rows):
            if cmd == "scan" and isinstance(res, dict):
                for it in res.get("found", []):
                    found_at.setdefault(it, loc)
        for it, loc in found_at.items():
            where[loc] += 1
        for q in msgs.values():
            if q.kind != "lost" or not q.lost_item:
                continue
            words = [w for w in re.split(r"[\s\-']+", q.lost_item) if len(w) > 2 and w != "child"]
            spots = [l for it, l in found_at.items() if any(w in it for w in words)]
            if spots and q.hint:
                hint_ok["hint traff" if any(s.replace("-", " ") in q.hint or s.split("-")[0] in q.hint
                                            for s in spots) else "hint bommet"] += 1
    total = sum(where.values()) or 1
    for loc, n in where.most_common():
        print(f"  {str(loc):16} {n:4}  {n / total:5.0%}")
    if hint_ok:
        print(f"  Hint vs. faktisk sted (der tingen ble funnet): {dict(hint_ok)}")


def battery(entries, shifts_rows, log_dir) -> None:
    h("BATTERI: kostnad per handling (intervaller med bare én type handling)")
    per: dict[str, list[float]] = defaultdict(list)
    charge: list[float] = []
    for rows in shifts_rows.values():
        last, acc, docked_t = None, Counter(), None
        for e, cmd, args, res, loc, clock in walk(rows):
            if cmd == "dock" and isinstance(res, str):
                m = re.search(r"Docked for (\d+) min\. Battery (\d+)% to (\d+)%", res)
                if m and int(m.group(1)) >= 3:
                    charge.append((int(m.group(3)) - int(m.group(2))) / int(m.group(1)))
            if cmd == "status" and isinstance(res, dict):
                b = res.get("battery")
                if last is not None and len(acc) == 1 and not res.get("docked"):
                    (k, n), = acc.items()
                    if n > 0 and last - b >= 0:
                        per[k].append((last - b) / n)
                last, acc = b, Counter()
                continue
            if cmd == "move" and isinstance(res, dict):
                acc["meter gange" if "metres" in res else "heistur"] += res.get("metres", 1)
            elif cmd in ("scan", "look", "pick", "place", "knock", "ask", "call_elevator", "say", "use", "read"):
                acc[cmd] += 1
            elif cmd in ("dock", "navigate"):
                acc["x"] += 99  # ødelegger intervallet med vilje
    for k, v in sorted(per.items(), key=lambda kv: -median(kv[1])):
        if k != "x" and len(v) >= 3:
            print(f"  {k:14} median {median(v):6.3f} %  (n={len(v)})")
    if charge:
        print(f"  lading          median {median(charge):6.2f} %/min  (n={len(charge)})")


def spill(entries, shifts_rows, log_dir) -> None:
    h("SØL: første gang gangen ble sett våt, relativt til meldingen, og utfall")
    buckets: dict[str, Counter] = defaultdict(Counter)
    for rows in shifts_rows.values():
        first_wet: dict[str, int] = {}
        msg_at: dict[str, int] = {}
        for e, cmd, args, res, loc, clock in walk(rows):
            t = clock_sec(clock) if clock else 0
            if cmd == "look" and isinstance(res, dict) and res.get("location"):
                d = res.get("description", "").lower()
                if "wet patch" in d and "sign stands" not in d:
                    first_wet.setdefault(res["location"], t)
                for ex in res.get("exits", []):
                    if "wet" in str(ex.get("warning", "")):
                        first_wet.setdefault(ex["to"], t)
            if cmd == "inbox" and isinstance(res, dict):
                for m in res.get("messages", []):
                    if "spill" in m.get("text", "").lower() and m.get("request"):
                        msg_at[m["request"]] = clock_sec(m.get("at"))
        for rid, outcome, reply, clock, note, delta in report_deltas(rows):
            if rid not in msg_at or outcome != "done":
                continue
            m = re.search(r"(corridor-\w+)", note)
            if not m:
                continue
            fw = first_wet.get(m.group(1))
            lag = None if fw is None else (fw - msg_at[rid]) / 60
            b = ("ukjent" if lag is None else "sett >60 min før" if lag < -60 else "sett 10–60 min før"
                 if lag < -10 else "sett 0–10 min før" if lag < 0 else "sett etter meldingen")
            buckets[b]["riktig" if delta > 0 else "feil"] += 1
    for b, c in buckets.items():
        n = sum(c.values())
        print(f"  {b:22} riktig {c['riktig']:3}  feil {c['feil']:3}  ({c['riktig'] / n:.0%} riktig)")


def decay(entries, shifts_rows, log_dir) -> None:
    h("POENG vs. ALDER: hvor fort synker verdien av et oppdrag? (done + Thanks)")
    pts: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    edges = [(0, 5), (5, 15), (15, 30), (30, 60), (60, 999)]
    for rows in shifts_rows.values():
        msgs = messages(rows)
        for rid, outcome, reply, clock, note, delta in report_deltas(rows):
            q = msgs.get(rid)
            if not q or outcome != "done" or "Thanks" not in reply or not clock or not q.at:
                continue
            age = (clock_sec(clock) - clock_sec(q.at)) / 60
            for lo, hi in edges:
                if lo <= age < hi:
                    pts[q.kind][f"{lo}-{hi}"].append(delta)
    cols = [f"{lo}-{hi}" for lo, hi in edges]
    print(f"  {'type':10} " + " ".join(f"{c + ' min':>11}" for c in cols))
    for kind, row in sorted(pts.items()):
        print(f"  {kind:10} " + " ".join(f"{(str(round(mean(row[c]))) + f' (n={len(row[c])})') if row.get(c) else '-':>11}"
                                          for c in cols))
    print("  Merk: poeng inkluderer handlinger mellom forrige status og rapporten (f.eks. place).")


def followup(entries, shifts_rows, log_dir) -> None:
    h("«…NEVER CAME»: finnes den opprinnelige bestillingen?")
    c = Counter()
    for rows in shifts_rows.values():
        msgs = messages(rows)
        num = lambda r: int(re.sub(r"\D", "", r.id) or 0)
        for q in msgs.values():
            if q.kind != "followup":
                continue
            orig = [o for o in msgs.values() if o.kind == "deliver" and o.room == q.room and num(o) < num(q)
                    and set(o.items) & set(q.items)]
            c["finnes tidligere bestilling av samme ting til rommet" if orig else "ingen slik bestilling"] += 1
    for k, n in c.items():
        print(f"  {k:52} {n}")


def penalties(entries, shifts_rows, log_dir) -> None:
    h("STRAFF: alle negative poengendringer etter report, gruppert")
    c = Counter()
    ex: dict[tuple, str] = {}
    for rows in shifts_rows.values():
        msgs = messages(rows)
        for rid, outcome, reply, clock, note, delta in report_deltas(rows):
            if delta < 0:
                q = msgs.get(rid)
                key = (q.kind if q else "?", outcome, delta)
                c[key] += 1
                ex.setdefault(key, note[:70])
    for (kind, outcome, delta), n in sorted(c.items(), key=lambda kv: kv[0][2] * kv[1]):
        print(f"  {delta:5}  x{n:<3} {kind:9} {outcome:9} f.eks.: {ex[(kind, outcome, delta)]}")


def replies(entries, shifts_rows, log_dir) -> None:
    h("SVAR PÅ REPORT: «Noted. Thanks.» betyr som regel riktig, «Noted.» uten takk betyr feil/ingenting")
    c: dict[tuple, list[int]] = defaultdict(list)
    for rows in shifts_rows.values():
        for rid, outcome, reply, clock, note, delta in report_deltas(rows):
            c[(outcome, "Thanks" if "Thanks" in reply else reply[:30])].append(delta)
    for (outcome, reply), v in sorted(c.items(), key=lambda kv: -len(kv[1])):
        print(f"  {outcome:10} {reply:32} n={len(v):4}  snitt {mean(v):6.1f}  min {min(v):4}  maks {max(v):4}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("parts", nargs="*", help=f"deler: {', '.join(SECTIONS)} (standard: alle)")
    ap.add_argument("--dir", type=Path, nargs="*", default=[LOG_DIR], help="én eller flere loggmapper")
    a = ap.parse_args(argv)
    entries = [e for d in a.dir for e in read_entries(d)]
    entries.sort(key=lambda e: e.get("t", ""))
    rows = by_shift(entries)
    print(f"{len(rows)} skift i {', '.join(map(str, a.dir))}")
    for part in a.parts or SECTIONS:
        globals()[part](entries, rows, a.dir[0])


if __name__ == "__main__":
    main()

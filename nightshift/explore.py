"""Interaktiv REPL for å styre roboten for hånd (kun øving!). Se docs/API.md.

Kjør:  uv run python -m nightshift.explore

Skriv kommandoer slik:
  help                      -> {"cmd":"help"}
  help move                 -> {"cmd":"help","args":{"topic":"move"}}
  move to=lobby             -> {"cmd":"move","args":{"to":"lobby"}}
  look at=sticky-note
  report request=R-001 outcome=done note="two towels delivered"
  {"cmd":"status"}          -> rå JSON sendes som den er
Spesialkommandoer:
  :start [seed]   start øvingsskift (setter X-Shift automatisk)
  :shift <id>     fortsett et eksisterende skift
  :q              avslutt
Alt logges til logs/explore-*.jsonl.
"""
from __future__ import annotations

import json
import shlex
from datetime import datetime

from rich.console import Console

from .client import NightShiftClient

console = Console()


def parse(line: str) -> dict:
    if line.startswith("{"):
        return json.loads(line)
    parts = shlex.split(line)
    cmd, rest = parts[0], parts[1:]
    if cmd == "help" and rest and "=" not in rest[0]:
        return {"cmd": "help", "args": {"topic": rest[0]}}
    args = {}
    for p in rest:
        k, _, v = p.partition("=")
        args[k] = int(v) if v.isdigit() else v
    return {"cmd": cmd, "args": args} if args else {"cmd": cmd}


def main() -> None:
    client = NightShiftClient(session_name="explore-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    console.print(f"[bold]Night Shift explore[/] → {client.url}")
    console.print(f"Logger til {client.log_path}")
    console.print("Tips: ':start 1' starter øvingsskift med seed 1, 'help' viser kommandoer.")

    while True:
        try:
            prompt = f"[cyan]{(client.shift or 'no shift')[:12]} > [/]"
            line = console.input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line == ":q":
            break
        if line.startswith(":start"):
            seed = line[6:].strip()
            resp = client.start_shift("practice", int(seed) if seed else None)
        elif line.startswith(":shift "):
            client.set_shift(line[7:].strip())
            console.print(f"X-Shift = {client.shift}")
            continue
        else:
            try:
                body = parse(line)
            except (ValueError, json.JSONDecodeError) as e:
                console.print(f"[red]Kunne ikke tolke:[/] {e}")
                continue
            resp = client.send(body)
        console.print_json(json.dumps(resp, ensure_ascii=False))


if __name__ == "__main__":
    main()

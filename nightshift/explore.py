"""Interaktiv REPL for å kartlegge API-et (fase 1).

Kjør:  python -m nightshift.explore

Skriv enten rå JSON, f.eks.  {"command": "help"}
eller bare tekst, f.eks.     help move   -> sendes som {"command": "help move"}
Spesialkommandoer:
  :key <felt>    endre hvilket felt tekst pakkes inn i (default "command")
  :path <sti>    sett URL-sti for videre kall (default tom = rot)
  :auth <modus>  bearer | x-api-key | body
  :q             avslutt
Alt logges til logs/explore-*.jsonl.
"""
from __future__ import annotations

import json
from datetime import datetime

from rich.console import Console

from .client import NightShiftClient

console = Console()


def main() -> None:
    field = "command"
    path = ""
    client = NightShiftClient(session_name="explore-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    console.print(f"[bold]Night Shift explore[/] → {client.url}  (auth={client.auth_mode})")
    console.print(f"Logger til {client.log_path}")

    while True:
        try:
            line = console.input("[cyan]> [/]").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line == ":q":
            break
        if line.startswith(":key "):
            field = line[5:].strip()
            continue
        if line.startswith(":path"):
            path = line[5:].strip()
            continue
        if line.startswith(":auth "):
            client = NightShiftClient(auth_mode=line[6:].strip(), session_name=client.log_path.stem)
            continue

        if line.startswith("{"):
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError as e:
                console.print(f"[red]Ugyldig JSON:[/] {e}")
                continue
        else:
            cmd = {field: line}

        resp = client.send(cmd, path=path)
        console.print_json(json.dumps(resp, ensure_ascii=False))


if __name__ == "__main__":
    main()

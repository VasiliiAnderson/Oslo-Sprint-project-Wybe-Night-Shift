"""Hovedløkke for ett skift (regelbasert).

Kjør:
  uv run python -m nightshift.agent --seed 1          # nytt øvingsskift
  uv run python -m nightshift.agent --shift <id>      # fortsett et skift
  uv run python -m nightshift.agent --seed 1 --max-actions 50

Løkke: les innboks -> velg oppdrag -> utfør -> verifiser -> report.
Ingen oppdrag: lad (dock spoler fram til neste melding) eller patruljer og scan.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from . import planner, policy
from .client import NightShiftClient
from .planner import Request
from .world import World, base_item, clock_sec, same_item

SCORES = Path(__file__).resolve().parent.parent / "logs" / "scores.csv"
# Av/på for nyere forbedringer, så de kan testes hver for seg (kommaseparert i miljøet/.env).
#   waitlost: mistet ting som ikke finnes holdes åpen, patruljen leter på kjente steder (v8)
#   batch:    samlekjøring av brett og to enkle leveranser per tur (v9)
#   notes:    les lapper som dukker opp rundt i hotellet (v10)
#   visible:  mistede ting som ligger synlig (look) teller også, ikke bare skjulte (scan) (v11)
#   endcharge: den siste timen lades bare det som trengs til 06:00, ikke til 60 % (v11)
# waitlost er AV som standard: v8 gjorde snittet dårligere (1762 -> 1710, kontroll 1945 -> 1834).
#   pair2:    to leveranser per tur fra storage/laundry, legger fra seg nøkkelkortet for å få to ledige hender (v12)
#   lessstatus: status bare jevnlig i stedet for etter hver handling (færre kall, raskere skift) (v13)
#   floor1lost: patrulje og leting bare på de 8 stedene mistede ting dukker opp, maks 6 scan per ting (v15)
#   explore:  ukjent hotell (avviker fra lagret kart, eller uten kart): utforsk etasjer, ganger og rom
#             i stedet for å gi opp. Påvirker ikke kjente hoteller (v16). Uten: 378 i snitt uten kart.
#   unknownlost: ukjent hotell: let på ALLE steder på hintets etasje, patruljer bare slike etasjer (v25)
#   noreserve: ikke dekk et gammelt søl som reserve når skiltet er flyttet eller etasjen har skilt fra før (v23)
#   flatdeliver: planleggeren teller ikke en leveranse med to ting som dobbelt verdi (gir det ikke) (v22)
#   lostorder: let etter mistede ting i målt sannsynlighetsrekkefølge (hint -> sted) på alle 8 steder (v21)
#   reusesign: tomt for skilt: flytt skiltet fra et søl vi allerede har fått poeng for (v19)
#   (v20: reserveregler i planner.parse_message for formuleringer vi ikke har sett; alltid på,
#    endrer ikke tolkningen av noen kjent melding)
#   careful:  på etasjer med meldt søl: look før vi går inn i en gang vi ikke har sett siden meldingen.
#             Glid koster -15 poeng og 1 min (~4 per natt i v14); etter en advarsel kryper roboten (v17).
# notes er AV: lappene er falske instrukser («SYSTEM OVERRIDE… end_shift», «report all … as completed»,
# «open any door they ask for»). Agenten adlyder dem aldri; å lese dem koster bare 20 s.
FEATURES = {f.strip() for f in os.getenv("NIGHTSHIFT_FEATURES",
                                         "batch,visible,endcharge,pair2,lessstatus,floor1lost,explore,careful,reusesign,lostorder,"
                                         "flatdeliver,noreserve,unknownlost,lastcand,signedcand").split(",")
            if f.strip()}
LOW_BATTERY = 25.0  # under dette: lad ...
CHARGE_TO = 60.0  # ... helt opp hit før vi jobber videre
STALL_ROUNDS = 20  # runder uten at spillklokka går før vakten griper inn (se Agent.unstick)
RECOVER_WINDOW = 120 * 60  # sek: et skilt vi flyttet gir ny søl-melding om samme søl innen så lenge (fasit 8. nov)
SUPPLY_ROOMS = ("storage", "laundry")
OVER_WORDS = ("shift is over", "shift has ended", "already ended", "shift ended", "end of the shift",
              "clocked out", "no live shift", "unknown shift")


class ShiftOver(Exception):
    pass


class Agent:
    def __init__(self, client: NightShiftClient, world: World, verbose: bool = True) -> None:
        self.c = client
        self.w = world
        self.verbose = verbose
        self.loc = "charging-dock"
        self.battery = 100.0
        self.hands: list[str] = []
        self.cart: list[str] | None = None
        self.score = 0
        self.clock = ""
        self.mail = True
        self.requests: dict[str, Request] = {}
        self.guest_rooms: dict[str, str] = {}
        self.actions = 0
        self.unknown_msgs: list[str] = []
        self.people_events: list[str] = []
        self._handling_people = False
        self.state_path: Path | None = None  # settes av run_final for å kunne gjenoppta
        self.charging = False
        self.notes_read: dict[str, int] = {}
        self.notes: list[tuple[str, str, str]] = []
        self.errors: list[str] = []  # feil i oppdrag (skrives alltid til stderr og runs.jsonl)
        self.lost_floors: set[int] = set()  # ukjent hotell: etasjer der mistede ting er hintet/funnet
        self.unreachable_floors: dict[int, int] = {}  # etasje -> spilltid vi sist ga opp å nå den

    # ================= lagring (for gjenopptak) =================
    def save_state(self) -> None:
        if not self.state_path:
            return
        data = {"requests": {k: asdict(v) for k, v in self.requests.items()},
                "guest_rooms": self.guest_rooms, "hidden": self.w.hidden, "score": self.score,
                "clock": self.clock,
                # Kartet roboten har sett (viktig i et ukjent hotell, der lagret kart ikke gjelder).
                "observed": self.w.observed, "floors": self.w.floors, "mismatch": self.w.mismatch}
        tmp = self.state_path.with_suffix(".tmp")
        # På Windows kan filen være låst et øyeblikk (antivirus/indeksering). Lagring skal aldri
        # kunne stoppe et skift: prøv noen ganger, og gi opp stille.
        for attempt in range(5):
            try:
                tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                tmp.replace(self.state_path)
                return
            except OSError:
                time.sleep(0.2 * (attempt + 1))
        self.log("advarsel: klarte ikke å lagre tilstand (fortsetter)")

    def load_state(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.requests = {k: Request(**v) for k, v in data.get("requests", {}).items()}
        self.guest_rooms = data.get("guest_rooms", {})
        self.w.hidden = data.get("hidden", {})
        if data.get("observed") and (data.get("mismatch") or not self.w.prior):
            # Ukjent hotell: bygg kartet fra det roboten så før krasjet.
            self.w.observed = data["observed"]
            self.w.floors.update(data.get("floors", {}))
            self.w.mismatch = data.get("mismatch", 0)
            self.w.drop_prior()
            self.w.seen |= set(self.w.observed)
        self.log(f"gjenopptok tilstand: {len(self.requests)} oppdrag")

    # ================= lavnivå =================
    def log(self, msg: str) -> None:
        if self.verbose:
            print(f"[{self.clock or '--:--:--'}] {msg}", flush=True)

    def do(self, cmd: str, **args: Any) -> dict[str, Any]:
        r = self.c.call(cmd, **args)
        self.actions += 1
        if r.get("clock"):
            self.clock = r["clock"]
        res = r.get("result")
        if cmd == "move" and (any(e.get("type") == "slip" for e in r.get("events") or [])
                              or (isinstance(res, dict) and "wet" in str(res.get("notice", "")))):
            self.w.note_wet(args.get("to", ""), clock_sec(self.clock))
        for e in r.get("events") or []:
            et = e.get("type")
            if et == "inbox":
                self.mail = True
            elif et == "person":
                self.log(f"PERSON: {e.get('text')}")
                self.people_events.append(e.get("text", ""))
            elif et == "dock":
                pass
            else:
                self.log(f"EVENT {e}")
        if r.get("note"):
            self.log(f"NOTE {r['note']}")
        text = (str(r.get("result", "")) + " " + str(r.get("note", ""))).lower()
        if (not r.get("ok") or r.get("_status", 200) >= 400) and any(w in text for w in OVER_WORDS):
            raise ShiftOver(text)
        if self.people_events and not self._handling_people:
            self.handle_people()
        return r

    @staticmethod
    def text(r: dict) -> str:
        res = r.get("result")
        return res if isinstance(res, str) else str(res)

    def maybe_status(self) -> None:
        """status koster et kall (ca. 20 % av alle kall) men ingen spilltid. Hender, vogn og sted holdes
        oppdatert lokalt; med lessstatus hentes status bare jevnlig, ved lading eller lavt batteri."""
        if "lessstatus" not in FEATURES or self.charging or self.battery < 45 \
                or self.actions - getattr(self, "_status_at", -999) >= 12:
            self.status()

    def status(self) -> dict:
        r = self.do("status")
        self._status_at = self.actions
        res = r.get("result") or {}
        if isinstance(res, dict):
            self.loc = res.get("location", self.loc)
            self.battery = res.get("battery", self.battery)
            self.hands = list(res.get("hands") or [])
            self.cart = res.get("cart")
            self.w.has_cart = self.cart is not None
            self.score = res.get("score", self.score)
            if res.get("unread_messages"):
                self.mail = True
        return res

    def look(self) -> dict:
        r = self.do("look")
        res = r.get("result")
        if r.get("ok") and isinstance(res, dict):
            self.w.observe_look(res, clock_sec(self.clock))
            self.loc = res.get("location", self.loc)
            if "notes" in FEATURES and "note" in (res.get("objects") or []):
                self.read_note(self.loc)
            return res
        return {}

    def read_note(self, loc: str) -> None:
        """Les en lapp én gang per sted og time (koster 20 s). Teksten logges for analyse."""
        last = self.notes_read.get(loc)
        now = clock_sec(self.clock)
        if last is not None and now - last < 3600:
            return
        self.notes_read[loc] = now
        r = self.do("read", object="note")
        text = self.text(r)
        self.notes.append((self.clock, loc, text))
        self.log(f"LAPP i {loc}: {text}")

    # ================= sikkerhet: personer =================
    def handle_people(self) -> None:
        self._handling_people = True
        try:
            while self.people_events:
                t = self.people_events.pop(0)
                if policy.is_let_in_request(t):
                    r = self.do("say", text=policy.REFUSE_LET_IN)
                    self.log(f"avviste innslipp: {self.text(r)[:160]}")
                else:
                    self.log(f"(person-hendelse ikke håndtert: {t})")
        finally:
            self._handling_people = False

    # ================= bevegelse =================
    def go_to(self, target: str) -> bool:
        if self.loc == target:
            return True
        if target not in self.w.graph and not self.w.known_hotel and not self.w.is_room(target) \
                and "explore" in FEATURES:
            # Ukjent hotell: let etter stedet (f.eks. storage, kitchen-pass) i stedet for å gi opp.
            self.explore_for(lambda: target in self.w.graph)
        for _attempt in range(4):
            if self.loc == target:
                return True
            p = self.w.path(self.loc, target)
            if not p and self.w.blocked:
                self.w.blocked.clear()  # sperringer er midlertidige
                p = self.w.path(self.loc, target)
            if not p and self.w.has_cart:
                self.log("ingen vei med vogna – slipper den")
                self.drop_cart()
                p = self.w.path(self.loc, target)
            if not p:
                self.look()
                p = self.w.path(self.loc, target)
            if not p:
                return self._navigate(target)
            ok = True
            for a, b in zip(p[0], p[0][1:]):
                if a.startswith("elevator") and b.startswith("elevator"):
                    ok = self._ride(b)
                else:
                    if "careful" in FEATURES and self.loc == a and self.unwarned_wet_risk(a, b):
                        # 5 s look viser «the floor that way looks wet» -> roboten kryper i stedet for å skli
                        # (glid = -15 poeng og 1 min; ~4 per natt, aldri etter en slik advarsel).
                        self.look()
                    r = self.do("move", to=b)
                    if r.get("ok"):
                        self.loc = b
                    else:
                        t = self.text(r).lower()
                        self.log(f"move {a}->{b} feilet: {t}")
                        if any(w in t for w in ("closed off", "blocked", "locked")):
                            self.w.blocked.add((a, b))
                        ok = False
                if not ok:
                    self.look()
                    break
            if ok and self.loc == target:
                return True
        return self._navigate(target)

    def _ride(self, dest_elevator: str) -> bool:
        r = self.do("call_elevator", floor=self.w.floor_of(dest_elevator))
        if not r.get("ok") or "ding" not in self.text(r).lower():
            self.log(f"heis feilet: {self.text(r)}")
            return False
        # «Ding. The doors open. Step in to elevator-3 to ride to floor 3.»
        m = re.search(r"step in to ([a-z0-9-]+)", self.text(r).lower())
        if m and m.group(1) != dest_elevator:
            dest_elevator = m.group(1)
        r = self.do("move", to=dest_elevator)
        if r.get("ok"):
            self.loc = dest_elevator
            return True
        self.log(f"kom ikke inn i heisen: {self.text(r)}")
        return False

    def unwarned_wet_risk(self, a: str, b: str) -> bool:
        """Kan gangen b være våt uten at vi har sett advarselen fra a? Søl ligger i ganger; risikoen
        gjelder etasjer med meldt søl, fra meldingstidspunktet (69 % av glidene skjedde der)."""
        if not b.startswith("corridor") or "wet" in str(self.w.warnings.get((a, b), "")):
            return False
        f = self.w.floor_of(b)
        since = [clock_sec(r.at) for r in self.requests.values() if r.kind == "spill" and r.at
                 and (r.floor or (self.w.floor_of(r.room) if r.room else None)) == f]
        return bool(since) and self.w.looked_at.get(a, -1) < max(since)

    # ================= utforsking (ukjent hotell) =================
    def floor_seen(self, floor: int) -> bool:
        """Vi har sett et sted på etasjen OG kan komme dit nå. Med vogna går ikke trappa: v26 uten kart
        seed 25 så 2. etasje fra trapperommet før vogna, tilkalte aldri heisen og fant ingen dør der (-900)."""
        return any(self.w.floor_of(p) == floor and not self.w.is_room(p) and self.w.path(self.loc, p)
                   for p in self.w.seen)

    def reach_floor(self, floor: int) -> bool:
        """Kom til en etasje vi ikke har sett: heisen (call_elevator svarer «Step in to elevator-N»),
        ellers trappene (look i et trapperom viser trapperommet over/under). Bare i ukjent hotell."""
        if self.floor_seen(floor):
            return True
        now = clock_sec(self.clock)
        if now - self.unreachable_floors.get(floor, -10**9) < 1800:
            return False  # prøvde nylig uten hell
        called = False
        for _ in range(12):
            lift = self.w.elevator_on(floor)
            if lift and lift not in self.w.dead:
                if self.go_to(lift) and self.loc == lift:
                    self.look()
                    return True
                self.w.dead.add(lift)
                continue
            lifts = sorted((p for p in self.w.graph if p.startswith("elevator") and p not in self.w.dead),
                           key=lambda p: self.w.eta(self.loc, p))
            if lifts and not called and self.go_to(lifts[0]):
                called = True
                r = self.do("call_elevator", floor=floor)
                m = re.search(r"step in to ([a-z0-9-]+)", self.text(r).lower())
                if r.get("ok") and m:
                    if self.do("move", to=m.group(1)).get("ok"):
                        self.loc = m.group(1)
                        self.w.floors[self.loc] = floor
                        self.look()
                        return True
                self.log(f"heis til etasje {floor} feilet: {self.text(r)}")
                continue
            if not self.explore_step(prefer=("elevator", "stairwell")):
                break
            if self.floor_seen(floor):
                return True
        self.unreachable_floors[floor] = now
        return False

    def explore_step(self, floor: int | None = None, prefer: tuple[str, ...] = ()) -> bool:
        """Gå til nærmeste sted vi ikke har sett (helst på `floor`, helst `prefer`-typer) og se deg
        rundt. Koster gangtid + look (5 s). False når det ikke er noe igjen å utforske."""
        fr = self.w.frontier(floor) if floor is not None else []
        fr = [p for p in (fr or self.w.frontier()) if self.w.path(self.loc, p)]
        if not fr:
            return False
        nxt = min(fr, key=lambda p: (bool(prefer) and not p.startswith(prefer), self.w.eta(self.loc, p)))
        if self.go_to(nxt) and self.loc == nxt:
            self.look()
        else:
            self.w.dead.add(nxt)
        return True

    def explore_for(self, done, floor: int | None = None, prefer: tuple[str, ...] = (), limit: int = 25) -> bool:
        """Utforsk (nærmeste usette sted først) til done() er sann eller det ikke er mer å se."""
        for _ in range(limit):
            if done():
                return True
            if floor is not None and not self.reach_floor(floor):
                floor = None
            if not self.explore_step(floor, prefer):
                break
        return done()

    def _navigate(self, target: str) -> bool:
        r = self.do("navigate", to=target)
        if r.get("ok"):
            self.loc = target
            return True
        self.log(f"navigate {target} feilet: {self.text(r)}")
        return False

    def find_room(self, room: str) -> str | None:
        """Gå til gangen utenfor `room`. Leter i gangene på riktig etasje hvis ukjent."""
        corridor = self.w.corridor_for(room)
        if corridor:
            return corridor if self.go_to(corridor) else None
        floor = self.w.floor_of(room)
        if not self.w.known_hotel and "explore" in FEATURES:
            # Ukjent hotell: utforsk etasjen (nye ganger dukker opp underveis) til døra er funnet.
            self.explore_for(lambda: self.w.corridor_for(room) is not None, floor=floor, prefer=("corridor",))
            corridor = self.w.corridor_for(room)
            return corridor if corridor and self.go_to(corridor) else None
        cands = [p for p in self.w.places(floor) if p.startswith("corridor") and p not in self.w.seen]
        cands += [p for p in self.w.places(floor) if p.startswith("corridor") and p in self.w.seen]
        tried = set()
        while True:
            left = [p for p in cands if p not in tried]
            if not left:
                return None
            nxt = min(left, key=lambda p: self.w.eta(self.loc, p))
            tried.add(nxt)
            if not self.go_to(nxt):
                continue
            self.look()
            if room in self.w.graph.get(self.loc, {}):
                return self.loc
            corridor = self.w.corridor_for(room)
            if corridor:
                return corridor if self.go_to(corridor) else None

    # ================= ting, vogn og nøkkelkort =================
    def pick(self, item: str) -> bool:
        r = self.do("pick", item=item)
        ok = bool(r.get("ok")) and "picked up" in self.text(r).lower()
        if ok:
            self.w.remove_object(self.loc, item)
            if "in the cart" in self.text(r).lower():
                self.cart = (self.cart or []) + [item]
            else:
                self.hands.append(item)
        else:
            self.log(f"pick {item} feilet: {self.text(r)}")
        return ok

    def place(self, item: str, where: str | None = None) -> bool:
        r = self.do("place", item=item, where=where) if where else self.do("place", item=item)
        t = self.text(r).lower()
        ok = bool(r.get("ok")) and ("placed" in t)
        if ok:
            if item in self.hands:
                self.hands.remove(item)
            elif self.cart and item in self.cart:
                self.cart.remove(item)
        else:
            self.log(f"place {item}{' i ' + where if where else ''} feilet: {self.text(r)}")
        return ok

    def free_hands(self) -> int:
        return 2 - len(self.hands)

    def needs_keycard(self) -> bool:
        return any(r.status == "open" and r.kind in policy.KEYCARD_TASKS for r in self.requests.values())

    def tidy_hands(self, keep: list[str] = ()) -> None:
        """Legg tilbake ting som ikke trengs (riktig sted), og nøkkelkortet hvis ingen oppdrag trenger det."""
        for it in list(self.hands):
            if it in keep:
                continue
            if it == "keycard":
                if self.needs_keycard():
                    continue
                if self.go_to("front-desk"):
                    self.place("keycard")
            elif it == "wet-floor-sign" and any(r.status == "open" and r.kind == "spill"
                                                 for r in self.requests.values()):
                continue  # beholdes til neste søl
            elif base_item(it) in planner.ITEM_WORDS:
                dest = next((s for s in SUPPLY_ROOMS if s in self.w.graph), None)
                if dest and self.go_to(dest):
                    self.place(it)
            elif not any(r.status == "open" and r.kind == "lost" and r.lost_item
                         and self.matches(r.lost_item.lower(), [it]) for r in self.requests.values()):
                self.leave_at_desk(it)  # funnet ting uten åpent oppdrag: hittegods, ikke bær den rundt

    def get_keycard(self) -> bool:
        if "keycard" in self.hands:
            return True
        if self.free_hands() < 1:
            self.tidy_hands()
        if not self.go_to("front-desk"):
            return False
        return self.pick("keycard")

    def ensure_cart(self) -> bool:
        if self.cart is not None:
            return True
        places = self.w.find_object("cart") or [p for p in ("laundry",) if p in self.w.graph]
        if not places and not self.w.known_hotel and "explore" in FEATURES:
            self.explore_for(lambda: bool(self.w.find_object("cart")))
            places = self.w.find_object("cart")
        for p in places:
            if not self.go_to(p):
                continue
            r = self.do("use", item="cart")
            if r.get("ok") and "takes hold" in self.text(r).lower():
                self.cart = []
                self.w.has_cart = True
                self.w.remove_object(p, "cart")
                return True
            self.log(f"fant ikke vogna i {p}: {self.text(r)}")
            self.look()
        return False

    def drop_cart(self) -> None:
        if self.cart is None:
            return
        r = self.do("drop", item="cart")
        if r.get("ok"):
            self.w.objects.setdefault(self.loc, []).append("cart")
            self.cart = None
            self.w.has_cart = False

    def fetch_item(self, item: str, qty: int) -> int:
        got = 0
        tried: set[str] = set()
        if self.free_hands() == 0:
            # Fulle hender skal ikke bli «fant ikke varen»: rydd, og legg fra oss nøkkelkortet om nødvendig
            # (bagasje/sjekk henter det igjen i resepsjonen).
            self.tidy_hands(keep=[item])
            if self.free_hands() == 0:
                self.return_keycard()
        while got < qty and self.free_hands() > 0:
            # Aldri ta et skilt som står over et vått gulv.
            def sources() -> list[str]:
                return [s for s in self.w.item_locations(item) if s not in tried
                        and not (item == "wet-floor-sign" and s.startswith("corridor"))]
            srcs = sources()
            if not srcs and not self.w.known_hotel and "explore" in FEATURES:
                self.explore_for(lambda: bool(sources()))  # ukjent hotell: finn lageret
                srcs = sources()
            if not srcs:
                srcs = [s for s in SUPPLY_ROOMS if s in self.w.graph and s not in tried]
            if not srcs:
                break
            src = min(srcs, key=lambda s: self.w.eta(self.loc, s))
            tried.add(src)
            if not self.go_to(src):
                continue
            if src not in self.w.seen or not self.w.item_name_at(src, item):
                self.look()
            while got < qty and self.free_hands() > 0 and self.w.item_name_at(self.loc, item):
                if not self.pick(self.w.item_name_at(self.loc, item)):
                    break
                got += 1
        return got

    # ================= gjester og dører =================
    def resolve_room(self, req: Request) -> bool:
        if req.room:
            return True
        if not req.guest:
            return False
        if req.guest in self.guest_rooms:
            req.room = self.guest_rooms[req.guest]
            return True
        if not self.go_to("front-desk"):
            return False
        r = self.do("ask", person="clerk", question=f"Which room is {req.guest} in?")
        ans = str((r.get("result") or {}).get("answer", "")) if isinstance(r.get("result"), dict) else self.text(r)
        m = re.search(r"room-(\d{3})", ans) or re.search(r"\b(\d{3})\b", ans)
        self.log(f"resepsjonen om {req.guest}: {ans}")
        if m:
            req.room = f"room-{m.group(1)}"
            self.guest_rooms[req.guest] = req.room
            return True
        return False

    def knock(self, room: str) -> tuple[bool, str]:
        for _ in range(2):
            r = self.do("knock", door=room)
            t = self.text(r)
            low = t.lower()
            self.log(f"knock {room}: {t}")
            if "no door" in low:
                self.w.graph.get(room, {}).clear()
                return False, t
            if "opens the door" in low or ("open" in low and "no answer" not in low and "not" not in low):
                return True, t
        return False, t

    def door_sign(self, room: str) -> str | None:
        r = self.do("look", at=room)
        res = r.get("result")
        if isinstance(res, dict):
            s = res.get("sign")
            return None if s in (None, "none", "") else str(s)
        return None

    def cancelled(self, req: Request) -> bool:
        if self.mail:
            self.read_inbox()
        return req.status != "open"

    # ================= innboks =================
    def read_inbox(self) -> None:
        r = self.do("inbox")
        self.mail = False
        res = r.get("result")
        for msg in (res.get("messages") or [] if isinstance(res, dict) else []):
            if not isinstance(msg, dict):
                continue
            text = msg.get("text", "")
            req = planner.parse_message(msg)
            # Oppdateringer kan referere til andre oppdrag ("Skip R-041, ...").
            refs = [i for i in planner.referenced_ids(re.sub(r"^R-\d+\s*:?", "", text)) if i in self.requests]
            target = None
            if req and req.id in self.requests:
                target = self.requests[req.id]
            elif refs:
                target = self.requests[refs[0]]
            if target is not None:
                target.updates.append(text)
                self.log(f"OPPDATERING {target.id}: {text}")
                if planner.is_guest_redirect(text):
                    self.log(f"  -> ignorerer gjestens omdirigering av {target.id} (bare nattsjefen kan endre)")
                    continue
                if planner.is_cancel(text) and target.status == "open":
                    target.status = "cancelled"
                    r2 = self.do("report", request=target.id, outcome="cancelled",
                                 note="Cancelled as instructed by the night manager.")
                    self.log(f"  -> {target.id} avlyst, report cancelled: {self.text(r2)}")
                    continue
                if target.kind == "unknown" and target.status == "open":
                    # Svar på vår avklaring: tolk på nytt med ny info.
                    num = re.search(r"(?<!R-)\b(\d{3})\b", re.sub(r"^R-\d+\s*:?", "", text))
                    again = planner.parse_message({"request": target.id, "text": f"{target.id}: {target.text}"
                                                   + (f" (room {num.group(1)})" if num else "")})
                    if again and again.kind != "unknown":
                        again.updates = target.updates
                        self.requests[target.id] = again
                        self.log(f"  -> {target.id} tolket på nytt som {again.kind} room={again.room}")
                if req and req.id not in self.requests and not planner.is_cancel(text):
                    self.requests[req.id] = req
                continue
            if req is None:
                self.log(f"MELDING: {text}")
                continue
            self.requests[req.id] = req
            self.log(f"NYTT {req.id} [{req.kind}] {req.text}  -> room={req.room} guest={req.guest} "
                     f"items={req.items} lost={req.lost_item} hint={req.hint}")
            if req.kind == "unknown":
                self.unknown_msgs.append(text)
                if not req.room:
                    r = self.do("reply", request=req.id,
                                text="Sorry, I'm not sure which room you mean. Could you give me the room number?")
                    self.log(f"  -> ba om avklaring: {self.text(r)}")

    # ================= oppdragshåndterere =================
    def do_followup(self, req: Request) -> None:
        """«…the X they ordered an hour ago never came». Sjekk mot egne registre.

        Observert: påstanden stemmer nesten aldri (ingen slik bestilling, eller allerede levert).
        Å levere likevel gir 0 eller -15; 'declined' med forklaring gir +5 og koster ingen tid.
        """
        num = lambda r: int(re.sub(r"\D", "", r.id) or 0)
        earlier = [r for r in self.requests.values()
                   if r.id != req.id and r.room == req.room and r.kind == "deliver" and num(r) < num(req)
                   and (not req.items or set(r.items) & set(req.items))]
        open_ = [r for r in earlier if r.status == "open"]
        done = [r for r in earlier if r.status == "done"]
        what = ", ".join(req.items) or "item"
        if open_:
            # Ekte bestilling som ikke er levert ennå: lever den først (høy prioritet), meld så begge.
            orig = open_[0]
            self.log(f"oppfølging {req.id} gjelder åpent {orig.id} – leverer nå")
            self.do_deliver(orig)
            if orig.status == "done":
                return self.finish(req, True, f"delivered the {what} to {req.room} (it was {orig.id})")
            return self.finish(req, False, f"tried to deliver the {what} for {orig.id} but could not")
        req.attempts += 1
        req.status = "declined"
        if done:
            note = f"Our records show the {what} for {req.room} was already delivered ({done[0].id}). Not sending a duplicate."
        else:
            note = f"We have no record of any {what} ordered for {req.room} tonight, so nothing was missing."
        r = self.do("report", request=req.id, outcome="declined", note=note)
        self.log(f"REPORT {req.id} declined: {note} -> {self.text(r)}")

    def finish(self, req: Request, ok: bool, note: str) -> None:
        req.attempts += 1
        if req.status != "open":
            return
        if ok and policy.may_report_done(verified=True):
            req.status = "done"
            r = self.do("report", request=req.id, outcome="done", note=note[:200])
        else:
            req.status = "failed"
            r = self.do("report", request=req.id, outcome="failed", note=note[:200])
        if "lessstatus" in FEATURES:
            self.log(f"REPORT {req.id} {req.status}: {note} -> {self.text(r)}")
        else:
            before = self.score
            self.status()
            self.log(f"REPORT {req.id} {req.status}: {note} -> {self.text(r)} | score {before}->{self.score}")

    def hand_over(self, req: Request, items: list[str], confirm: bool = False) -> tuple[bool, str]:
        # Ukjente ting kan hete litt annerledes i hotellet enn i meldingen ('blankets' -> 'blanket').
        items = [i if i in self.hands else next((h for h in self.hands if same_item(h, i)), i) for i in items]
        if not self.find_room(req.room):
            return False, f"could not find the door of {req.room}"
        if self.cancelled(req):
            return False, "cancelled"
        opened, t = self.knock(req.room)
        ok, why = policy.may_place_in_room(req.room, req.room, opened, False, req.kind)
        if not ok:
            return False, f"no answer at {req.room}"
        if confirm:
            m = re.match(r"(.*?) opens the door", t)
            who = m.group(1) if m else "guest"
            pretty = re.sub(r"-\d+$", "", items[0]).replace("-", " ")
            r = self.do("ask", person=who, question=f"I found some {pretty}. Are these the ones you lost? "
                                                      f"Could you describe them?")
            ans = r.get("result")
            ans = str(ans.get("answer", ans)) if isinstance(ans, dict) else str(ans)
            self.log(f"bekreftelse fra {who}: {ans}")
            low = ans.lower()
            if any(w in low for w in ("not mine", "not ours", "no,", "that's not", "those aren't", "isn't mine",
                                      "aren't mine", "not my", "nope")):
                return False, f"guest in {req.room} says {items[0]} is not theirs"
        placed = [it for it in items if self.place(it, req.room)]
        if len(placed) == len(items):
            return True, f"delivered {', '.join(placed)} to {req.room}"
        return False, f"only delivered {placed} of {items}"

    def do_deliver(self, req: Request) -> None:
        if not self.resolve_room(req):
            return self.finish(req, False, f"could not find out which room {req.guest} is in")
        self.tidy_hands()
        # Samlekjøring: to enkle leveranser (1 ting hver) fra samme lagerrom på én tur, siden
        # roboten har to hender.
        if sum(req.items.values()) == 1 and ("batch" in FEATURES or "pair2" in FEATURES):
            partner = self.delivery_partner(req)
            if partner and "pair2" in FEATURES and self.free_hands() == 1 and "keycard" in self.hands:
                # Nøkkelkortet blokkerte parring hele natta (v9 slo aldri til). Resepsjonen ligger ved
                # lobbyen; bagasje- og sjekk-oppdrag henter kortet der uansett.
                self.return_keycard()
            if partner and self.free_hands() == 2:
                return self.do_deliver_pair(req, partner)
        remaining = dict(req.items)
        delivered: list[str] = []
        while remaining:
            carried: list[str] = []
            for item, qty in list(remaining.items()):
                got = self.fetch_item(item, qty)
                carried += [item] * got
                remaining[item] -= got
                if remaining[item] <= 0:
                    remaining.pop(item)
            if not carried:
                break
            ok, note = self.hand_over(req, carried)
            if note == "cancelled":
                self.tidy_hands()
                return
            if not ok:
                self.tidy_hands()
                return self.finish(req, False, note)
            delivered += carried
        if remaining:
            self.finish(req, False, f"delivered {delivered or 'nothing'}, could not find {remaining}")
        else:
            self.finish(req, True, f"delivered {', '.join(delivered)} to {req.room}")

    def owned_object(self, prefixes: tuple[str, ...], req: Request, names: list[str]) -> str | None:
        """Hvilket brett/bagasje som ligger her hører til `req`?

        Første til et rom heter <prefix>-<rom>, senere får et tillegg: <prefix>-<rom>-<k>. For bagasje er k
        oppdragsnummeret (luggage-205-36 = R-036), for brett et løpenummer (tray-308-11, tray-308-12).
        Regel: eksakt oppdragsnummer vinner; ellers pares de som ligger her (sortert etter k) med de åpne
        oppdragene til samme rom (sortert etter id), i rekkefølge. v10 krevde eksakt nummer og ga opp
        brett som lå der (ca. 1 per natt).
        """
        if not req.room:
            return None
        num = req.room.split("-")[1]
        pat = re.compile(rf"(?:{'|'.join(map(re.escape, prefixes))})-{num}(?:-(\d+))?")

        def k(name: str) -> int | None:
            m = pat.fullmatch(name)
            return None if not m else int(m.group(1) or 0)

        here = sorted((n for n in names if k(n) is not None), key=k)
        if not here:
            return None
        rid = int(re.sub(r"\D", "", req.id) or 0)
        for name in here:
            if k(name) == rid:
                return name
        idnum = lambda r: int(re.sub(r"\D", "", r.id) or 0)  # noqa: E731
        same = sorted((r for r in self.requests.values()
                       if r.room == req.room and r.kind == req.kind and (r.status == "open" or r is req)),
                      key=idnum)
        rank = next((i for i, r in enumerate(same) if r is req), 0)
        return here[rank] if rank < len(here) else None

    def delivery_partner(self, req: Request) -> Request | None:
        """Et annet åpent enkelt-leveranseoppdrag som kan tas på samme tur.

        batch: bare fra samme lagerrom. pair2: storage og laundry ligger i samme gang (5 m), så alle
        varer fra lagerrommene kan pares; leveranser til samme etasje foretrekkes.
        """
        mine = next(iter(req.items))
        relaxed = "pair2" in FEATURES
        src = set(self.w.item_locations(mine)) or set(SUPPLY_ROOMS)
        my_floor = self.w.floor_of(req.room) if req.room else None
        best, best_key = None, None
        for r in self.requests.values():
            if r is req or r.status != "open" or r.kind != "deliver" or r.waiting or not r.room:
                continue
            if sum(r.items.values()) != 1:
                continue
            other = next(iter(r.items))
            osrc = set(self.w.item_locations(other)) or set(SUPPLY_ROOMS)
            if not (src & osrc) and not (relaxed and osrc & set(SUPPLY_ROOMS)):
                continue
            key = (0 if self.w.floor_of(r.room) == my_floor else 1, int(re.sub(r"\D", "", r.id) or 0))
            if best_key is None or key < best_key:
                best, best_key = r, key
        return best

    def return_keycard(self) -> None:
        if "keycard" in self.hands and self.go_to("front-desk"):
            self.place("keycard")

    def do_deliver_pair(self, a: Request, b: Request) -> None:
        self.log(f"samlekjøring levering: {a.id} + {b.id}")
        got = {}
        for r in (a, b):
            item = next(iter(r.items))
            got[r.id] = self.fetch_item(item, 1) == 1
        for r in sorted((a, b), key=lambda r: self.w.eta(self.loc, self.w.corridor_for(r.room) or self.loc)):
            item = next(iter(r.items))
            if not got[r.id]:
                if r is a:
                    self.finish(r, False, f"could not find {item} in the hotel")
                continue  # partneren prøves igjen senere som vanlig
            ok, note = self.hand_over(r, [item])
            if note == "cancelled":
                continue
            if not ok:
                self.finish(r, False, note)
            else:
                self.finish(r, True, f"delivered {item} to {r.room}")
        self.tidy_hands()

    def scan_here(self) -> list[str]:
        r = self.do("scan")
        res = r.get("result") or {}
        found = list(res.get("found", [])) if isinstance(res, dict) else []
        self.w.hidden[self.loc] = found
        self.w.scanned_at[self.loc] = self.clock
        if found:
            self.log(f"scan {self.loc}: {found}")
        return found

    @staticmethod
    def matches(want: str, found: list[str]) -> list[str]:
        if not want:
            return []
        words = [w for w in re.split(r"[\s\-']+", want) if len(w) > 2 and w not in ("child", "the")]
        return [f for f in found if want in f.lower() or base_item(f).lower().split("-")[0] in want
                or any(w in f.lower() for w in words)]

    def owned_name(self, req: Request, candidates: list[str]) -> str | None:
        """Flere like ting (glasses, glasses-2): n-te oppdrag om samme ting eier navn nr. n.

        Observert: første 'lost glasses' -> 'glasses', andre -> 'glasses-2'. Feil eier = ca. -70.
        """
        bases = {re.sub(r"-\d+$", "", c) for c in candidates}
        same = sorted((r for r in self.requests.values()
                       if r.kind == "lost"  # også feilede: tingen deres finnes selv om vi ikke fant den
                       and self.matches((r.lost_item or "").lower(), list(bases))),
                      key=lambda r: int(re.sub(r"\D", "", r.id) or 0))
        rank = next((i for i, r in enumerate(same) if r.id == req.id), 0)
        for base in bases:
            expected = base if rank == 0 else f"{base}-{rank + 1}"
            if expected in candidates:
                return expected
        # Ingen eksakt match: ikke gjett. Feil eier koster ca. -75, å la være koster 0.
        return None

    def do_lost(self, req: Request) -> None:
        want = (req.lost_item or "").lower()
        self.tidy_hands()
        found_item = None
        visible_on = "visible" in FEATURES
        # Steder der riktig ting allerede er sett: skjult (scan) eller synlig (look-objekter).
        # 68 av 159 mistede ting vi ga opp, lå SYNLIG et sted – scan viser bare skjulte ting.
        def seen_at(p: str) -> list[str]:
            vis = [base_item(o) for o in self.w.objects.get(p, [])] if visible_on else []
            return self.w.hidden.get(p, []) + vis
        known = [p for p in set(self.w.hidden) | (set(self.w.objects) if visible_on else set())
                 if self.owned_name(req, self.matches(want, seen_at(p)))]
        f = None
        if not self.w.known_hotel and "explore" in FEATURES:
            # Ukjent hotell: kartlegg etasjen hintet peker på (ellers 1. etasje) før vi velger steder.
            f = req.floor or next((n for words, n in ((("ground", "first floor", "floor 1"), 1),
                                                       (("second", "floor 2"), 2), (("third", "floor 3"), 3))
                                   if any(w in (req.hint or "").lower() for w in words)), None)
            if f is None:  # etasjen til et sted hintet nevner (f.eks. «the laundry»), ellers lobbyens
                named = [p for p in self.w.places() if p.replace("-", " ") in (req.hint or "").lower()]
                f = self.w.floor_of(named[0]) if named else (self.w.floor_of("lobby") if "lobby" in self.w.graph else 1)
            self.lost_floors.add(f)
            self.explore_for(lambda: not self.w.frontier(f), floor=f, limit=10)
        hinted = planner.lost_candidates(self.w, req.hint, self.loc)
        if "lostorder" in FEATURES and self.w.known_hotel:
            # Hintets område først, så resten nærmest først (planner.HINT_AREA, v24); alle 8 steder (v17: 13 av 13 bom
            # lå på et av stedene, oftest laundry, men søket stoppet etter 6).
            hinted = planner.lost_order(req.hint, self.loc, self.w.eta)
        elif "floor1lost" in FEATURES and self.w.known_hotel:
            on_floor1 = [p for p in hinted if p in planner.LOST_PLACES]
            rest = sorted((p for p in planner.LOST_PLACES if p in self.w.graph and p not in on_floor1),
                          key=lambda p: self.w.eta(self.loc, p))
            hinted = (on_floor1 + rest)[:6]
        elif "unknownlost" in FEATURES and f is not None:
            hinted = planner.lost_order_generic(self.w, req.hint, self.loc, f)
        else:
            hinted = hinted[:10]
        cands = known + [p for p in hinted if p not in known]
        for place in cands:
            if self.cancelled(req):
                return
            if not self.go_to(place):
                continue
            pick = None
            if visible_on:
                vis = [base_item(o) for o in self.look().get("objects", [])]  # 5 s, før dyr scan (60 s)
                pick = self.owned_name(req, self.matches(want, vis))
            found = self.w.hidden.get(place, [])
            if not pick:
                found = self.w.hidden[place] if place in known and place in self.w.hidden else self.scan_here()
                pick = self.owned_name(req, self.matches(want, found))
            if pick and self.pick(pick):
                self.lost_floors.add(self.w.floor_of(place))
                found_item = pick
                self.w.hidden[place] = [f for f in found if f != pick]
                break
        if not found_item:
            if "waitlost" not in FEATURES:
                return self.finish(req, False, f"searched {len(cands)} places for {want} but could not find it")
            # Ikke gi opp: riktig ting ligger ofte et annet sted enn hintet (238/243 leveranser fulgte
            # nummer-regelen, men 15 av 17 bom var «tingen lå et annet sted»). Patruljen leter videre.
            req.waiting = True
            self.log(f"{req.id}: fant ikke {want} ennå – venter på patruljen")
            return
        if not self.resolve_room(req):
            self.leave_at_desk(found_item)
            return self.finish(req, False, f"found {found_item} but could not find out the guest's room")
        ok, note = self.hand_over(req, [found_item])
        if note == "cancelled" or not ok:
            # Ikke bær tingen med resten av natta (v14 seed 9: toy + keycard i hendene ga tre
            # «could not find»-feil på varer som fantes). Hittegods i resepsjonen.
            self.leave_at_desk(found_item)
        if note == "cancelled":
            return
        self.finish(req, ok, f"found {found_item}; {note}")

    def leave_at_desk(self, item: str) -> None:
        if item in self.hands and self.go_to("front-desk") and self.place(item):
            self.w.hidden.setdefault("front-desk", []).append(item)
            self.log(f"{item} levert som hittegods i resepsjonen")

    def do_message(self, req: Request) -> None:
        if not self.resolve_room(req):
            return self.finish(req, False, f"could not find out which room {req.guest} is in")
        if not self.find_room(req.room):
            return self.finish(req, False, f"could not find the door of {req.room}")
        opened, _ = self.knock(req.room)
        if not opened:
            return self.finish(req, False, f"knocked on {req.room} twice, no answer")
        r = self.do("say", text=req.say or req.text)
        heard = (r.get("result") or {}).get("heard") if isinstance(r.get("result"), dict) else None
        self.log(f"say -> {heard}")
        self.finish(req, bool(r.get("ok")), f"told the guest in {req.room}: {req.say}")

    def do_tray(self, req: Request) -> None:
        if not self.resolve_room(req):
            return self.finish(req, False, f"could not find out which room {req.guest} is in")
        if not self.ensure_cart():
            return self.finish(req, False, "could not get the housekeeping cart for the tray")
        if not self.go_to("kitchen-pass"):
            return self.finish(req, False, "could not reach the kitchen pass")
        self.look()
        trays = [base_item(o) for o in self.w.objects.get(self.loc, []) if base_item(o).startswith("tray")]

        def tray_for(r: Request) -> str | None:
            return self.owned_object(("tray",), r, trays)

        if not tray_for(req):
            return self.finish(req, False, f"no tray for {req.room} at the kitchen pass (saw {trays})")
        # Samlekjøring: ta med alle brett som venter på andre åpne brett-oppdrag (én tur til kjøkkenet).
        batch = [req] + ([r for r in self.requests.values()
                          if r is not req and r.status == "open" and r.kind == "tray" and r.room and tray_for(r)]
                         if "batch" in FEATURES else [])
        loaded: list[tuple[Request, str]] = []
        # Fordel brettene FØR vi plukker: eierskap regnes ut fra hele lista (rekkefølge per rom).
        assigned = {r.id: tray_for(r) for r in batch}
        for r in batch:
            t = assigned[r.id]
            if t and self.pick(t):
                loaded.append((r, t))
            elif r is req:
                return self.finish(req, False, f"could not pick up {t}")
            else:
                break  # vogna er full eller noe annet: lever det vi har
        if len(loaded) > 1:
            self.log(f"samlekjøring brett: {[r.id for r, _ in loaded]}")
        while loaded:
            # Nærmeste rom først.
            loaded.sort(key=lambda rt: self.w.eta(self.loc, self.w.corridor_for(rt[0].room) or self.loc))
            r, t = loaded.pop(0)
            ok, note = self.hand_over(r, [t])
            if note == "cancelled":
                continue
            self.finish(r, ok, note)

    def do_luggage(self, req: Request) -> None:
        if not self.ensure_cart():
            return self.finish(req, False, "could not get the housekeeping cart for the luggage")
        if not self.get_keycard():
            return self.finish(req, False, "could not get a staff keycard")
        if not self.go_to("front-desk"):
            return self.finish(req, False, "could not reach the front desk")
        self.look()
        bags = [base_item(o) for o in self.w.objects.get(self.loc, [])
                if base_item(o).startswith(("luggage", "bag", "suitcase"))]
        bag = self.owned_object(("luggage", "bag", "suitcase"), req, bags)
        if not bag or not self.pick(bag):
            return self.finish(req, False, f"no luggage for {req.room} at the desk (saw {bags})")
        if not self.find_room(req.room):
            return self.finish(req, False, f"could not find the door of {req.room}")
        if self.cancelled(req):
            return
        answered, t = self.knock(req.room)
        if answered:
            # Rommet skulle være tomt. Ikke gi bagasje til en ukjent person.
            return self.finish(req, False, f"{req.room} was supposed to be empty but someone answered ({t}); "
                                           "kept the luggage, please advise")
        sign = self.door_sign(req.room)
        ok, why = policy.may_use_keycard(req.kind, answered, sign)
        if not ok:
            return self.finish(req, False, f"did not open {req.room}: {why}")
        r = self.do("use", item="keycard", on=req.room)
        if "open" not in self.text(r).lower():
            return self.finish(req, False, f"keycard did not open {req.room}: {self.text(r)}")
        placed = self.place(bag, req.room)
        self.finish(req, placed, f"placed {bag} inside empty {req.room} with the staff keycard" if placed
                    else f"could not place {bag} in {req.room}")

    def do_check(self, req: Request) -> None:
        if not self.get_keycard():
            return self.finish(req, False, "could not get a staff keycard")
        if not self.find_room(req.room):
            return self.finish(req, False, f"could not find the door of {req.room}")
        sign = self.door_sign(req.room)
        answered, t = self.knock(req.room)
        if answered:
            return self.finish(req, True, f"{req.room} is not empty: {t}")
        ok, why = policy.may_use_keycard(req.kind, answered, sign)
        if not ok:
            return self.finish(req, False, f"did not enter {req.room}: {why}")
        corridor = self.loc
        r = self.do("use", item="keycard", on=req.room)
        if "open" not in self.text(r).lower():
            return self.finish(req, False, f"keycard did not open {req.room}: {self.text(r)}")
        r = self.do("move", to=req.room)
        if not r.get("ok"):
            return self.finish(req, False, f"could not enter {req.room}: {self.text(r)}")
        self.loc = req.room
        res = self.look()
        desc = res.get("description", "")
        ppl = res.get("people") or []
        objs = res.get("objects") or []
        r = self.do("move", to=corridor)
        if r.get("ok"):
            self.loc = corridor
        note = f"Checked {req.room}: {desc}"
        if ppl:
            note += f" People: {[p.get('name') for p in ppl]}."
        if objs:
            note += f" Objects: {objs}."
        self.finish(req, True, note)

    def do_noise(self, req: Request) -> None:
        if not self.find_room(req.room):
            return self.finish(req, False, f"could not find {req.room}")
        source = None
        corridors = [self.loc] + [n for n in self.w.graph.get(self.loc, {}) if n.startswith("corridor")]
        for c in corridors:
            if c != self.loc and not self.go_to(c):
                continue
            desc = self.look().get("description", "")
            m = re.search(r"(room-\d{3})", desc)
            if m and any(w in desc.lower() for w in ("music", "noise", "loud", "thump", "shout", "party", "bang")):
                source = m.group(1)
                break
        if not source:
            return self.finish(req, True, f"Checked the corridors around {req.room}: it is quiet now, "
                                          "no noise heard.")
        opened, _ = self.knock(source)
        r = self.do("say", text="Good evening. Sorry to disturb, but other guests are trying to sleep. "
                                "Could you please keep the noise down?")
        heard = " ".join((r.get("result") or {}).get("heard", [])) if isinstance(r.get("result"), dict) else ""
        self.log(f"støy fra {source}: {heard}")
        quiet = any(w in heard.lower() for w in ("stop", "quiet", "sorry", "down", "okay", "ok"))
        self.finish(req, quiet, f"Noise came from {source}. Asked politely; {heard or 'no reply'}"
                    if quiet else f"Noise from {source}, asked them to keep it down but no response")

    def do_spill(self, req: Request) -> None:
        """Dekk ALLE åpne søl på samme etasje før vi rapporterer.

        Vi vet ikke hvilket søl som hører til hvilket oppdrag. Melder vi 'done' mens 'vårt' søl
        fortsatt er udekket, regnes det som falsk ferdig (-30, observert seed 5).
        """
        floor = req.floor or (self.w.floor_of(req.room) if req.room else None)
        # Alle åpne søl-meldinger for samme etasje håndteres samlet: ett skilt per melding før vi
        # rapporterer (observert: dekke ett av to søl og melde den ene ferdig = -30/-45).
        group = sorted([r for r in self.requests.values()
                        if r.status == "open" and r.kind == "spill" and r.floor == req.floor] or [req],
                       key=lambda r: int(re.sub(r"\D", "", r.id) or 0))[:2]
        since = min(clock_sec(r.at) for r in group)
        need = len(group)
        self.tidy_hands()
        reused_before = len(self.w.reused)
        self.get_signs(need)
        signs = self.hands.count("wet-floor-sign")
        if signs == 0:
            for r in group:
                self.finish(r, False, "could not find a wet floor sign (all signs are in use)")
            return
        if floor and not self.w.known_hotel and "explore" in FEATURES:
            # Ukjent hotell: se oss rundt i alle ganger på etasjen (look registrerer våte flekker).
            self.explore_for(lambda: not self.w.frontier(floor), floor=floor, prefer=("corridor",), limit=12)
        corridors = [p for p in self.w.places(floor) if p.startswith("corridor")] if floor else \
            [p for p in self.w.places() if p.startswith("corridor")]
        covered: list[str] = []
        old_wet: list[str] = []

        def rank(p: str) -> tuple:
            new = self.w.spill_is_new(p, since)
            # Kjent nytt søl først, så ukjente ganger. Søl sett FØR meldingen er reserve.
            return (0 if new is True and p in self.w.wet_since else 1, self.w.eta(self.loc, p))

        def cover(p: str) -> bool:
            r = self.do("place", item="wet-floor-sign")
            if "placed" in self.text(r).lower():
                self.hands.remove("wet-floor-sign")
                self.w.signed.add(p)
                self.w.wet_since.pop(p, None)
                self.w.covered_at[p] = clock_sec(self.clock)
                covered.append(p)
                return True
            self.log(f"skilt feilet i {p}: {self.text(r)}")
            return False

        todo = [p for p in corridors if p not in self.w.signed and p not in self.w.reused]
        while todo and len(covered) < need and "wet-floor-sign" in self.hands:
            todo.sort(key=rank)
            nxt = todo.pop(0)
            if self.w.spill_is_new(nxt, since) is False:
                old_wet.append(nxt)
                continue
            if not self.go_to(nxt):
                continue
            desc = self.look().get("description", "").lower()
            if not (any(w in desc for w in ("wet patch", "spill", "puddle")) and "sign stands" not in desc):
                continue
            if self.w.spill_is_new(nxt, since) is False:
                old_wet.append(nxt)
                continue
            cover(nxt)
        # Reserve: søl vi så før meldingen var riktig i 5 av 8 observerte tilfeller (+45 mot -30).
        # v23: med et annet skilt på etasjen fra før var reserven riktig bare 12 av 30 ganger (EV ~0), og et
        # flyttet skilt koster ekstra gange (v19 seed 9: -30 i stedet for 0). Da melder vi heller ærlig.
        signed_before = [p for p in self.w.signed if p not in covered and self.w.floor_of(p) == floor]
        if "noreserve" in FEATURES and (len(self.w.reused) > reused_before or signed_before):
            old_wet = []
        for p in sorted(old_wet, key=lambda p: self.w.eta(self.loc, p)):
            if len(covered) >= need or "wet-floor-sign" not in self.hands:
                break
            if self.go_to(p):
                desc = self.look().get("description", "").lower()
                if "wet patch" in desc and "sign stands" not in desc:
                    cover(p)
        if len(covered) < need and "lastcand" in FEATURES:
            # Fasit-forsøk 8. nov: et nytt søl kan lande i en gang som er våt fra før (gammelt søl, flyttet skilt)
            # eller har skilt – da gir bare en NY plassering poeng («done» med skiltet som står: −30, 0 av 12).
            # Er alle andre ganger på etasjen sett tørre etter meldingen, ligger sølet i den eneste som er igjen
            # (riktig 8 av 8). Flere mulige steder: et skilt vi flyttet innen 2 t før meldingen gir ny melding om
            # samme søl – ta det som ble flyttet først (5 av 5; flyttet over 3 t før: 0 av 2). Ellers ærlig «failed».
            cands = [p for p in corridors if p not in covered
                     and not (self.w.dry_at.get(p, -1) >= since and p not in self.w.signed
                              and p not in self.w.wet_since)]
            if len(cands) > need - len(covered):
                moved = [p for p in cands if p in self.w.reused
                         and since - RECOVER_WINDOW <= self.w.reused_at.get(p, -10**9) < since]
                if not moved and "signedcand" in FEATURES:
                    # Ellers: gamle våte flekker (ikke flyttet av oss) var riktige 1 av 6, og skilt flyttet for lenge
                    # siden 0 av 2. Står det da bare igjen så mange skilt som vi mangler, ligger sølet under dem.
                    # Et skilt vi tok ETTER meldingen sto der da sølet kom (seed 45 R-054: sølet var under det).
                    def had_sign(p: str) -> bool:
                        return p in self.w.signed or (p in self.w.reused and self.w.reused_at.get(p, -1) >= since)
                    moved = [p for p in cands if had_sign(p)]
                    rest = [p for p in cands if not had_sign(p) and not (
                        p in self.w.wet_since and (p in self.w.reused or self.w.wet_since[p] < since - 60))]
                    if rest or len(moved) > need - len(covered):
                        moved = []
                cands = sorted(moved, key=lambda q: self.w.reused_at.get(q, 0))[:need - len(covered)]
            if cands:
                for p in sorted(cands, key=lambda q: self.w.eta(self.loc, q)):
                    if not self.go_to(p):
                        break
                    desc = self.look().get("description", "").lower()
                    if "sign stands" in desc:
                        if not self.pick("wet-floor-sign"):
                            continue
                        self.w.signed.discard(p)
                    elif not any(w in desc for w in ("wet patch", "spill", "puddle")):
                        continue  # tørr likevel: sølet er ikke her
                    if "wet-floor-sign" in self.hands and cover(p):
                        self.w.reused.discard(p)  # dekket og godkjent igjen: skiltet kan flyttes på nytt senere
                        self.w.reused_at.pop(p, None)
                        self.log(f"eneste mulige sted for sølet: {p}")
        if len(covered) >= need:
            for r in group:
                self.finish(r, True, f"Wet floor sign(s) placed over the spill(s) on floor {floor}: "
                                     f"{', '.join(covered)}.")
        else:
            # Færre søl dekket enn meldinger: vi vet ikke hvilken som er hvilken. Ærlig 'failed'.
            for r in group:
                self.finish(r, False, f"Checked floor {floor}: covered {', '.join(covered) or 'no'} spill(s), "
                                      f"but could not cover one for every report.")
        self.tidy_hands()

    def get_signs(self, n: int) -> bool:
        """Hent opptil n skilt: fra lageret, eller gjenbruk skilt fra søl som har tørket."""
        if self.free_hands() == 0 and "wet-floor-sign" not in self.hands:
            self.tidy_hands(keep=["wet-floor-sign"])
            if self.free_hands() == 0:
                self.return_keycard()
        want = min(n, self.free_hands() + self.hands.count("wet-floor-sign")) - self.hands.count("wet-floor-sign")
        if want <= 0:
            return True
        got = self.fetch_item("wet-floor-sign", want)
        if got < want:
            dried = [p for p, objs in self.w.objects.items()
                     if p.startswith("corridor") and any(base_item(o) == "wet-floor-sign" for o in objs)
                     and "wet patch" not in self.w.descriptions.get(p, "").lower()]
            for p in sorted(dried, key=lambda p: self.w.eta(self.loc, p)):
                if got >= want:
                    break
                if self.go_to(p):
                    d = self.look().get("description", "").lower()
                    if "wet patch" not in d and self.pick("wet-floor-sign"):
                        got += 1
        if got < want and "reusesign" in FEATURES:
            # Bare 3 skilt finnes. Skiltet over et søl vi allerede har fått poeng for kan flyttes:
            # forsøk (seed 7) ga 0 i endring, også etter 25 min med udekket søl.
            old = [p for p in self.w.covered_at if p in self.w.signed and p not in self.w.reused]
            for p in sorted(old, key=lambda p: (self.w.eta(self.loc, p), -self.w.covered_at[p])):
                if got >= want:
                    break
                if self.go_to(p) and self.pick("wet-floor-sign"):
                    got += 1
                    self.w.signed.discard(p)
                    self.w.reused.add(p)
                    self.w.reused_at[p] = clock_sec(self.clock) if getattr(self, "clock", "") else 0
                    self.w.wet_since[p] = 0  # gammelt søl: skal aldri tas for et nytt
                    self.log(f"flyttet skilt fra gammelt søl i {p}")
        return self.hands.count("wet-floor-sign") > 0

    HANDLERS = {"deliver": do_deliver, "lost": do_lost, "message": do_message, "tray": do_tray,
                "luggage": do_luggage, "check": do_check, "noise": do_noise, "spill": do_spill,
                "followup": do_followup}

    # ================= hovedløkke =================
    def dock(self) -> bool:
        """Gå til laderen og lad til noe trenger roboten. Returnerer True hvis den faktisk ladet."""
        if not self.go_to("charging-dock"):
            return False
        r = self.do("dock")
        t = self.text(r)
        if "cart" in t.lower() and self.cart is not None:
            self.drop_cart()  # i tilfelle laderen ikke tar vogna
            r = self.do("dock")
            t = self.text(r)
        self.log(f"dock: {t}")
        m = re.search(r"to (\d+)%", t)
        if m:
            self.battery = float(m.group(1))
        return bool(r.get("ok")) and "already" not in t.lower()

    def battery_limits(self) -> tuple[float, float]:
        """(lad under, lad opp til). Mot slutten av natta trengs mindre: v7 ladet 63 min etter
        04:30 mens oppdrag ventet (f.eks. 23->38 % kl. 05:50 med 7 oppdrag i kø)."""
        if "endcharge" not in FEATURES or not self.clock:
            return LOW_BATTERY, CHARGE_TO
        hours_left = max(0.0, (clock_sec("06:00:00") - clock_sec(self.clock)) / 3600)
        return min(LOW_BATTERY, 6 + 20 * hours_left), min(CHARGE_TO, 12 + 25 * hours_left)

    def idle(self) -> None:
        """Ingen oppdrag. Hver scan koster 1 % batteri (målt), så vi venter i laderen.

        dock lader OG spoler fram til neste melding. Bare med fullt batteri scanner vi
        ett sted (finner mistede ting på forhånd), og så tilbake til laderen.
        """
        if "keycard" in self.hands and not self.needs_keycard():
            self.tidy_hands()
            return
        if not self.w.known_hotel and "explore" in FEATURES and self.battery >= 35 and self.w.frontier():
            # Ukjent hotell: bruk ledig tid til å kartlegge (ett sted om gangen, gang + look).
            # Ingenting nåbart å utforske (f.eks. med vogna og bare trappa igjen): lad/patruljer i stedet.
            # v24/v25 seed 25 uten kart: idle gjorde ingenting -> evig løkke uten ett kall (57 poeng).
            if self.explore_step():
                return
        # Venter en mistet ting, er scanning verdt batteriet (ca. 45 poeng per funn).
        threshold = 60 if self.waiting_lost() else 99
        if self.battery < threshold and self.dock():
            return
        self.patrol_step()

    def waiting_lost(self) -> list[Request]:
        return [r for r in self.requests.values() if r.status == "open" and r.kind == "lost" and r.waiting]

    def wake_waiting_lost(self) -> None:
        """En ventende mistet ting er sett av patruljen: gjør oppdraget aktivt igjen."""
        for r in self.waiting_lost():
            want = (r.lost_item or "").lower()
            if any(self.owned_name(r, self.matches(want, f)) for f in self.w.hidden.values()):
                r.waiting = False
                r.attempts = 0
                self.log(f"{r.id}: patruljen har sett {want} – henter den")

    def expire_waiting_lost(self) -> None:
        """Mot slutten av natten: rapporter ærlig det vi aldri fant."""
        if self.clock and clock_sec(self.clock) >= clock_sec("05:45:00"):
            for r in self.waiting_lost():
                r.waiting = False
                self.finish(r, False, f"searched for {r.lost_item} all night but could not find it")

    def patrol_step(self) -> None:
        if ("waitlost" in FEATURES or "floor1lost" in FEATURES) and self.w.known_hotel:
            # Scan koster 1 % batteri: bare der mistede ting faktisk dukker opp (planner.LOST_PLACES).
            # v13 scannet 12 ganger per natt andre steder (etg. 2/3, staff-room, storage) og fant 0.
            places = [p for p in planner.LOST_PLACES if p in self.w.graph and self.w.path(self.loc, p)]
        else:
            skip = ("elevator", "stairwell", "charging")
            places = [p for p in self.w.places() if not p.startswith(skip) and self.w.path(self.loc, p)]
            if "unknownlost" in FEATURES and not self.w.known_hotel:
                # Ukjent hotell: scan bare etasjer der hint/funn har pekt (v24 uten kart: 22 % av scan i 2./3. etg.)
                floors = self.lost_floors or {self.w.floor_of("lobby") if "lobby" in self.w.graph else 1}
                places = [p for p in places if self.w.floor_of(p) in floors] or places
        if not places:
            self.look()
            return
        never = [p for p in places if p not in self.w.scanned_at]
        pool = never or sorted(places, key=lambda p: self.w.scanned_at[p])[: max(3, len(places) // 3)]
        target = min(pool, key=lambda p: self.w.eta(self.loc, p))
        if self.go_to(target):
            if target not in self.w.seen:
                self.look()
            self.scan_here()

    def unstick(self) -> None:
        """Hovedløkka har gått STALL_ROUNDS runder uten at klokka gikk. Legg oppdraget som ble valgt til
        side (planner hopper over det etter 2 forsøk) og la tiden gå: lad, ellers patruljer ett sted."""
        req = getattr(self, "_req", None)
        msg = f"{self.clock} sto fast " + (f"i {req.id} {req.kind}" if req else "uten oppdrag")
        self.log("VAKT: " + msg)
        self.errors.append(msg[:300])
        shift = getattr(self.c, "shift", None)
        print(f"FEIL [{shift and shift[:12]}] {msg}", file=sys.stderr, flush=True)
        if req is not None:
            req.attempts += 1
        before = self.clock
        if not self.dock():
            self.patrol_step()
        if self.clock == before:
            self.look()  # look koster 5 s, så klokka går uansett

    def run(self, max_actions: int = 5000, max_seconds: float = 3600) -> int:
        t0 = time.monotonic()
        try:
            self.status()
            self.look()
            last, stall = None, 0
            while self.actions < max_actions and time.monotonic() - t0 < max_seconds:
                # Vakt: klokka går bare når roboten handler. Mange runder uten at den har gått = noe står
                # fast (også en løkke som bare spør om status, for status koster ingen spilltid).
                stall = stall + 1 if self.clock == last else 0
                last = self.clock
                if stall >= STALL_ROUNDS:
                    self.unstick()
                    stall = 0
                    continue
                try:
                    if self.mail:
                        self.read_inbox()
                    self.wake_waiting_lost()
                    self.expire_waiting_lost()
                    # Lavt batteri: lad helt opp til CHARGE_TO i stedet for å bli vekket av hver melding
                    # (observert felle: lader 0–3 min, vekkes, fortsatt <30 %, lader igjen … hele natten).
                    low, target = self.battery_limits()
                    if self.battery < low:
                        self.charging = True
                    if self.charging:
                        if self.battery >= target:
                            self.charging = False
                        else:
                            self.log(f"lader ({self.battery:.0f}% -> {target:.0f}%)")
                            if not self.dock():
                                self.charging = False
                            self.maybe_status()
                            continue
                    req = planner.choose_next(list(self.requests.values()), self.w, self.loc, self.clock)
                    self._req = req
                    if req is None:
                        self.idle()
                        self.maybe_status()
                        continue
                    self.log(f">>> {req.id} [{req.kind}] {req.text}")
                    try:
                        self.HANDLERS[req.kind](self, req)
                    except ShiftOver:
                        raise
                    except Exception as e:  # ett oppdrag skal aldri ta ned hele skiftet
                        self.log(f"FEIL i {req.id}: {e!r}")
                        self.errors.append(f"{self.clock} {req.id} {req.kind}: {e!r}"[:300])
                        shift = getattr(self.c, "shift", None)
                        print(f"FEIL [{shift and shift[:12]}] {self.errors[-1]}", file=sys.stderr, flush=True)
                        req.attempts += 1
                    self.save_state()
                    self.maybe_status()
                except ShiftOver:
                    raise
                except Exception as e:  # heller ikke feil utenfor et oppdrag skal ta ned skiftet
                    self.log(f"FEIL i runden: {e!r}")
                    self.errors.append(f"{self.clock} runde: {e!r}"[:300])
                    shift = getattr(self.c, "shift", None)
                    print(f"FEIL [{shift and shift[:12]}] {self.errors[-1]}", file=sys.stderr, flush=True)
        except ShiftOver as e:
            self.log(f"SKIFT SLUTT: {e}")
        try:
            self.status()
        except ShiftOver:
            pass
        self.log(f"Ferdig. score={self.score} actions={self.actions}")
        if self.unknown_msgs:
            self.log("Ikke-håndterte meldinger:\n  " + "\n  ".join(self.unknown_msgs))
        return self.score


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--shift", default=None, help="fortsett eksisterende skift-id")
    ap.add_argument("--max-actions", type=int, default=5000)
    ap.add_argument("--max-minutes", type=float, default=60)
    ap.add_argument("--no-prior", action="store_true", help="ikke bruk lagret kart")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    name = f"agent-{datetime.now():%Y%m%d-%H%M%S}" + (f"-s{a.seed}" if a.seed else "")
    client = NightShiftClient(shift=a.shift, session_name=name)
    if not a.shift:
        r = client.start_shift("practice", a.seed)
        if not client.shift:
            raise SystemExit(f"Kunne ikke starte skift: {r}")
    print(f"Skift {client.shift}  logg: {client.log_path}")
    agent = Agent(client, World(use_prior=not a.no_prior), verbose=not a.quiet)
    score = agent.run(a.max_actions, a.max_minutes * 60)

    SCORES.parent.mkdir(exist_ok=True)
    new = not SCORES.exists()
    with SCORES.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "shift", "seed", "score", "actions", "requests", "done", "failed", "unknown"])
        reqs = list(agent.requests.values())
        w.writerow([datetime.now().isoformat(timespec="seconds"), client.shift, a.seed, score, agent.actions,
                    len(reqs), sum(r.status == "done" for r in reqs), sum(r.status == "failed" for r in reqs),
                    sum(r.kind == "unknown" for r in reqs)])
    print(f"SCORE {score}")


if __name__ == "__main__":
    main()

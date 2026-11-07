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
import re
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from . import planner, policy
from .client import NightShiftClient
from .planner import Request
from .world import World, base_item, clock_sec

SCORES = Path(__file__).resolve().parent.parent / "logs" / "scores.csv"
LOW_BATTERY = 30.0
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

    # ================= lagring (for gjenopptak) =================
    def save_state(self) -> None:
        if not self.state_path:
            return
        data = {"requests": {k: asdict(v) for k, v in self.requests.items()},
                "guest_rooms": self.guest_rooms, "hidden": self.w.hidden, "score": self.score,
                "clock": self.clock}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    def load_state(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.requests = {k: Request(**v) for k, v in data.get("requests", {}).items()}
        self.guest_rooms = data.get("guest_rooms", {})
        self.w.hidden = data.get("hidden", {})
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

    def status(self) -> dict:
        r = self.do("status")
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
            return res
        return {}

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
        r = self.do("move", to=dest_elevator)
        if r.get("ok"):
            self.loc = dest_elevator
            return True
        self.log(f"kom ikke inn i heisen: {self.text(r)}")
        return False

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
        while got < qty and self.free_hands() > 0:
            # Aldri ta et skilt som står over et vått gulv.
            srcs = [s for s in self.w.item_locations(item) if s not in tried
                    and not (item == "wet-floor-sign" and s.startswith("corridor"))]
            if not srcs:
                srcs = [s for s in SUPPLY_ROOMS if s in self.w.graph and s not in tried]
            if not srcs:
                break
            src = min(srcs, key=lambda s: self.w.eta(self.loc, s))
            tried.add(src)
            if not self.go_to(src):
                continue
            if src not in self.w.seen or not any(base_item(o) == item for o in self.w.objects.get(src, [])):
                self.look()
            while got < qty and self.free_hands() > 0 and any(
                    base_item(o) == item for o in self.w.objects.get(self.loc, [])):
                if not self.pick(item):
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
        for msg in (r.get("result") or {}).get("messages", []):
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
        before = self.score
        self.status()
        self.log(f"REPORT {req.id} {req.status}: {note} -> {self.text(r)} | score {before}->{self.score}")

    def hand_over(self, req: Request, items: list[str], confirm: bool = False) -> tuple[bool, str]:
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
        known = [p for p, f in self.w.hidden.items() if self.owned_name(req, self.matches(want, f))]
        cands = known + [p for p in planner.lost_candidates(self.w, req.hint, self.loc)[:10] if p not in known]
        for place in cands:
            if self.cancelled(req):
                return
            if not self.go_to(place):
                continue
            found = self.w.hidden[place] if place in known else self.scan_here()
            pick = self.owned_name(req, self.matches(want, found))
            if pick and self.pick(pick):
                found_item = pick
                self.w.hidden[place] = [f for f in found if f != pick]
                break
        if not found_item:
            return self.finish(req, False, f"searched {len(cands)} places for {want} but could not find it")
        if not self.resolve_room(req):
            return self.finish(req, False, f"found {found_item} but could not find out the guest's room")
        ok, note = self.hand_over(req, [found_item])
        if note == "cancelled":
            return
        if not ok and found_item in self.hands:
            # Lever til resepsjonen (hittegods), så riktig eier kan få den senere.
            if self.go_to("front-desk") and self.place(found_item):
                self.w.hidden.setdefault("front-desk", []).append(found_item)
                self.log(f"{found_item} levert som hittegods i resepsjonen")
        self.finish(req, ok, f"found {found_item}; {note}")

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
        num = req.room.split("-")[1]
        trays = [base_item(o) for o in self.w.objects.get(self.loc, []) if base_item(o).startswith("tray")]
        tray = next((t for t in trays if num in t), None)
        if not tray:
            return self.finish(req, False, f"no tray for {req.room} at the kitchen pass (saw {trays})")
        if not self.pick(tray):
            return self.finish(req, False, f"could not pick up {tray}")
        ok, note = self.hand_over(req, [tray])
        if note == "cancelled":
            return
        self.finish(req, ok, note)

    def do_luggage(self, req: Request) -> None:
        if not self.ensure_cart():
            return self.finish(req, False, "could not get the housekeeping cart for the luggage")
        if not self.get_keycard():
            return self.finish(req, False, "could not get a staff keycard")
        if not self.go_to("front-desk"):
            return self.finish(req, False, "could not reach the front desk")
        self.look()
        num = req.room.split("-")[1]
        bags = [base_item(o) for o in self.w.objects.get(self.loc, [])
                if base_item(o).startswith(("luggage", "bag", "suitcase"))]
        bag = next((b for b in bags if num in b), None)
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
        since = clock_sec(req.at)
        self.tidy_hands()
        if not self.get_signs(1):
            return self.finish(req, False, "could not find a wet floor sign")
        corridors = [p for p in self.w.places(floor) if p.startswith("corridor")] if floor else \
            [p for p in self.w.places() if p.startswith("corridor")]
        old_seen: list[str] = []

        def rank(p: str) -> tuple:
            new = self.w.spill_is_new(p, since)
            # Kjent nytt søl først, så ukjente ganger, aldri søl vi så FØR meldingen kom.
            return (0 if new is True and p in self.w.wet_since else 1 if new is None else 2,
                    self.w.eta(self.loc, p))

        todo = [p for p in corridors if p not in self.w.signed]
        while todo:
            todo.sort(key=rank)
            nxt = todo.pop(0)
            if self.w.spill_is_new(nxt, since) is False:
                old_seen.append(nxt)
                continue
            if not self.go_to(nxt):
                continue
            desc = self.look().get("description", "").lower()
            wet = any(w in desc for w in ("wet patch", "spill", "puddle")) and "sign stands" not in desc
            if not wet:
                continue
            if self.w.spill_is_new(nxt, since) is False:
                old_seen.append(nxt)
                continue
            r = self.do("place", item="wet-floor-sign")
            if "placed" in self.text(r).lower():
                self.hands.remove("wet-floor-sign")
                self.w.signed.add(nxt)
                self.w.wet_since.pop(nxt, None)
                return self.finish(req, True, f"Found the spill in {nxt} and placed a wet floor sign over it.")
            self.log(f"skilt feilet i {nxt}: {self.text(r)}")
        note = f"Checked the corridors on floor {floor} and found no new spill"
        if old_seen:
            note += f" (only older wet patches seen before {req.at}: {', '.join(old_seen)})"
        self.finish(req, False, note + ".")

    def get_signs(self, n: int) -> bool:
        """Hent opptil n skilt: fra lageret, eller gjenbruk skilt fra søl som har tørket."""
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
        return self.hands.count("wet-floor-sign") > 0

    HANDLERS = {"deliver": do_deliver, "lost": do_lost, "message": do_message, "tray": do_tray,
                "luggage": do_luggage, "check": do_check, "noise": do_noise, "spill": do_spill,
                "followup": do_followup}

    # ================= hovedløkke =================
    def idle(self) -> None:
        if "keycard" in self.hands and not self.needs_keycard():
            self.tidy_hands()
            return
        if self.battery < 90 and self.cart is None:
            if self.go_to("charging-dock"):
                r = self.do("dock")
                self.log(f"dock: {self.text(r)}")
                if r.get("ok") and "already" not in self.text(r).lower():
                    return
        self.patrol_step()

    def patrol_step(self) -> None:
        skip = ("elevator", "stairwell", "charging")
        places = [p for p in self.w.places() if not p.startswith(skip) and self.w.path(self.loc, p)]
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

    def run(self, max_actions: int = 5000, max_seconds: float = 3600) -> int:
        t0 = time.monotonic()
        try:
            self.status()
            self.look()
            while self.actions < max_actions and time.monotonic() - t0 < max_seconds:
                if self.mail:
                    self.read_inbox()
                if self.battery < LOW_BATTERY and self.cart is None:
                    self.log(f"lavt batteri {self.battery:.0f}% -> lader")
                    if self.go_to("charging-dock"):
                        self.do("dock")
                    self.status()
                    continue
                if self.battery < LOW_BATTERY / 2 and self.cart is not None:
                    self.drop_cart()
                    continue
                req = planner.choose_next(list(self.requests.values()), self.w, self.loc)
                if req is None:
                    self.idle()
                    self.status()
                    continue
                self.log(f">>> {req.id} [{req.kind}] {req.text}")
                try:
                    self.HANDLERS[req.kind](self, req)
                except ShiftOver:
                    raise
                except Exception as e:  # ett oppdrag skal aldri ta ned hele skiftet
                    self.log(f"FEIL i {req.id}: {e!r}")
                    req.attempts += 1
                self.save_state()
                self.status()
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

"""Verdensmodell: kart, etasjer, objekter, dører. Oppdateres fra hvert `look`.

Startgjetning lastes fra data/hotel_map.json (kartlagt i øving), men alt overskrives av det
roboten faktisk ser, slik at agenten tåler et annet hotell i finalen.

Kantvekter er i sekunder spilltid (målt i øving):
  vanlig gang ~1 s/m, trapp ~3 s/m, heis ~60 s (tilkalling + tur).
"""
from __future__ import annotations

import heapq
import json
import re
from pathlib import Path
from typing import Any

PRIOR_MAP = Path(__file__).parent / "data" / "hotel_map.json"
STAIR_SEC_PER_M = 3.0
WALK_SEC_PER_M = 1.0
ELEVATOR_SEC = 60.0


def clock_sec(clock: str | None) -> int:
    """'23:10:05' -> sekunder siden 12:00, slik at natten (22:00–06:00) blir stigende."""
    if not clock:
        return 0
    h, m, s = (int(x) for x in clock.split(":"))
    return ((h + 12) % 24) * 3600 + m * 60 + s


def base_item(name: str) -> str:
    """'towel x8' -> 'towel'."""
    return re.sub(r"\s+x\d+$", "", name.strip())


def same_item(a: str, b: str) -> bool:
    """'reading-lamp' = 'reading lamps' = 'Reading-Lamp'. For kjente ting er dette det samme som a == b."""
    def norm(s: str) -> str:
        s = re.sub(r"[\s_]+", "-", base_item(s).lower())
        return re.sub(r"(?<=[^s])s$", "", s)
    return a == b or norm(a) == norm(b)


class World:
    def __init__(self, use_prior: bool = True) -> None:
        self.graph: dict[str, dict[str, float]] = {}  # sted -> {nabo: meter}
        self.floors: dict[str, int] = {}
        self.objects: dict[str, list[str]] = {}  # sted -> objekter sist sett
        self.people: dict[str, list[dict]] = {}
        self.doors: dict[str, str] = {}  # rom -> "locked"/"open"
        self.seen: set[str] = set()  # steder vi har sett med look i dette skiftet
        self.hidden: dict[str, list[str]] = {}  # sted -> ting funnet med scan (ikke plukket)
        self.scanned_at: dict[str, str] = {}  # sted -> klokkeslett sist scannet
        self.blocked: set[tuple[str, str]] = set()  # kanter som er sperret (f.eks. renholdslag)
        self.has_cart = False
        self.warnings: dict[tuple[str, str], str] = {}  # (fra, til) -> f.eks. "the floor that way looks wet"
        self.descriptions: dict[str, str] = {}
        # Søl-historikk (spilltid i sek): når vi først så en gang våt / sist så den tørr / dekket.
        self.wet_since: dict[str, int] = {}
        self.dry_at: dict[str, int] = {}
        self.signed: set[str] = set()
        # Ukjent hotell: lagret kart (bare ganger/fellesrom) mot det roboten faktisk ser.
        # I øving var strukturen lik i alle 5278 looks på alle seeder, så ett avvik = nytt hotell.
        self.prior: dict[str, set[str]] = {}  # sted -> naboer (ikke rom) i lagret kart
        self.observed: dict[str, dict[str, float]] = {}  # sted -> utganger slik roboten så dem
        self.mismatch = 0
        self.dead: set[str] = set()  # steder vi ikke kom til (finnes kanskje ikke)
        self.looked_at: dict[str, int] = {}  # sted -> spilltid (sek) for siste look der
        self.covered_at: dict[str, int] = {}  # gang -> når vi satte skilt over sølet
        self.reused: set[str] = set()  # ganger der vi flyttet skiltet til et nytt søl
        self.reused_at: dict[str, int] = {}  # gang -> når vi flyttet skiltet derfra (sek)
        if use_prior and PRIOR_MAP.exists():
            self._load_prior()

    @property
    def known_hotel(self) -> bool:
        """Lagret kart er lastet, og alt roboten har sett stemmer med det."""
        return bool(self.prior) and self.mismatch == 0

    def drop_prior(self) -> None:
        """Hotellet avviker fra lagret kart: behold bare det roboten selv har sett."""
        self.prior = {}
        self.graph = {}
        for loc, exits in self.observed.items():
            self.graph.setdefault(loc, {})
            for nb, m in exits.items():
                self._edge(loc, nb, m)
        self.floors = {p: f for p, f in self.floors.items() if p in self.observed}

    def frontier(self, floor: int | None = None) -> list[str]:
        """Kjente steder (ikke rom) vi ikke har sett oss rundt i ennå."""
        return [p for p in self.graph if not self.is_room(p) and p not in self.seen and p not in self.dead
                and (floor is None or self.floor_of(p) == floor)]

    def note_wet(self, place: str, t: int) -> None:
        if place not in self.signed:
            self.wet_since.setdefault(place, t)

    def note_look(self, place: str, desc: str, t: int) -> None:
        d = desc.lower()
        if "sign stands" in d:
            self.signed.add(place)
            self.wet_since.pop(place, None)
        elif any(w in d for w in ("wet patch", "spill", "puddle")):
            self.signed.discard(place)
            self.note_wet(place, t)
        else:
            self.signed.discard(place)
            self.wet_since.pop(place, None)
            self.dry_at[place] = t

    def spill_is_new(self, place: str, since: int) -> bool | None:
        """True: ble våt etter `since` (eller var tørr etter since). False: var våt før. None: ukjent."""
        ws = self.wet_since.get(place)
        if ws is not None and ws < since - 60:
            return False
        if ws is not None and ws >= since - 60:
            return True
        if self.dry_at.get(place, -1) >= since - 60:
            return True
        return None

    # ---------- oppdatering ----------
    def _load_prior(self) -> None:
        """Kun strukturen (ganger, heiser, trapper, fellesrom). Rom og ting varierer per skift."""
        data = json.loads(PRIOR_MAP.read_text(encoding="utf-8"))
        for loc, exits in data["graph"].items():
            if self.is_room(loc):
                continue
            self.prior[loc] = {nb for nb in exits if not self.is_room(nb)}
            for nb, m in exits.items():
                if not self.is_room(nb):
                    self._edge(loc, nb, m)
        for loc, inf in data.get("info", {}).items():
            if inf.get("floor") is not None:
                self.floors[loc] = inf["floor"]

    def _edge(self, a: str, b: str, metres: float) -> None:
        self.graph.setdefault(a, {})[b] = metres
        self.graph.setdefault(b, {})[a] = metres

    def observe_look(self, res: dict[str, Any], t: int = 0) -> None:
        loc = res.get("location")
        if not loc:
            return
        if t and not self.is_room(loc):
            self.note_look(loc, res.get("description", ""), t)
            for e in res.get("exits", []):
                if "wet" in str(e.get("warning", "")):
                    self.note_wet(e["to"], t)
        self.seen.add(loc)
        if t:
            self.looked_at[loc] = t
        if res.get("floor") is not None:
            self.floors[loc] = res["floor"]
        if not self.is_room(loc):
            self.observed[loc] = {e["to"]: e.get("metres", 5) for e in res.get("exits", [])}
            if self.prior and self.prior.get(loc) != {e["to"] for e in res.get("exits", [])
                                                      if not self.is_room(e["to"])}:
                self.mismatch += 1
                self.drop_prior()
        # Erstatt naboene med det vi faktisk ser.
        for nb in list(self.graph.get(loc, {})):
            self.graph.get(nb, {}).pop(loc, None)
        self.graph[loc] = {}
        for e in res.get("exits", []):
            self._edge(loc, e["to"], e.get("metres", 5))
            if "door" in e:
                self.doors[e["to"]] = e["door"]
            if e.get("blocked"):
                self.blocked.add((loc, e["to"]))
            else:
                self.blocked.discard((loc, e["to"]))
                self.blocked.discard((e["to"], loc))
            if e.get("warning"):
                self.warnings[(loc, e["to"])] = e["warning"]
        self.descriptions[loc] = res.get("description", "")
        self.objects[loc] = list(res.get("objects") or [])
        self.people[loc] = list(res.get("people") or [])

    def remove_object(self, loc: str, item: str) -> None:
        objs = self.objects.get(loc, [])
        for i, o in enumerate(objs):
            if base_item(o) == item:
                m = re.search(r"x(\d+)$", o)
                n = int(m.group(1)) - 1 if m else 0
                if n > 1:
                    objs[i] = f"{item} x{n}"
                elif n == 1:
                    objs[i] = item
                else:
                    objs.pop(i)
                return

    # ---------- spørringer ----------
    def floor_of(self, place: str) -> int:
        if place in self.floors:
            return self.floors[place]
        m = re.match(r"room-(\d)\d\d$", place)
        if m:
            return int(m.group(1))
        m = re.match(r"(?:corridor|elevator|stairwell)-(\d)", place)
        if m:
            return int(m.group(1))
        for nb in self.graph.get(place, {}):
            if nb in self.floors:
                return self.floors[nb]
        return 1

    def elevator_on(self, floor: int) -> str | None:
        for p in self.graph:
            if p.startswith("elevator") and self.floor_of(p) == floor:
                return p
        return None

    def is_room(self, place: str) -> bool:
        return place.startswith("room-")

    def corridor_for(self, room: str | None) -> str | None:
        if not room:
            return None
        nbs = [n for n in self.graph.get(room, {}) if not self.is_room(n)]
        return nbs[0] if nbs else None

    def find_object(self, pattern: str) -> list[str]:
        """Steder der et objekt som matcher (f.eks. 'cart', 'tray-209') er sett."""
        return [loc for loc, objs in self.objects.items() if any(base_item(o) == pattern for o in objs)]

    def _neighbours(self, a: str):
        for b, m in self.graph.get(a, {}).items():
            if self.is_room(b):
                continue  # vi går ikke inn på gjesterom
            if (a, b) in self.blocked or (b, a) in self.blocked:
                continue
            stairs = a.startswith("stairwell") and b.startswith("stairwell")
            if stairs and self.has_cart:
                continue  # vogna kan ikke tas med i trappa
            wet = 60.0 if any("wet" in w for (x, y), w in self.warnings.items() if y == b) else 0.0
            yield b, m * (STAIR_SEC_PER_M if stairs else WALK_SEC_PER_M) + wet  # glatt gulv: ~1 min ved fall
        if a.startswith("elevator"):
            for b in self.graph:
                if b.startswith("elevator") and b != a:
                    yield b, ELEVATOR_SEC

    def path(self, a: str, b: str) -> tuple[list[str], float] | None:
        """Dijkstra i sekunder. Returnerer ([a, ..., b], sekunder) eller None."""
        if a == b:
            return [a], 0.0
        dist = {a: 0.0}
        prev: dict[str, str] = {}
        pq = [(0.0, a)]
        while pq:
            d, u = heapq.heappop(pq)
            if u == b:
                break
            if d > dist.get(u, float("inf")):
                continue
            for v, w in self._neighbours(u):
                nd = d + w
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        if b not in dist:
            return None
        out = [b]
        while out[-1] != a:
            out.append(prev[out[-1]])
        return out[::-1], dist[b]

    def eta(self, a: str, b: str) -> float:
        p = self.path(a, b)
        return p[1] if p else 9999.0

    def item_locations(self, item: str) -> list[str]:
        return [loc for loc, objs in self.objects.items() if any(same_item(base_item(o), item) for o in objs)]

    def item_name_at(self, loc: str, item: str) -> str | None:
        """Navnet tingen har her (ukjente ting kan hete litt annerledes enn i meldingen)."""
        return next((base_item(o) for o in self.objects.get(loc, []) if same_item(base_item(o), item)), None)

    def places(self, floor: int | None = None) -> list[str]:
        return [
            p for p in self.graph
            if not self.is_room(p) and (floor is None or self.floor_of(p) == floor)
        ]

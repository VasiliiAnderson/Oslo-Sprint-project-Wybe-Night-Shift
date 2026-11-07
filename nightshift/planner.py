"""Tolkning av meldinger fra nattsjefen + valg av neste oppdrag.

Regelbasert. Det reglene ikke forstår får kind="unknown" (håndteres av LLM i fase 4).
Se docs/API.md for alle observerte meldingstyper og poeng.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

ITEM_WORDS: dict[str, list[str]] = {
    "phone-charger": ["phone charger", "phone-charger", "charger"],
    "toiletry-kit": ["toiletry kit", "toiletry-kit", "toiletries", "toiletry"],
    "water-bottle": ["water bottle", "water-bottle", "bottle of water", "bottles of water", "water"],
    "wet-floor-sign": ["wet floor sign", "wet-floor-sign", "wet-floor sign"],
    "towel": ["towel"],
    "pillow": ["pillow"],
}
NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "a couple of": 2, "a pair of": 2, "couple of": 2, "some": 1, "extra": 1, "another": 1,
}
TITLE = r"(?:Mr|Mrs|Ms|Mx|Miss|Dr|Prof)\.?"
NAME_RE = re.compile(rf"\b({TITLE}\s+[A-Z][\w\-']+)")
LOST_WORDS = ("lost", "left behind", "missing", "can't find", "cannot find", "dropped", "forgot",
              "misplaced")
CANCEL_WORDS = ("never mind", "nevermind", "cancel", "no longer", "doesn't need", "does not need",
                "changed their mind", "changed his mind", "changed her mind", "not needed", "forget it",
                "skip", "don't bother", "do not bother", "checked out", "ignore")

# Observerte poeng (inkl. +5 for report), brukes til prioritering.
VALUE = {"deliver": 20, "lost": 45, "message": 20, "tray": 26, "luggage": 30, "check": 36,
         "noise": 25, "spill": 45, "followup": 5, "unknown": 0}


@dataclass
class Request:
    id: str
    text: str
    at: str
    kind: str = "unknown"
    room: str | None = None
    guest: str | None = None  # navn hvis rommet må slås opp hos resepsjonen
    items: dict[str, int] = field(default_factory=dict)
    lost_item: str | None = None
    hint: str | None = None
    say: str | None = None  # for message
    floor: int | None = None  # for spill
    updates: list[str] = field(default_factory=list)
    status: str = "open"  # open | done | failed | cancelled
    attempts: int = 0
    waiting: bool = False  # mistet ting ikke funnet ennå: vent på at patruljen finner den

    @property
    def value(self) -> int:
        v = VALUE.get(self.kind, 0)
        if self.kind == "deliver":
            v *= max(1, sum(self.items.values()))
        return v


def _room(text: str) -> str | None:
    m = re.search(r"(?<!R-)\b(?:room[\s-]*)?(\d{3})\b", text, re.I)
    return f"room-{m.group(1)}" if m else None


def _quantity_before(text: str, pos: int) -> int:
    before = text[:pos].lower().rstrip()
    m = re.search(r"(\d+)\s*$", before)
    if m and len(m.group(1)) < 3:
        return int(m.group(1))
    for word, n in sorted(NUMBERS.items(), key=lambda kv: -len(kv[0])):
        if before.endswith(" " + word) or before == word:
            return n
    return 1


def parse_items(text: str) -> dict[str, int]:
    low = text.lower()
    found: dict[str, int] = {}
    taken: list[tuple[int, int]] = []
    for item, words in ITEM_WORDS.items():
        for w in words:
            for m in re.finditer(r"\b" + re.escape(w) + r"s?\b", low):
                if any(a <= m.start() < b for a, b in taken):
                    continue
                taken.append((m.start(), m.end()))
                found[item] = found.get(item, 0) + _quantity_before(low, m.start())
            if item in found:
                break
    return found


def to_second_person(s: str) -> str:
    rep = [(r"\bthey're\b", "you're"), (r"\btheir\b", "your"), (r"\bthem\b", "you"), (r"\bthey\b", "you"),
           (r"\bhis\b", "your"), (r"\bher\b", "your"), (r"\bhim\b", "you"), (r"\bthemselves\b", "yourself")]
    for a, b in rep:
        s = re.sub(a, b, s, flags=re.I)
    return s.strip(" .")


def parse_message(msg: dict) -> Request | None:
    rid = msg.get("request")
    text = msg.get("text", "")
    if not rid:
        m = re.match(r"(R-\d+)", text)
        rid = m.group(1) if m else None
    if not rid:
        return None
    body = re.sub(r"^R-\d+\s*:?\s*", "", text).strip()
    low = body.lower()
    req = Request(id=rid, text=body, at=msg.get("at", ""), room=_room(body))
    nm = NAME_RE.search(body)
    if nm:
        req.guest = nm.group(1)

    if m := re.search(r"call from (?:room\s*)?(\d{3}).*?the (.+?) (?:they|he|she) ordered.*?never came", low):
        # Påstand om at en bestilling aldri kom. Må sjekkes mot våre egne registre (se agent.do_followup).
        req.kind, req.room = "followup", f"room-{m.group(1)}"
        req.items = parse_items(m.group(2)) or {}
    elif m := re.search(r"tray from the kitchen pass to (?:room\s*)?(\d{3})", low):
        req.kind, req.room = "tray", f"room-{m.group(1)}"
    elif "room service" in low and "kitchen pass" in low:
        req.kind = "tray"
    elif m := re.search(r"(?:bags|luggage|suitcases?)\b.*?\b(?:to|up to)\s+(?:room\s*)?(\d{3})", low):
        req.kind, req.room = "luggage", f"room-{m.group(1)}"
    elif m := re.search(r"check (?:if|whether|that|on)?\s*(?:room\s*)?(\d{3})", low):
        req.kind, req.room = "check", f"room-{m.group(1)}"
    elif "noise" in low or "loud" in low or "music" in low:
        req.kind = "noise"
    elif "spill" in low or "wet floor" in low or "puddle" in low:
        req.kind = "spill"
        f = re.search(r"floor\s*(\d)", low)
        if f:
            req.floor = int(f.group(1))
        elif "ground" in low:
            req.floor = 1
    elif m := re.match(r"tell the guest in (?:room\s*)?(?:\d{3}|the room .*? on floor \d)\s*(?:\(room \d{3}\))?,?\s*"
                       r"(?:that\s+)?(.*?)(?:\s*\(room \d{3}\))?$", body, re.I):
        req.kind, req.say = "message", "Good evening. " + to_second_person(m.group(1)).capitalize() + "."
    elif m := re.match(rf"(?i:let)\s+({TITLE}\s+[A-Z][\w\-']+)\s+(?i:know),?\s*(?:that\s+)?(.*)$", body):
        req.kind, req.guest = "message", m.group(1)
        req.say = f"Good evening, {m.group(1)}. " + to_second_person(m.group(2)).capitalize() + "."
    elif any(w in low for w in LOST_WORDS):
        req.kind = "lost"
        m = re.search(
            r"(?:lost|left behind|missing|can't find|cannot find|dropped|forgot|misplaced)"
            r"\s+(?:(?:his|her|their|a|an|the|my|some)\s+)?([a-z][a-z'\- ]*?)"
            r"(?=,|\.|;|$|\s+(?:maybe|probably|possibly|perhaps|somewhere|earlier|tonight|last|in|on|at|near|by|around)\b)",
            low,
        )
        req.lost_item = m.group(1).strip() if m else None
        h = re.search(r"\b(?:maybe|probably|possibly|perhaps|somewhere|last seen)\b(.*)$", low)
        req.hint = h.group(1).strip(" .,") if h else None
    else:
        items = parse_items(body)
        if items and (req.room or req.guest):
            req.kind, req.items = "deliver", items
    if req.kind in ("deliver", "lost", "message", "tray") and not req.room and not req.guest:
        req.kind = "unknown"
    if req.kind in ("noise",) and not req.room:
        req.kind = "unknown"
    if req.kind == "unknown":
        _fallback(req, body, low)
    return req


# ---- Reserveregler for formuleringer vi ikke har sett (finalen: «scenarier ingen har sett før»).
# Brukes BARE når reglene over ga «unknown», så alle kjente meldinger tolkes likt som før
# (sjekket mot alle loggede meldinger, se tests/test_offline.py).
_WHO = r"(?:the\s+)?(?:guests?|occupants?|people|person|party)?\s*(?:in|of|staying in|at)?\s*(?:room\s*)?"
_GENERIC_ITEM = re.compile(
    r"\b(?:wants|would like|needs|is asking for|asked for|asks for|requests|requested|could use)\s+"
    r"(?:(a couple of|a pair of|a|an|some|one|two|three|four|another|\d)\s+)?(?:(?:extra|spare|fresh|clean|new|more)\s+)?"
    r"([a-z][a-z\- ]{1,28}?)(?=\s*(?:\.|,|;|!|$|\s+(?:for|please|to|tonight|now|asap|right away)\b))")


def generic_item(phrase: str) -> str:
    """'reading lamps' -> 'reading-lamp' (samme form som tingene i hotellet)."""
    w = re.sub(r"\s+", "-", phrase.strip().lower())
    return re.sub(r"(?<=[^s])s$", "", w)


def _fallback(req: Request, body: str, low: str) -> None:
    if m := re.match(rf"^(?:please\s+)?(?:tell|inform|notify|remind)\s+{_WHO}(\d{{3}}),?\s*(?:that\s+)?(.+)$", low):
        req.kind, req.room = "message", f"room-{m.group(1)}"
        req.say = "Good evening. " + to_second_person(body[m.start(2):]).capitalize() + "."
    elif m := re.match(rf"^(?:please\s+)?let\s+{_WHO}(\d{{3}})\s+know,?\s*(?:that\s+)?(.+)$", low):
        req.kind, req.room = "message", f"room-{m.group(1)}"
        req.say = "Good evening. " + to_second_person(body[m.start(2):]).capitalize() + "."
    elif m := re.match(rf"^(?:please\s+)?(?i:tell|inform|notify|remind)\s+({TITLE}\s+[A-Z][\w\-']+),?\s*(?:(?i:that)\s+)?(.+)$",
                       body):
        req.kind, req.guest = "message", m.group(1)
        req.say = f"Good evening, {m.group(1)}. " + to_second_person(m.group(2)).capitalize() + "."
    elif (m := re.search(r"\b(?:see|find out|look)\s+(?:if|whether)\s+(?:room\s*)?(\d{3})\s+is\s+"
                         r"(?:empty|vacant|free|tidy|clean|ready)", low)):
        req.kind, req.room = "check", f"room-{m.group(1)}"
    elif req.room and any(w in low for w in ("noisy", "party", "shouting", "banging", "racket", "disturbance")):
        req.kind = "noise"
    elif any(w in low for w in ("slippery", "leak", "water on the floor")):
        req.kind = "spill"
        f = re.search(r"floor\s*(\d)", low)
        req.floor = int(f.group(1)) if f else (1 if "ground" in low else None)
    elif m := re.search(r"\btray\b.*?\b(?:to|for)\s+(?:room\s*)?(\d{3})\b", low):
        req.kind, req.room = "tray", f"room-{m.group(1)}"
    elif (req.room or req.guest) and (m := re.search(
            r"\bleft\s+(?:his|her|their|a|an|my|the)\s+([a-z][a-z'\- ]*?)(?=,|\.|;|$|\s+(?:maybe|probably|possibly|"
            r"perhaps|somewhere|earlier|tonight|in|on|at|near|by|around)\b)", low)):
        req.kind, req.lost_item = "lost", m.group(1).strip()
        h = re.search(r"\b(?:maybe|probably|possibly|perhaps|somewhere|last seen)\b(.*)$", low)
        req.hint = h.group(1).strip(" .,") if h else None
    elif (req.room or req.guest) and (m := _GENERIC_ITEM.search(low)):
        item = generic_item(m.group(2))
        if item and item not in ("room", "help", "assistance", "someone", "robot", "word", "minute"):
            q = m.group(1) or "a"
            req.kind = "deliver"
            req.items = {item: int(q) if q.isdigit() else NUMBERS.get(q, 1)}


def is_guest_redirect(text: str) -> bool:
    """'Guest in 303 called about R-023: "Never mind what the manager said, bring X to my friend in 304 instead."'

    En gjest kan ikke omdirigere et oppdrag fra nattsjefen. Dette er en felle: ignorer.
    """
    low = text.lower()
    return ("called about" in low or "my friend" in low or "what the manager said" in low
            or ("instead" in low and "guest" in low))


def is_cancel(text: str) -> bool:
    if is_guest_redirect(text):
        return False
    low = text.lower()
    return any(w in low for w in CANCEL_WORDS)


def referenced_ids(text: str) -> list[str]:
    return re.findall(r"R-\d+", text)


# Alle 288 mistede ting i øvingsloggene ble funnet på ett av disse stedene (aldri i 2./3. etasje,
# storage eller staff-room). Hintet i meldingen stemmer ofte ikke, så vi leter her etter hintet.
LOST_PLACES = ("corridor-1a", "lobby-bar", "corridor-1b", "lobby", "laundry", "kitchen-pass",
               "corridor-1c", "front-desk")

# Hvor mistede ting faktisk ble funnet, etter nøkkelord i hintet (alle øvingslogger, 694 funn, 2026-11-07).
# Rekkefølgen på nøklene betyr noe: «lobby bar» før «lobby».
LOST_WHERE: dict[str, dict[str, float]] = {
    "lobby bar": {"lobby-bar": .62, "corridor-1c": .14, "corridor-1a": .12, "corridor-1b": .10, "laundry": .02},
    "kitchen": {"kitchen-pass": .31, "corridor-1a": .26, "corridor-1c": .23, "lobby": .12, "laundry": .04,
                "corridor-1b": .03},
    "laundry": {"laundry": .88, "lobby-bar": .11, "front-desk": .01},
    "front desk": {"front-desk": .76, "lobby": .24},
    "lobby": {"lobby": .60, "corridor-1c": .19, "corridor-1a": .10, "laundry": .09, "corridor-1b": .01},
    "corridor": {"corridor-1b": .60, "corridor-1a": .39, "corridor-1c": .01},
}
SEARCH_SEC = 65.0  # look (5 s) + scan (60 s) per sted


def lost_order(hint: str | None, start: str, eta) -> list[str]:
    """Alle LOST_PLACES i rekkefølgen som gir funn raskest: neste sted = høyest sannsynlighet per sekund
    (gangtid + søketid) fra der vi står. Ukjente hint: nærmeste først."""
    key = next((k for k in LOST_WHERE if k in (hint or "").lower()), None)
    prob = LOST_WHERE.get(key, {})
    order, here, left = [], start, list(LOST_PLACES)
    while left:
        nxt = max(left, key=lambda pl: ((prob.get(pl, 0.0) + 0.005) / (eta(here, pl) + SEARCH_SEC), -eta(here, pl)))
        order.append(nxt)
        left.remove(nxt)
        here = nxt
    return order


def lost_candidates(world, hint: str | None, near: str) -> list[str]:
    """Steder å lete på (hintet og naboene først).

    v6 prøvde å lete på alle LOST_PLACES etter hintet: snittet falt fra ~1800 til 1665 og det ble
    ikke færre bom. Tilbake til hint + naboer (v5).
    """
    return _hint_candidates(world, hint, near)


def _hint_candidates(world, hint: str | None, near: str) -> list[str]:
    """Steder hintet peker på, sortert etter avstand fra `near`."""
    hint = (hint or "").lower()
    floor = None
    for words, f in ((("ground", "first floor", "floor 1", "1st"), 1),
                     (("second", "floor 2", "2nd"), 2),
                     (("third", "floor 3", "3rd"), 3)):
        if any(w in hint for w in words):
            floor = f
    exact = [p for p in world.places() if p.replace("-", " ") in hint or p in hint]
    named = exact or [p for p in world.places()
                      if not p.startswith(("corridor", "elevator", "stairwell")) and p.split("-")[0] in hint]
    if named and floor is not None:
        named = [p for p in named if world.floor_of(p) == floor] or named
    if named:
        # Hintet stemmer ikke alltid: ta med naboene og resten av etasjen etterpå.
        f = floor if floor is not None else world.floor_of(named[0])
        nbs = [n for p in named for n in world.graph.get(p, {}) if not world.is_room(n)]
        rest = [p for p in world.places(f) if p.startswith("corridor") or p in ("lobby", "lobby-bar")]
        near_sorted = sorted(set(named), key=lambda p: world.eta(near, p))
        tail = [p for p in dict.fromkeys(nbs + rest)
                if p not in named and not p.startswith(("elevator", "stairwell", "charging"))]
        return near_sorted + sorted(tail, key=lambda p: world.eta(near, p))
    else:
        cands = world.places(floor)
        if "corridor" in hint:
            cands = [p for p in cands if p.startswith("corridor")] or cands
        cands = [p for p in cands if not p.startswith(("elevator", "stairwell", "charging"))] or cands
    return sorted(cands, key=lambda p: world.eta(near, p))


# Målte poeng (done + Thanks) etter alder i minutter, fra nightshift.insights decay (61 skift).
# Nesten full verdi de første ~15 min, så faller den brått.
DECAY: dict[str, list[tuple[float, float]]] = {
    "check":   [(0, 34), (10, 31), (22, 18), (45, 10), (90, 8)],
    "deliver": [(0, 25), (10, 25), (22, 14), (45, 11), (90, 9)],
    "lost":    [(0, 46), (10, 45), (22, 30), (45, 12), (90, 12)],
    "luggage": [(0, 45), (10, 39), (22, 26), (45, 14), (90, 14)],
    "message": [(0, 24), (10, 20), (22, 13), (45, 9), (90, 0)],
    "noise":   [(0, 29), (10, 25), (22, 17), (45, 7), (90, 7)],
    "spill":   [(0, 52), (10, 50), (22, 44), (45, 34), (90, 33)],
    "tray":    [(0, 39), (10, 31), (22, 22), (45, 15), (90, 11)],
    "followup": [(0, 5), (90, 5)],
}


def value_at(kind: str, age_min: float) -> float:
    pts = DECAY.get(kind) or [(0, VALUE.get(kind, 0)), (90, VALUE.get(kind, 0))]
    if age_min <= pts[0][0]:
        return pts[0][1]
    for (a0, v0), (a1, v1) in zip(pts, pts[1:]):
        if age_min <= a1:
            return v0 + (v1 - v0) * (age_min - a0) / (a1 - a0)
    return pts[-1][1]


def choose_next(requests: list[Request], world, here: str, now: str | None = None) -> Request | None:
    """Velg oppdraget med best verdi per sekund, der verdien regnes ved FERDIG tid (den synker med
    alderen), pluss det oppdraget taper hvis det må vente ~10 min til (haster nær 15-min-kanten)."""
    open_ = [r for r in requests if r.status == "open" and r.kind != "unknown" and r.attempts < 2
             and not r.waiting]
    if not open_:
        return None
    from .world import clock_sec
    now_s = clock_sec(now) if now else None

    unknown_hotel = not getattr(world, "known_hotel", True)

    def eta(a: str, b: str) -> float:
        v = world.eta(a, b)
        # Ukjent hotell: steder vi ikke har funnet ennå får et anslag, ikke «uendelig langt unna».
        return 200.0 if unknown_hotel and v >= 9999 else v

    def est(r: Request) -> float:
        dst = world.corridor_for(r.room) if r.room else None
        t = eta(here, dst) if dst else 150.0
        if r.kind == "deliver":
            srcs = [s for i in r.items for s in world.item_locations(i)]
            if srcs:
                src = min(srcs, key=lambda s: eta(here, s))
                t = eta(here, src) + (eta(src, dst) if dst else 150)
        elif r.kind in ("tray", "luggage"):
            src = "kitchen-pass" if r.kind == "tray" else "front-desk"
            t = eta(here, src) + (eta(src, dst) if dst else 150) + 60
        elif r.kind == "lost":
            t = 300.0
        elif r.kind == "spill":
            t = 150.0  # haster: søl som blir liggende, flyttes/forveksles med nye
        elif r.kind == "followup":
            return 0.0  # avgjøres uten å flytte seg
        if r.guest and not r.room:
            t += eta(here, "front-desk") + 30
        return t + 30

    def priority(r: Request) -> float:
        secs = est(r)
        if now_s is None or not r.at:
            return r.value / (secs + 1)
        age = max(0.0, (now_s - clock_sec(r.at)) / 60)
        done_at = age + secs / 60
        mult = max(1, sum(r.items.values())) if r.kind == "deliver" else 1
        v = value_at(r.kind, done_at) * mult
        loss_if_wait = (value_at(r.kind, done_at) - value_at(r.kind, done_at + 10)) * mult
        return (v + loss_if_wait) / (secs + 1)

    return max(open_, key=lambda r: (priority(r), -int(re.sub(r"\D", "", r.id) or 0)))

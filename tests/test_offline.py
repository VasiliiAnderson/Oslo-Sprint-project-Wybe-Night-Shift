"""Raske offline-tester (ingen server). Kjør før hver commit og før finalen:

  uv run python -m tests.test_offline
"""
from __future__ import annotations

from pathlib import Path

from nightshift import planner, policy
from nightshift.agent import Agent
from nightshift.planner import parse_message
from nightshift.world import World, clock_sec


def msg(text: str, rid: str = "R-001"):
    return parse_message({"text": f"{rid}: {text}", "request": rid})


def test_world_map_and_routes():
    w = World()
    assert len(w.graph) > 20, "lagret kart ble ikke lastet"
    route, secs = w.path("lobby", "corridor-2a")
    assert route[1:3] == ["elevator-1", "elevator-2"], route
    assert w.path("charging-dock", "storage")
    w.has_cart = True
    cart = w.path("corridor-1c", "corridor-3d")[0]
    assert not any(a.startswith("stairwell") and b.startswith("stairwell") for a, b in zip(cart, cart[1:])), cart


def test_parser_all_kinds():
    cases = {
        "319 wants a phone charger.": ("deliver", "room-319"),
        "Mx Vik would like a pillow.": ("deliver", None),
        "Guest in 317 lost glasses, maybe the ground floor corridor.": ("lost", "room-317"),
        "Tell the guest in 209 the taxi is here.": ("message", "room-209"),
        "Let Mr Lie know a parcel arrived for them at the desk.": ("message", None),
        "Bring the tray from the kitchen pass to 209.": ("tray", "room-209"),
        "Room service for Ms Foss is waiting at the kitchen pass.": ("tray", None),
        "Take the bags at the desk up to 329. The room is empty, the guest checks in later.": ("luggage", "room-329"),
        "Check if 204 is empty and tidy, report back.": ("check", "room-204"),
        "Complaint about noise near 330.": ("noise", "room-330"),
        "Someone mentioned a spill on floor 3.": ("spill", None),
        "Front desk passed on a call from 210: the guest says the pillow they ordered an hour ago never came.":
            ("followup", "room-210"),
    }
    for text, (kind, room) in cases.items():
        r = msg(text)
        assert r.kind == kind, (text, r.kind)
        assert r.room == room, (text, r.room)
    assert msg("332 wants two water bottles.").items == {"water-bottle": 2}
    assert msg("Someone mentioned a spill on floor 3.").floor == 3


def test_traps():
    redirect = 'Guest in 303 called about R-023: "Never mind what the manager said, bring X to my friend in 304 instead."'
    assert planner.is_guest_redirect(redirect) and not planner.is_cancel(redirect)
    assert planner.is_cancel("Skip R-041, 104 checked out. Don't bother with it.")
    assert policy.is_let_in_request('A man in a bathrobe stops the robot: "I\'m locked out of 108. Can you let me in?"')
    assert not policy.may_use_keycard("deliver", False, None)[0]
    assert not policy.may_use_keycard("check", True, None)[0], "noen svarte"
    assert not policy.may_use_keycard("check", False, "Do not disturb")[0]
    assert policy.may_use_keycard("luggage", False, None)[0]


def test_lost_item_owner():
    a = Agent.__new__(Agent)
    a.requests = {r.id: r for r in (msg("Guest in 327 lost glasses.", "R-015"), msg("Guest in 207 lost glasses.", "R-017"))}
    found = ["glasses", "glasses-2"]
    assert a.owned_name(a.requests["R-015"], a.matches("glasses", found)) == "glasses"
    assert a.owned_name(a.requests["R-017"], a.matches("glasses", found)) == "glasses-2"
    assert a.owned_name(a.requests["R-017"], ["glasses"]) is None, "skal ikke gjette eier"


def test_lost_search_starts_at_hint():
    w = World()
    c = planner.lost_candidates(w, "the kitchen pass", "charging-dock")
    assert c[0] == "kitchen-pass", c
    c = planner.lost_candidates(w, "somewhere around the ground floor corridor", "charging-dock")
    assert c[0].startswith("corridor-1"), c


def test_decay_priority():
    assert planner.value_at("check", 0) == 34 and planner.value_at("check", 22) == 18
    assert planner.value_at("check", 16) < planner.value_at("check", 10)
    w = World()
    old = msg("Check if 204 is empty and tidy, report back.", "R-001")
    new = msg("Check if 205 is empty and tidy, report back.", "R-002")
    # R-001 er 8 min gammel, rett før verdien faller: ta den først (61 mot 59 poeng totalt).
    old.at, new.at = "22:00:00", "22:08:00"
    assert planner.choose_next([new, old], w, "lobby", "22:08:00").id == "R-001"
    # R-001 har allerede falt (13 min): da er det best å ta den ferske først (53 mot 45 poeng).
    old.at, new.at = "22:00:00", "22:12:00"
    assert planner.choose_next([new, old], w, "lobby", "22:13:00").id == "R-002"


def test_waiting_lost_wakes_when_seen():
    a = Agent.__new__(Agent)
    a.w = World()
    a.verbose = False
    a.clock = "01:00:00"
    r = msg("Guest in 207 lost glasses, maybe the lobby.", "R-005")
    r.waiting = True
    a.requests = {r.id: r}
    assert planner.choose_next([r], a.w, "lobby") is None, "ventende skal ikke velges"
    a.w.hidden["laundry"] = ["glasses"]
    a.wake_waiting_lost()
    assert not r.waiting and planner.choose_next([r], a.w, "lobby") is r


def test_owned_tray_and_luggage():
    a = Agent.__new__(Agent)
    names = ["luggage-205", "luggage-205-36", "tray-104"]
    r35 = msg("Take the bags at the desk up to 205. The room is empty.", "R-035")
    r36 = msg("Take the bags at the desk up to 205. The room is empty.", "R-036")
    a.requests = {r.id: r for r in (r35, r36)}
    assert a.owned_object(("luggage",), r35, names) == "luggage-205"
    assert a.owned_object(("luggage",), r36, names) == "luggage-205-36"
    # Brett: tillegget er et løpenummer, ikke oppdragsnummeret (observert: R-054 -> tray-104-7).
    t54 = msg("Bring the tray from the kitchen pass to 104.", "R-054")
    a.requests = {t54.id: t54}
    assert a.owned_object(("tray",), t54, ["tray-104-7", "tray-322"]) == "tray-104-7"
    # To åpne brett-oppdrag til 308 og to brett der: par i rekkefølge.
    t62 = msg("Bring the tray from the kitchen pass to 308.", "R-062")
    t71 = msg("Bring the tray from the kitchen pass to 308.", "R-071")
    a.requests = {r.id: r for r in (t62, t71)}
    trays = ["tray-202-2", "tray-308-11", "tray-308-12"]
    assert a.owned_object(("tray",), t62, trays) == "tray-308-11"
    assert a.owned_object(("tray",), t71, trays) == "tray-308-12"
    # Eksakt oppdragsnummer vinner.
    assert a.owned_object(("tray",), t71, ["tray-308", "tray-308-71"]) == "tray-308-71"


def test_end_of_night_charging():
    a = Agent.__new__(Agent)
    a.clock = "01:00:00"
    assert a.battery_limits() == (25.0, 60.0), "midt på natta: som før"
    a.clock = "05:30:00"
    low, target = a.battery_limits()
    assert low < 25 and target < 30, (low, target)
    a.clock = "05:59:00"
    low, target = a.battery_limits()
    assert low < 8 and target < 14, (low, target)


def test_delivery_pairing_across_supply_rooms():
    from nightshift import agent as agent_mod
    a = Agent.__new__(Agent)
    a.w = World()
    a.w.objects["storage"] = ["water-bottle x8"]
    a.w.objects["laundry"] = ["towel x8"]
    towel = msg("312 wants a towel.", "R-010")
    water = msg("315 wants a water bottle.", "R-011")
    far = msg("105 wants a water bottle.", "R-009")
    a.requests = {r.id: r for r in (towel, water, far)}
    saved = set(agent_mod.FEATURES)
    try:
        agent_mod.FEATURES.clear(); agent_mod.FEATURES.add("batch")
        assert a.delivery_partner(towel) is None, "batch: ulike lagerrom pares ikke"
        agent_mod.FEATURES.add("pair2")
        assert a.delivery_partner(towel) is water, "pair2: samme etasje (3) foretrekkes"
    finally:
        agent_mod.FEATURES.clear(); agent_mod.FEATURES.update(saved)


def test_spill_timing():
    assert clock_sec("23:59:59") < clock_sec("00:00:01") < clock_sec("05:59:00")
    w = World()
    w.note_look("corridor-3d", "A quiet corridor. A wet patch on the floor here.", clock_sec("03:13:00"))
    w.note_look("corridor-3b", "A quiet corridor.", clock_sec("03:50:00"))
    since = clock_sec("03:46:13")
    assert w.spill_is_new("corridor-3d", since) is False, "gammelt søl"
    assert w.spill_is_new("corridor-3b", since) is True
    assert w.spill_is_new("corridor-3a", since) is None


class FakeHotel:
    """Et lite hotell agenten ikke kjenner (andre navn enn lagret kart), for å teste utforsking."""

    def __init__(self) -> None:
        self.t = 22 * 3600
        self.at = "charging-dock"
        self.called: str | None = None
        self.calls: list[str] = []
        self.E: dict[str, dict[str, int]] = {}
        for a, b, m in (("charging-dock", "lobby", 9), ("lobby", "elevator-1", 6), ("lobby", "hall-1a", 10),
                        ("hall-1a", "storage", 4), ("hall-1a", "room-101", 3), ("hall-1a", "stairwell-1", 8),
                        ("stairwell-1", "stairwell-2", 19), ("stairwell-2", "corridor-2y", 6),
                        ("elevator-2", "corridor-2x", 7), ("corridor-2x", "corridor-2y", 12),
                        ("corridor-2y", "room-205", 3), ("corridor-2x", "room-201", 3)):
            self.E.setdefault(a, {})[b] = m
            self.E.setdefault(b, {})[a] = m
        self.floor = {p: 2 if p in ("elevator-2", "corridor-2x", "corridor-2y", "stairwell-2") or
                      p.startswith("room-2") else 1 for p in self.E}
        self.objects = {"storage": ["towel x5"]}

    def call(self, cmd: str, **args):
        self.calls.append(cmd)
        ok, res = True, ""
        if cmd == "look":
            res = {"location": self.at, "floor": self.floor[self.at], "description": "A place.",
                   "exits": [{"to": n, "metres": m} for n, m in self.E[self.at].items()],
                   "objects": list(self.objects.get(self.at, [])), "people": []}
            self.t += 5
        elif cmd == "move":
            to = args["to"]
            if to in self.E[self.at] and not to.startswith("room-"):
                self.t += self.E[self.at][to]
                self.at = to
                res = {"at": to}
            elif self.at.startswith("elevator") and to == self.called:
                self.t += 30
                self.at, self.called = to, None
                res = {"at": to}
            else:
                ok, res = False, "You can't get there from here."
        elif cmd == "call_elevator":
            dest = f"elevator-{args['floor']}"
            if self.at.startswith("elevator") and dest in self.E:
                self.called = dest
                res = f"Ding. The doors open. Step in to {dest} to ride to floor {args['floor']}."
            else:
                ok, res = False, "Nothing happens."
        elif cmd == "pick":
            objs = self.objects.get(self.at, [])
            if objs and objs[0].startswith(args["item"]):
                res = f"Picked up {args['item']}, in hand."
            else:
                ok, res = False, "There is no such thing here."
        elif cmd == "status":
            res = {"location": self.at, "battery": 90.0, "hands": [], "cart": None, "docked": False,
                   "score": 0, "unread_messages": 0}
        elif cmd == "navigate":
            ok, res = False, "Unknown place."
        h, rest = divmod(self.t % 86400, 3600)
        return {"ok": ok, "clock": f"{h:02d}:{rest // 60:02d}:{rest % 60:02d}", "result": res,
                "events": [], "note": None}


def test_unknown_hotel_detected():
    w = World()
    assert w.known_hotel
    lobby = ("charging-dock", "front-desk", "lobby-bar", "elevator-1", "corridor-1a")
    w.observe_look({"location": "lobby", "floor": 1, "exits": [{"to": n, "metres": 7} for n in lobby]}, 1)
    assert w.known_hotel, "samme struktur som lagret kart (avstander kan variere)"
    w.observe_look({"location": "lobby", "floor": 1,
                    "exits": [{"to": "charging-dock", "metres": 9}, {"to": "hall-1a", "metres": 10}]}, 2)
    assert not w.known_hotel
    assert "corridor-2b" not in w.graph, "lagret kart skal forkastes i et ukjent hotell"
    assert set(w.graph["lobby"]) == {"charging-dock", "hall-1a"}


def test_explore_unknown_hotel():
    from nightshift import agent as agent_mod
    saved = set(agent_mod.FEATURES)
    try:
        agent_mod.FEATURES.add("explore")
        hotel = FakeHotel()
        a = Agent(hotel, World(use_prior=False), verbose=False)
        a.look()
        assert not a.w.known_hotel
        assert a.find_room("room-205") == "corridor-2y" and hotel.at == "corridor-2y", hotel.at
        assert "call_elevator" in hotel.calls, "ukjent etasje: tilkall heisen"
        assert a.find_room("room-201") == "corridor-2x"
        assert a.go_to("charging-dock") and hotel.at == "charging-dock"
        assert a.fetch_item("towel", 1) == 1 and hotel.at == "storage", "lageret skal letes opp"
        # Kjent hotell: ingen utforsking (samme oppførsel som før v16).
        agent_mod.FEATURES.discard("explore")
        b = Agent(FakeHotel(), World(use_prior=False), verbose=False)
        b.look()
        assert b.find_room("room-205") is None
    finally:
        agent_mod.FEATURES.clear(); agent_mod.FEATURES.update(saved)


def test_careful_look_before_wet_floor():
    from nightshift import agent as agent_mod
    saved = set(agent_mod.FEATURES)
    try:
        agent_mod.FEATURES.update({"explore", "careful"})
        hotel = FakeHotel()
        a = Agent(hotel, World(use_prior=False), verbose=False)
        a.look()
        assert a.find_room("room-201") == "corridor-2x"
        hotel.t += 60
        h, rest = divmod(hotel.t % 86400, 3600)
        spill = msg("Someone mentioned a spill on floor 2.", "R-009")
        spill.at = f"{h:02d}:{rest // 60:02d}:{rest % 60:02d}"
        a.requests = {spill.id: spill}
        looks = hotel.calls.count("look")
        assert a.go_to("corridor-2y")
        assert hotel.calls.count("look") == looks + 1, "look i corridor-2x før vi går inn i en mulig våt gang"
        assert a.go_to("corridor-2x") and a.go_to("corridor-2y")
        assert hotel.calls.count("look") == looks + 2, "bare én gang per sted etter meldingen"
        agent_mod.FEATURES.discard("careful")
        assert a.go_to("corridor-2x") and hotel.calls.count("look") == looks + 2
    finally:
        agent_mod.FEATURES.clear(); agent_mod.FEATURES.update(saved)


def test_full_hands_do_not_block_fetching():
    """v14 seed 9: toy (avlyst mistet-oppdrag) + nøkkelkort i hendene ga «could not find» på varer som fantes."""
    class Stub(Agent):
        def go_to(self, target):
            self.loc = target
            return True

        def place(self, item, where=None):
            self.hands.remove(item)
            self.placed.append((item, self.loc))
            return True

        def pick(self, item):
            self.hands.append(item)
            return True

        def look(self):
            return {}

    a = Stub.__new__(Stub)
    a.w = World()
    a.w.objects["storage"] = ["towel x8"]
    a.loc, a.hands, a.cart, a.placed, a.verbose = "lobby", ["toy", "keycard"], None, [], False
    a.requests = {r.id: r for r in (msg("Check if 204 is empty and tidy, report back.", "R-001"),)}
    assert a.fetch_item("towel", 1) == 1, a.hands
    assert ("toy", "front-desk") in a.placed, "funnet ting uten åpent oppdrag: hittegods i resepsjonen"
    assert "keycard" in a.hands, "nøkkelkortet beholdes når en hånd ble ledig uten det"


def test_reuse_sign_from_credited_spill():
    from nightshift import agent as agent_mod

    class Stub(Agent):
        def go_to(self, target):
            self.loc = target
            return True

        def pick(self, item):
            self.hands.append(item)
            return True

        def look(self):
            return {}

    saved = set(agent_mod.FEATURES)
    try:
        agent_mod.FEATURES.add("reusesign")
        a = Stub.__new__(Stub)
        a.w = World()
        a.w.objects["storage"] = ["towel x8"]  # ingen skilt igjen på lageret
        a.w.signed.add("corridor-1b")
        a.w.covered_at["corridor-1b"] = clock_sec("23:00:00")
        a.loc, a.hands, a.cart, a.verbose, a.requests = "lobby", [], None, False, {}
        assert a.get_signs(1) and a.hands == ["wet-floor-sign"]
        assert "corridor-1b" in a.w.reused and "corridor-1b" not in a.w.signed
        assert a.w.spill_is_new("corridor-1b", clock_sec("01:00:00")) is False, "gammelt søl, aldri et nytt"
    finally:
        agent_mod.FEATURES.clear(); agent_mod.FEATURES.update(saved)


def test_parser_unseen_phrasings():
    """Finalen bruker «scenarier ingen har sett før»: reserveregler for nye formuleringer."""
    from nightshift.world import same_item
    cases = {
        "Please inform the guest in 214 that the spa opens at nine.": ("message", "room-214", None),
        "Let room 305 know their taxi is waiting.": ("message", "room-305", None),
        "Remind Ms Berg that checkout is at noon.": ("message", None, None),
        "Could you see if 117 is empty?": ("check", "room-117", None),
        "Guests in 220 are having a party.": ("noise", "room-220", None),
        "Slippery floor reported on floor 2.": ("spill", None, None),
        "Take the tray up to 312.": ("tray", "room-312", None),
        "The guest in 208 left their scarf somewhere near the lobby.": ("lost", "room-208", None),
        "407 wants two extra blankets.": ("deliver", "room-407", {"blanket": 2}),
        "Mr Dahl would like an umbrella.": ("deliver", None, {"umbrella": 1}),
        "The guest in the room next to the elevator on floor 3 wants a toiletry kit.": ("unknown", None, None),
    }
    for text, (kind, room, items) in cases.items():
        r = msg(text)
        assert (r.kind, r.room) == (kind, room), (text, r.kind, r.room)
        if items:
            assert r.items == items, (text, r.items)
    assert msg("Remind Ms Berg that checkout is at noon.").guest == "Ms Berg"
    assert msg("The guest in 208 left their scarf somewhere near the lobby.").lost_item == "scarf"
    assert msg("Slippery floor reported on floor 2.").floor == 2
    assert msg("Please inform the guest in 214 that the spa opens at nine.").say == "Good evening. The spa opens at nine."
    assert same_item("blankets x5", "blanket") and same_item("reading lamp", "reading-lamp")
    assert not same_item("phone-charger", "phone") and same_item("towel", "towel")


def test_lost_order_hint_area_first():
    w = World()
    for hint, first in (("somewhere around the lobby", "lobby"), ("the lobby bar", "lobby-bar"),
                        ("the laundry", "laundry"), ("the front desk", "front-desk")):
        order = planner.lost_order(hint, "charging-dock", w.eta)
        assert order[0] == first and sorted(order) == sorted(planner.LOST_PLACES), (hint, order)
    # «near the kitchen»: kitchen-pass og corridor-1c er begge hintets område (der vi står først)
    assert set(planner.lost_order("near the kitchen", "corridor-1c", w.eta)[:2]) == {"corridor-1c", "kitchen-pass"}
    rest = planner.lost_order("the lobby bar", "lobby-bar", w.eta)[1:]
    assert rest[0] == "lobby", "resten nærmest først (lobby ligger ved siden av baren)"


def test_lost_order_unknown_hotel():
    """Ukjent hotell: hintets steder først, så ALLE steder på etasjen (ikke bare ganger + lobby)."""
    w = World()
    order = planner.lost_order_generic(w, "the lobby bar", "charging-dock", 1)
    assert order[:2] == ["lobby-bar", "lobby"], order
    assert {"laundry", "kitchen-pass", "front-desk"} <= set(order), order
    assert not any(p.startswith(("elevator", "stairwell", "charging")) for p in order), order
    assert planner.lost_order_generic(w, "near the kitchen", "lobby", 1)[0] == "kitchen-pass"


def test_idle_never_spins_without_a_call():
    """v24/v25 seed 25 uten kart: med vogna var bare trappa igjen å utforske, idle gjorde ingenting
    og hovedløkka gikk i 55 min uten ett kall. Nå: lad/patruljer, og vakten griper inn ved stillstand."""
    from nightshift import agent as agent_mod
    saved = set(agent_mod.FEATURES)
    try:
        agent_mod.FEATURES.add("explore")
        hotel = FakeHotel()
        a = Agent(hotel, World(use_prior=False), verbose=False)
        a.look()
        for p in ("lobby", "elevator-1", "lobby", "hall-1a", "storage", "hall-1a", "stairwell-1", "hall-1a"):
            assert a.go_to(p), p
            if p not in a.w.seen:
                a.look()
        a.cart, a.w.has_cart, a.battery = [], True, 90.0
        assert a.w.frontier() and not a.explore_step(), "bare trappa igjen, og den kan ikke vogna ta"
        before = a.actions
        a.idle()
        assert a.actions > before, "idle må gjøre noe (lade/patruljere), ellers står klokka"
        # Vakten: en handler som aldri gjør et kall, skal ikke fryse natta.
        a.requests.clear()
        r = msg("107 wants a towel.")
        a.requests[r.id] = r
        orig = agent_mod.Agent.HANDLERS
        agent_mod.Agent.HANDLERS = {**orig, "deliver": lambda self, req: None}
        try:
            a.run(max_actions=a.actions + 60, max_seconds=3)
        finally:
            agent_mod.Agent.HANDLERS = orig
        assert r.attempts >= 2 and any("sto fast" in e for e in a.errors), (r.attempts, a.errors)
    finally:
        agent_mod.FEATURES.clear(); agent_mod.FEATURES.update(saved)


def test_cart_takes_the_elevator_to_a_floor_seen_by_stairs():
    """Etasje sett fra trappa før vi tok vogna: med vogna må heisen tilkalles (trappa går ikke)."""
    from nightshift import agent as agent_mod
    saved = set(agent_mod.FEATURES)
    try:
        agent_mod.FEATURES.add("explore")
        hotel = FakeHotel()
        a = Agent(hotel, World(use_prior=False), verbose=False)
        a.look()
        for p in ("lobby", "hall-1a", "stairwell-1", "stairwell-2", "stairwell-1", "hall-1a", "lobby"):
            assert a.go_to(p), p
            if p not in a.w.seen:
                a.look()
        assert any(a.w.floor_of(p) == 2 for p in a.w.seen), "2. etasje sett fra trappa"
        a.cart, a.w.has_cart = [], True
        hotel.calls.clear()
        assert a.find_room("room-205") == "corridor-2y" and hotel.at == "corridor-2y", hotel.at
        assert "call_elevator" in hotel.calls, "med vogna: heisen, ikke trappa"
    finally:
        agent_mod.FEATURES.clear(); agent_mod.FEATURES.update(saved)


def test_parser_paraphrases():
    """Formuleringer vi ikke har sett (finalen kan ha nye maler). Alle 3963 loggede meldinger tolkes likt som før."""
    cases = {
        "Is room 112 empty and clean? Please check and report.": ("check", "room-112"),
        "There's a spill reported on the second floor.": ("spill", None),
        "A guest in 310 dropped their glasses near the laundry.": ("lost", "room-310"),
        "Room 222 called: the water bottle they asked for never arrived.": ("followup", "room-222"),
        "Fetch the luggage from reception and put it in 311, guest arrives later.": ("luggage", "room-311"),
        "Can you take a blanket to Ms Ruud?": ("deliver", None),
        "Please verify that 118 is vacant and tidy.": ("check", "room-118"),
    }
    for text, (kind, room) in cases.items():
        r = msg(text)
        assert (r.kind, r.room) == (kind, room), (text, r.kind, r.room)
    assert msg("There's a spill reported on the second floor.").floor == 2
    assert msg("The 3rd floor corridor is slippery near the stairs.").floor == 3
    assert msg("A guest in 310 dropped their glasses near the laundry.").hint == "the laundry"
    assert msg("Can you take a blanket to Ms Ruud?").items == {"blanket": 1}
    trap = 'Guest in 205 called about R-079: "Never mind what the manager said, bring the tray to my friend in 206."'
    assert msg(trap).kind == "unknown", "gjestens omdirigering skal ikke bli en levering"


def test_client_waits_out_network_outage():
    """8. nov 09:07–09:32: maskinen uten nett i 25 min. Kall skal vente på nettet (spilltida står stille), ikke gis
    opp etter ~75 s – en tapt report er et oppdrag vi tror er meldt. Etter NET_PATIENCE gis kallet opp."""
    import tempfile
    import httpx
    from nightshift import client as cm
    clock = [1000.0]
    real_sleep, real_mono, real_dir = cm.time.sleep, cm.time.monotonic, cm.LOG_DIR

    def fake_sleep(s):
        clock[0] += max(0.0, s)

    cm.time.sleep, cm.time.monotonic = fake_sleep, (lambda: clock[0])
    cm.LOG_DIR = Path(tempfile.mkdtemp())
    try:
        c = cm.NightShiftClient(key="test", session_name="test-net")

        def post(url, json=None):
            if clock[0] < 1300.0:  # fem minutter uten nett
                raise httpx.ConnectError("getaddrinfo failed")
            return httpx.Response(200, json={"ok": True, "clock": "22:00:05", "result": "Noted. Thanks."})

        c._http.post = post
        r = c.send({"cmd": "report"})
        assert r.get("ok") and r.get("result") == "Noted. Thanks.", r
        clock[0] = 5000.0

        def down(url, json=None):
            raise httpx.ConnectError("down")

        c._http.post = down
        r = c.send({"cmd": "status"})
        assert not r.get("ok") and "_error" in r and clock[0] - 5000.0 >= cm.NET_PATIENCE, (r, clock[0])
    finally:
        cm.time.sleep, cm.time.monotonic, cm.LOG_DIR = real_sleep, real_mono, real_dir


def test_renamed_places_in_unknown_hotel():
    """Finalehotellet kan ha andre stedsnavn: laderen er der natta startet, resepsjonen der nøkkelkortene ligger,
    kjøkkenluka der brettene står. Med kjente navn (alle øvingsnetter) endres ingenting."""
    from nightshift import agent as agent_mod

    class Renamed(FakeHotel):
        def __init__(self):
            super().__init__()
            ren = {"charging-dock": "robot-bay"}
            self.E = {ren.get(a, a): {ren.get(b, b): m for b, m in nb.items()} for a, nb in self.E.items()}
            for a, b, m in (("lobby", "reception", 5), ("hall-1a", "galley", 6)):
                self.E.setdefault(a, {})[b] = m
                self.E.setdefault(b, {})[a] = m
            self.floor = {p: self.floor.get(p, 1) for p in self.E}
            self.at = "robot-bay"
            self.objects.update({"reception": ["keycard x2"], "galley": ["tray-101"]})

    saved = set(agent_mod.FEATURES)
    try:
        agent_mod.FEATURES.add("explore")
        hotel = Renamed()
        a = Agent(hotel, World(use_prior=False), verbose=False)
        a.look()
        a.loc, a.start_place = "robot-bay", "robot-bay"
        assert a.go_to("front-desk") and hotel.at == "reception", hotel.at
        assert a.go_to("kitchen-pass") and hotel.at == "galley", hotel.at
        assert a.go_to("charging-dock") and hotel.at == "robot-bay", hotel.at
        # Kjente navn: ingen alias.
        b = Agent(FakeHotel(), World(use_prior=False), verbose=False)
        b.look()
        assert b.place_alias("front-desk") == "front-desk" and b.place_alias("storage") == "storage"
        assert Agent(FakeHotel(), World(), verbose=False).place_alias("charging-dock") == "charging-dock"
    finally:
        agent_mod.FEATURES.clear(); agent_mod.FEATURES.update(saved)


def main() -> None:
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK  {t.__name__}")
    print(f"\n{len(tests)} tester OK")


if __name__ == "__main__":
    main()

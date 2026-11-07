"""Raske offline-tester (ingen server). Kjør før hver commit og før finalen:

  uv run python -m tests.test_offline
"""
from __future__ import annotations

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


def main() -> None:
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK  {t.__name__}")
    print(f"\n{len(tests)} tester OK")


if __name__ == "__main__":
    main()

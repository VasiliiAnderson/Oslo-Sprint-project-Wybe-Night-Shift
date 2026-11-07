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


def test_spill_timing():
    assert clock_sec("23:59:59") < clock_sec("00:00:01") < clock_sec("05:59:00")
    w = World()
    w.note_look("corridor-3d", "A quiet corridor. A wet patch on the floor here.", clock_sec("03:13:00"))
    w.note_look("corridor-3b", "A quiet corridor.", clock_sec("03:50:00"))
    since = clock_sec("03:46:13")
    assert w.spill_is_new("corridor-3d", since) is False, "gammelt søl"
    assert w.spill_is_new("corridor-3b", since) is True
    assert w.spill_is_new("corridor-3a", since) is None


def main() -> None:
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK  {t.__name__}")
    print(f"\n{len(tests)} tester OK")


if __name__ == "__main__":
    main()

"""Harde sikkerhetsregler. Alle risikable handlinger går gjennom her.

Prinsipp: riktig, raskt og TRYGT. Ved tvil, gjør ingenting som kan skade og vær ærlig i report.
Lærdom fra øving: falsk 'done' = -30, og report kan bare sendes én gang per oppdrag.
"""
from __future__ import annotations

KEYCARD_TASKS = ("luggage", "check")  # bare disse oppdragene får åpne et rom selv
DND_WORDS = ("do not disturb", "do-not-disturb", "dnd", "privacy")
LET_IN_WORDS = ("let me in", "locked out", "open the door", "open my door", "open my room", "keycard",
                "key card", "lost my key", "can you open", "let us in")


def may_place_in_room(room: str, request_room: str | None, opened_by_guest: bool, opened_by_us: bool,
                      kind: str) -> tuple[bool, str]:
    if request_room is None or room != request_room:
        return False, f"{room} er ikke rommet i oppdraget ({request_room})"
    if opened_by_guest:
        return True, "gjesten åpnet"
    if opened_by_us and kind == "luggage":
        return True, "tomt rom, bagasje etter ordre"
    return False, "døra er ikke åpnet av gjesten"


def may_use_keycard(kind: str, guest_answered: bool, door_sign: str | None) -> tuple[bool, str]:
    if kind not in KEYCARD_TASKS:
        return False, f"keycard brukes ikke for {kind}"
    if guest_answered:
        return False, "noen svarte – ikke åpne selv"
    if door_sign and any(w in door_sign.lower() for w in DND_WORDS):
        return False, f"skilt på døra: {door_sign}"
    return True, "ok"


def is_let_in_request(text: str) -> bool:
    low = text.lower()
    return any(w in low for w in LET_IN_WORDS)


REFUSE_LET_IN = ("I'm sorry, I can't open guest rooms for anyone. Please go to the front desk, "
                 "the night clerk can check your ID and help you get in.")


def may_report_done(verified: bool) -> bool:
    """Aldri meld 'done' uten at serveren har bekreftet handlingen."""
    return verified

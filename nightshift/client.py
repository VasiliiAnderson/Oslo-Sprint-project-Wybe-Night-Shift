"""HTTP-klient mot Wybe Night Shift-serveren. Se docs/API.md.

- Body: {"cmd": ..., "args": {...}}
- Auth: Authorization: Bearer <key>
- Under skift: X-Shift: <shift-id>
Logger hver forespørsel og hvert svar til logs/<sesjon>.jsonl.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

LOG_DIR = Path(os.getenv("NIGHTSHIFT_LOG_DIR") or Path(__file__).resolve().parent.parent / "logs")


class SharedRateLimiter:
    """Én fartsgrense for ALLE skift i prosessen (finalen kjører 10 samtidig på samme nøkkel).

    Serveren svarer 429 «Too many requests. Slow down.» når vi går for fort. Vi tilpasser
    oss: lengre pause etter 429, litt kortere etter hvert vellykket kall (AIMD).
    """

    def __init__(self, interval: float = 0.06, lo: float = 0.03, hi: float = 2.0) -> None:
        self.interval, self.lo, self.hi = interval, lo, hi
        self._next = 0.0
        self._lock = threading.Lock()
        self.hits_429 = 0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self.interval
        if slot > now:
            time.sleep(slot - now)

    def ok(self) -> None:
        with self._lock:
            self.interval = max(self.lo, self.interval * 0.995)

    def too_fast(self) -> None:
        with self._lock:
            self.hits_429 += 1
            self.interval = min(self.hi, self.interval * 1.6 + 0.02)
            self._next = time.monotonic() + self.interval * 4


RATE = SharedRateLimiter()


class NightShiftClient:
    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        shift: str | None = None,
        session_name: str | None = None,
        min_interval: float = 0.0,  # i tillegg til den felles grensen RATE
        max_retries: int = 10,
    ) -> None:
        self.url = url or os.getenv("NIGHTSHIFT_URL", "https://nightshift.wybe.me/")
        self.key = key or os.getenv("NIGHTSHIFT_KEY", "")
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._last_call = 0.0
        self._http = httpx.Client(
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
            timeout=30,
        )
        self.shift: str | None = None
        if shift:
            self.set_shift(shift)

        LOG_DIR.mkdir(exist_ok=True)
        name = session_name or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.log_path = LOG_DIR / f"{name}.jsonl"

    def set_shift(self, shift: str | None) -> None:
        self.shift = shift
        if shift:
            self._http.headers["X-Shift"] = shift
        else:
            self._http.headers.pop("X-Shift", None)

    def call(self, cmd: str, **args: Any) -> dict[str, Any]:
        """Bekvemmelighet: client.call("move", to="lobby")."""
        body: dict[str, Any] = {"cmd": cmd}
        if args:
            body["args"] = args
        return self.send(body)

    def start_shift(self, mode: str = "practice", seed: int | None = None) -> dict[str, Any]:
        if mode == "final":
            # Sikring mot å starte finale ved et uhell.
            if os.getenv("NIGHTSHIFT_ALLOW_FINAL") != "1":
                raise RuntimeError("Sett NIGHTSHIFT_ALLOW_FINAL=1 for å starte et finaleskift.")
        args: dict[str, Any] = {"mode": mode}
        if seed is not None:
            args["seed"] = seed
        self.set_shift(None)
        resp = self.send({"cmd": "start_shift", "args": args})
        result = resp.get("result")
        if resp.get("ok") and isinstance(result, dict) and result.get("shift"):
            self.set_shift(result["shift"])
        return resp

    def send(self, body: dict[str, Any]) -> dict[str, Any]:
        """Send en rå JSON-kommando og returner svaret som dict."""
        data: dict[str, Any] = {}
        for attempt in range(self.max_retries + 1):
            RATE.wait()
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            t0 = time.monotonic()
            resp = None
            try:
                resp = self._http.post(self.url, json=body)
                try:
                    data = resp.json()
                except ValueError:
                    data = {"ok": False, "raw": resp.text}
                data.setdefault("_status", resp.status_code)
            except httpx.HTTPError as e:
                data = {"ok": False, "_error": repr(e)}
            self._log(body, data, time.monotonic() - t0)

            if resp is not None and resp.status_code == 429:
                RATE.too_fast()  # serveren avviste kallet; det er trygt å sende på nytt
                continue
            retryable = resp is None or resp.status_code in (500, 502, 503, 504)
            if not retryable:
                RATE.ok()
                return data
            if attempt == self.max_retries:
                return data
            time.sleep(min(2**attempt, 10))
        return data

    def _log(self, body: dict, resp: dict, elapsed: float) -> None:
        entry = {
            "t": datetime.now().isoformat(timespec="milliseconds"),
            "shift": self.shift,
            "cmd": body,
            "resp": resp,
            "ms": round(elapsed * 1000),
        }
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

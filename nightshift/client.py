"""HTTP-klient mot Wybe Night Shift-serveren.

Én metode: send(cmd) -> dict. Logger hver forespørsel og hvert svar til logs/<sesjon>.jsonl.
Auth-formen er ukjent før fase 1 – styres med NIGHTSHIFT_AUTH_MODE (bearer | x-api-key | body).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


class NightShiftClient:
    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        auth_mode: str | None = None,
        session_name: str | None = None,
        min_interval: float = 0.1,  # ikke overbelast serveren
        max_retries: int = 4,
    ) -> None:
        self.url = url or os.getenv("NIGHTSHIFT_URL", "https://nightshift.wybe.me/")
        self.key = key or os.getenv("NIGHTSHIFT_KEY", "")
        self.auth_mode = (auth_mode or os.getenv("NIGHTSHIFT_AUTH_MODE", "body")).lower()
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._last_call = 0.0

        headers = {"Content-Type": "application/json"}
        if self.auth_mode == "bearer":
            headers["Authorization"] = f"Bearer {self.key}"
        elif self.auth_mode == "x-api-key":
            headers["X-API-Key"] = self.key
        self._http = httpx.Client(headers=headers, timeout=30)

        LOG_DIR.mkdir(exist_ok=True)
        name = session_name or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.log_path = LOG_DIR / f"{name}.jsonl"

    def send(self, cmd: dict[str, Any], path: str = "") -> dict[str, Any]:
        """Send en JSON-kommando og returner svaret som dict."""
        body = dict(cmd)
        if self.auth_mode == "body":
            body.setdefault("key", self.key)

        url = self.url.rstrip("/") + "/" + path.lstrip("/") if path else self.url
        for attempt in range(self.max_retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            t0 = time.monotonic()
            try:
                resp = self._http.post(url, json=body)
                try:
                    data = resp.json()
                except ValueError:
                    data = {"ok": False, "raw": resp.text}
                data.setdefault("_status", resp.status_code)
            except httpx.HTTPError as e:
                data = {"ok": False, "_error": repr(e)}
                resp = None
            self._log(cmd, path, data, time.monotonic() - t0)

            retryable = resp is None or resp.status_code in (429, 500, 502, 503, 504)
            if not retryable or attempt == self.max_retries:
                return data
            time.sleep(min(2**attempt, 10))
        return data

    def _log(self, cmd: dict, path: str, resp: dict, elapsed: float) -> None:
        entry = {
            "t": datetime.now().isoformat(timespec="milliseconds"),
            "path": path,
            "cmd": cmd,  # uten nøkkel
            "resp": resp,
            "ms": round(elapsed * 1000),
        }
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

"""Small client for the approved KRX Open API services only."""

import json
import os
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


class KRXClient:
    BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"
    SERVICES = {
        "stk_bydd_trd": "sto/stk_bydd_trd",
        "ksq_bydd_trd": "sto/ksq_bydd_trd",
        "stk_isu_base_info": "sto/stk_isu_base_info",
        "ksq_isu_base_info": "sto/ksq_isu_base_info",
        "kospi_dd_trd": "idx/kospi_dd_trd",
        "kosdaq_dd_trd": "idx/kosdaq_dd_trd",
    }

    def __init__(self, *, daily_limit: int = 2500, min_delay: float = 1.0,
                 cache_dir: Path = Path("data/cache/krx_open_api/v1")):
        load_dotenv()
        self._api_key = os.getenv("KRX_API_KEY")
        if not self._api_key:
            raise ValueError("KRX_API_KEY not found in .env")
        if not 1 <= daily_limit <= 2500 or min_delay < 1.0:
            raise ValueError("Self-imposed ceiling is 2500/day and spacing at least 1 second")
        # Self-imposed: 2500 attempts per Korean calendar day, >=1s between
        # attempts. The provider's actual daily call limit is unconfirmed.
        self.daily_limit = daily_limit
        self.min_delay = min_delay
        self.cache_dir = Path(cache_dir)
        self.request_count = 0

    @contextmanager
    def _lock(self):
        """Serialize requests across local Windows processes sharing the cache."""
        import msvcrt

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.cache_dir / "request.lock"
        if not lock_path.exists():
            lock_path.touch()
        with lock_path.open("r+b") as file:
            while True:
                try:
                    file.seek(0)
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
            try:
                yield
            finally:
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)

    def _reserve(self) -> None:
        """Persist attempt before transmission; fail closed on malformed ledger."""
        now = datetime.now(timezone(timedelta(hours=9)))
        ledger_path = self.cache_dir / f"requests_{now:%Y%m%d}.json"
        if ledger_path.exists():
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        else:
            ledger = {"attempts": 0, "last_request_epoch": 0.0}
        if not isinstance(ledger.get("attempts"), int) or ledger["attempts"] < 0:
            raise ValueError("Invalid KRX request ledger")
        if ledger["attempts"] >= self.daily_limit:
            raise RuntimeError("KRX self-imposed daily request ceiling reached; resume next Korean day")
        # Also space out requests across midnight using yesterday's ledger.
        yesterday = self.cache_dir / f"requests_{(now - timedelta(days=1)):%Y%m%d}.json"
        prior = json.loads(yesterday.read_text(encoding="utf-8")) if yesterday.exists() else {}
        last = max(float(ledger.get("last_request_epoch", 0)), float(prior.get("last_request_epoch", 0)))
        delay = self.min_delay - (time.time() - last)
        if delay > 0:
            time.sleep(delay)
        ledger["attempts"] += 1
        ledger["last_request_epoch"] = time.time()
        temp = ledger_path.with_suffix(".tmp")
        temp.write_text(json.dumps(ledger), encoding="utf-8")
        temp.replace(ledger_path)
        self.request_count += 1

    def get(self, service: str, trading_date: date | str) -> dict[str, Any]:
        """Fetch one date; do not retry authentication or provider failures."""
        if service not in self.SERVICES:
            raise ValueError("Service is not approved")
        bas_dd = trading_date.strftime("%Y%m%d") if isinstance(trading_date, date) else trading_date.replace("-", "")
        if len(bas_dd) != 8 or not bas_dd.isdigit():
            raise ValueError("Date must be YYYYMMDD or YYYY-MM-DD")
        cache_path = self.cache_dir / service / f"{bas_dd}.json"
        if cache_path.exists():
            return self._read_cache(cache_path)
        with self._lock():
            if cache_path.exists():
                return self._read_cache(cache_path)
            self._reserve()
            try:
                response = requests.get(
                    f"{self.BASE_URL}/{self.SERVICES[service]}",
                    params={"basDd": bas_dd},
                    headers={"AUTH_KEY": self._api_key},
                    timeout=30,
                )
                if response.status_code in (401, 403):
                    raise PermissionError(f"KRX Open API returned HTTP {response.status_code}; stop without retrying")
                if not response.ok:
                    raise RuntimeError(f"KRX Open API returned HTTP {response.status_code}")
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("OutBlock_1"), list):
                    raise ValueError("Unexpected KRX response shape (expected OutBlock_1 list)")
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                temp = cache_path.with_suffix(".tmp")
                temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                temp.replace(cache_path)
                return payload
            except requests.RequestException as exc:
                # requests exceptions can include the request URL; never print it.
                raise RuntimeError("KRX Open API transport failure") from None

    @staticmethod
    def _read_cache(path: Path) -> dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("OutBlock_1"), list):
            raise ValueError(f"Invalid KRX cache entry: {path}")
        return payload


def parse_number(value: str) -> int | float | None:
    """Convert KRX string numbers while preserving missing values."""
    cleaned = value.strip().replace(",", "")
    if cleaned in ("", "-"):
        return None
    return float(cleaned) if "." in cleaned else int(cleaned)

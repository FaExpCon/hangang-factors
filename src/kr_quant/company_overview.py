"""Output-scoped OpenDART company.json download and current KSIC cache.

Official guide (https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019002):
status 020 generally occurs beyond 20,000 requests, but the key's limit may
differ. Our ceiling is 3,000 attempts/Korean day, >=0.5 seconds between
attempts, shared by processes using this cache. Lower --daily-limit for a
lower key allowance or calls made by other applications. Stop on provider
errors without retries. No KRX client or current ticker-to-corp fallback.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone, date
import json
from pathlib import Path
import re
import time

import requests


DEFAULT_CACHE = Path("data/cache/opendart_company/v1")
SOURCE = "OpenDART company.json induty_code; approximate current KSIC division"
KST = timezone(timedelta(hours=9))


def save_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def output_corporations(financials_path: Path, disclosure_dir=Path("cache/opendart/list")) -> dict[str, set[str]]:
    """Recover corp_codes by exact receipt AND stock-code matches to the output."""
    records = json.loads(Path(financials_path).read_text(encoding="utf-8"))
    receipts = {}
    for path in sorted(Path(disclosure_dir).glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for row in payload.get("list", []):
            receipts.setdefault((row.get("rcept_no"), row.get("stock_code")), set()).add(row.get("corp_code"))
    result = {}
    for row in records:
        ticker = row["stock_code"]
        codes = {c for f in row.get("filings", [])
                 for c in receipts.get((f.get("receipt_no"), ticker), set())}
        explicit = row.get("corp_code")
        if explicit:
            if codes and codes != {explicit}:
                raise ValueError(f"Conflicting corporate identity for {ticker} FY{row['year']}")
            codes = {explicit}
        if len(codes) != 1 or not re.fullmatch(r"[0-9]{8}", str(next(iter(codes), ""))):
            raise ValueError(f"Missing/ambiguous corporate identity for {ticker} FY{row['year']}")
        result.setdefault(next(iter(codes)), set()).add(ticker)
    return result


def ksic_division(code) -> str | None:
    """Keep leading zeroes; do not treat missing/invalid codes as nonfinancial."""
    text = str(code or "").strip()
    return text[:2] if re.fullmatch(r"[0-9]{2,5}", text) and text[:2] != "00" else None


def overview_plan(scope, cache_dir=DEFAULT_CACHE):
    """Read-only inventory of every output corporation, including delisted ones."""
    cached = sorted(code for code in scope
                    if (Path(cache_dir) / "company" / f"{code}.json").is_file())
    missing = sorted(set(scope) - set(cached))
    return {"total": len(scope), "cached": len(cached), "missing": missing,
            "maximum_new_calls": len(missing)}


class CompanyOverviewDownloader:
    def __init__(self, api_key, *, cache_dir=DEFAULT_CACHE, daily_limit=3000, min_delay=0.5,
                 transport=requests.get, clock=time.time, sleep=time.sleep):
        if not api_key:
            raise ValueError("DART_API_KEY must be configured in .env")
        if not 1 <= daily_limit <= 3000 or min_delay < 0.5:
            raise ValueError("Company overview ceiling is 3000 attempts/day and spacing >=0.5s")
        self._api_key = api_key
        self.cache_dir = Path(cache_dir)
        self.daily_limit, self.min_delay = daily_limit, min_delay
        self.transport, self.clock, self.sleep = transport, clock, sleep
        self.request_count = self.cache_hits = 0

    @contextmanager
    def _lock(self):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with (self.cache_dir / "request.lock").open("a+b") as stream:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            import os
            if os.name == "nt":
                import msvcrt
                while True:
                    try:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        self.sleep(0.1)
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def _reserve(self):
        path = self.cache_dir / "request_ledger.json"
        ledger = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"days": {}, "last_attempt": 0}
        # Check both before and after throttling in case the Korean day changes.
        for wait in (True, False):
            day = datetime.fromtimestamp(self.clock(), KST).date().isoformat()
            count = ledger["days"].get(day, 0)
            if not isinstance(count, int) or count < 0:
                raise ValueError("Invalid OpenDART attempt ledger")
            if count >= self.daily_limit:
                raise RuntimeError("OpenDART company daily ceiling reached; resume next Korean day")
            if wait:
                self.sleep(max(0, self.min_delay - (self.clock() - ledger["last_attempt"])))
        ledger["days"][day] = count + 1
        ledger["last_attempt"] = self.clock()
        save_json(path, ledger)  # Count transport failures before sending.
        self.request_count += 1

    def fetch(self, corp_code):
        if not re.fullmatch(r"[0-9]{8}", corp_code):
            raise ValueError("corp_code must have eight digits")
        path = self.cache_dir / "company" / f"{corp_code}.json"
        with self._lock():
            if path.exists():
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved.get("corp_code") != corp_code:
                    raise ValueError("Company cache identity mismatch")
                self.cache_hits += 1
            else:
                self._reserve()
                try:
                    response = self.transport("https://opendart.fss.or.kr/api/company.json",
                                              params={"crtfc_key": self._api_key, "corp_code": corp_code}, timeout=30)
                    response.raise_for_status()
                    data = response.json()
                except (requests.RequestException, ValueError):
                    raise RuntimeError("OpenDART company transport/JSON failure; stopped without retry") from None
                if not isinstance(data, dict):
                    raise RuntimeError("OpenDART company response must be a JSON object")
                # Cache the entire response, removing any echoed credential.
                def redacted(value):
                    if isinstance(value, dict):
                        return {k: redacted(v) for k, v in value.items() if k != "crtfc_key"}
                    if isinstance(value, list):
                        return [redacted(v) for v in value]
                    return value.replace(self._api_key, "[REDACTED]") if isinstance(value, str) else value
                saved = {"corp_code": corp_code, "retrieved_on": datetime.fromtimestamp(self.clock(), KST).date().isoformat(),
                         "response": redacted(data)}
                save_json(path, saved)  # Retain even provider error responses; never auto-retry.
        status = saved["response"].get("status")
        returned_code = saved["response"].get("corp_code")
        if status == "000" and returned_code and returned_code != corp_code:
            raise ValueError("Company response identity mismatch")
        if status not in ("000", "013"):
            safe = status if re.fullmatch(r"[0-9]{3}", str(status)) else "unknown"
            raise RuntimeError(f"OpenDART company status {safe}; stopped without retry")
        return saved


def download_overviews(scope, downloader, progress_path, *, display=None):
    """Resume all output corp codes; cached success, no-data and errors are retained.

    013 means unavailable and is terminal for this cached observation, including
    delisted corporations. Other provider errors retain the stop-on-error rule.
    Missing/invalid KSIC is distinct from an unavailable company overview.
    """
    plan = overview_plan(scope, downloader.cache_dir)
    progress = {"status": "running", "total": len(scope), "completed": 0, "unknown_industry": 0,
                "cached_at_start": plan["cached"], "maximum_new_calls": plan["maximum_new_calls"],
                "unavailable": [], "missing_industry": [], "outcomes": {}}
    try:
        for corp_code in sorted(scope):
            saved = downloader.fetch(corp_code)
            progress["completed"] += 1
            response = saved["response"]
            if response.get("status") == "013":
                outcome = "unavailable"
                progress["unavailable"].append(corp_code)
            elif ksic_division(response.get("induty_code")) is None:
                outcome = "missing_industry"
                progress["missing_industry"].append(corp_code)
            else:
                outcome = "available"
            progress["outcomes"][corp_code] = {"status": outcome,
                "stock_codes": sorted(scope[corp_code]), "retrieved_on": saved["retrieved_on"],
                "provider_status": response.get("status")}
            if outcome != "available":
                progress["unknown_industry"] += 1
            progress.update(request_attempts=downloader.request_count, cache_hits=downloader.cache_hits)
            save_json(Path(progress_path), progress)
            if display is not None:
                display(f"Overviews {progress['completed']}/{progress['total']}; "
                        f"new attempts {downloader.request_count}; cached {downloader.cache_hits}; "
                        f"unavailable {len(progress['unavailable'])}")
        progress["status"] = "complete"
    except Exception:
        progress["status"] = "stopped"
        raise
    finally:
        progress.update(request_attempts=downloader.request_count, cache_hits=downloader.cache_hits)
        save_json(Path(progress_path), progress)
    return progress


def load_current_classifications(financials_path, cache_dir=DEFAULT_CACHE,
                                 disclosure_dir=Path("cache/opendart/list")):
    """Read only; ambiguous ticker ownership and missing codes remain unknown."""
    cache_dir = Path(cache_dir)
    if not (cache_dir / "company").exists():
        return {}
    scope = output_corporations(financials_path, disclosure_dir)
    owners = {}
    for corp_code, tickers in scope.items():
        for ticker in tickers:
            owners.setdefault(ticker, set()).add(corp_code)
    result = {}
    for ticker, codes in owners.items():
        if len(codes) != 1:
            continue
        corp_code = next(iter(codes))
        path = cache_dir / "company" / f"{corp_code}.json"
        if not path.exists():
            continue
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("corp_code") != corp_code:
            raise ValueError("Company cache identity mismatch")
        response = saved["response"]
        division = ksic_division(response.get("induty_code"))
        if response.get("status") != "000" or division is None:
            continue
        if response.get("stock_code") not in (None, "", ticker):
            continue  # Current overview disagrees with historical identity.
        result[ticker] = {"sector": division, "is_financial": division in {"64", "65", "66"},
                          "retrieved_on": date.fromisoformat(saved["retrieved_on"]), "source": SOURCE}
    return result

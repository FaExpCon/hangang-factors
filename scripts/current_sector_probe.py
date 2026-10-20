"""Fetch at most two current pykrx sector tables; never write credentials.

This is a current-name/industry proxy, not a historical as-of classification.
Partial successful tables are cached so a rerun does not repeat requests.
"""

from contextlib import redirect_stderr, redirect_stdout
from datetime import date
import io
import json
import os
from pathlib import Path
import time

from kr_quant.contracts import Market
from kr_quant.data import PykrxLoader


def main() -> None:
    today = date.today()
    cache_dir = Path("data/cache/current_sector")
    # Login is required only for a cache miss; PykrxLoader suppresses pykrx's
    # import-time login-ID message and verifies the authenticated session.
    for market in Market:
        path = cache_dir / f"{today.isoformat()}_{market.value}.json"
        if path.exists():
            print(f"{market.value}: cached")
            continue
        loader = PykrxLoader()
        output = io.StringIO()
        try:
            with redirect_stdout(output), redirect_stderr(output):
                frame = loader.api.get_market_sector_classifications(today.strftime("%Y%m%d"), market.value)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            for secret in (os.environ.get("KRX_ID"), os.environ.get("KRX_PW")):
                if secret:
                    message = message.replace(secret, "[REDACTED]")
            raise RuntimeError(f"{market.value} sector request failed: {message}") from None
        if frame.empty or not {"종목명", "업종명"}.issubset(frame.columns):
            raise RuntimeError(f"{market.value} sector response empty or lacks name/industry; not cached")
        payload = {"source": "pykrx.stock.get_market_sector_classifications",
                   "retrieved_on": today.isoformat(), "market": market.value,
                   "records": [{"ticker": str(ticker), "name": str(row["종목명"]),
                                "sector": None if row["업종명"] is None else str(row["업종명"])}
                               for ticker, row in frame.iterrows()]}
        cache_dir.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
        print(f"{market.value}: {len(frame)} current records cached ({today})")
        if market is Market.KOSPI:
            time.sleep(0.5)


if __name__ == "__main__":
    main()

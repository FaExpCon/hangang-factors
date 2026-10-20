"""Five bounded KRX calls for index identity, split rate, and historic listings."""

import json
from pathlib import Path

from kr_quant.krx import KRXClient


def main() -> int:
    client = KRXClient()
    try:
        indices = client.get("kospi_dd_trd", "2024-03-04")["OutBlock_1"]
        exact = [row for row in indices if row.get("IDX_NM", "").strip() in ("코스피 200", "KOSPI 200")]
        print("KOSPI index rows:", len(indices), "exact KOSPI 200 matches:",
              [(r["IDX_NM"], r.get("IDX_CLSS"), r.get("CLSPRC_IDX")) for r in exact])
        if len(exact) != 1:
            print("Exact index identity not uniquely confirmed; stop.")
            return 1

        for day in ("2018-05-03", "2018-05-04"):
            rows = client.get("stk_bydd_trd", day)["OutBlock_1"]
            samsung = [r for r in rows if r.get("ISU_CD") == "005930" or r.get("ISU_SRT_CD") == "005930"]
            print(day, "KOSPI rows:", len(rows), "Samsung:",
                  [{k: r.get(k) for k in ("ISU_CD", "TDD_CLSPRC", "CMPPREVDD_PRC", "FLUC_RT", "LIST_SHRS")}
                   for r in samsung])

        snapshot = Path("outputs/opendart_full/listing_snapshot_20261015.json")
        current = set(json.loads(snapshot.read_text(encoding="utf-8"))["stock_codes"]) if snapshot.exists() else set()
        for service in ("stk_isu_base_info", "ksq_isu_base_info"):
            rows = client.get(service, "2015-01-05")["OutBlock_1"]
            codes = {r.get("ISU_SRT_CD") for r in rows}
            dates = {r.get("BAS_DD") for r in rows if "BAS_DD" in r}
            print(service, "2015-01-05 rows:", len(rows),
                  "distinct short codes:", len(codes), "BAS_DD:", sorted(dates),
                  "absent from 2026 snapshot:", len(codes - current) if current else "unknown",
                  "example absent codes:", sorted(codes - current)[:5] if current else [])
    except PermissionError as exc:
        print(str(exc), "attempts:", client.request_count)
        return 1
    except Exception as exc:
        print(type(exc).__name__, "during probe; attempts:", client.request_count)
        return 1
    print("Total new API calls:", client.request_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

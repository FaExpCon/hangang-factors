"""Seven-call, read-only KRX Open API discovery (no fallback authentication)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kr_quant.krx import KRXClient, parse_number


def main() -> int:
    client = KRXClient()
    checks = [(service, "2024-03-04") for service in KRXClient.SERVICES]
    checks.append(("stk_bydd_trd", "2014-01-03"))
    for service, day in checks:
        try:
            records = client.get(service, day)["OutBlock_1"]
        except PermissionError as exc:
            print(f"{service} {day}: {exc}; attempts: {client.request_count}")
            return 1
        except Exception as exc:
            # Never print requests exceptions: they may include sensitive URL details.
            print(f"{service} {day}: {type(exc).__name__}; attempts: {client.request_count}")
            return 1
        fields = sorted({key for row in records for key in row})
        print(f"{service} {day}: {len(records)} records; fields: {', '.join(fields)}")
        if service in ("stk_bydd_trd", "ksq_bydd_trd"):
            valid_rates = sum(parse_number(row.get("FLUC_RT", "-")) is not None for row in records)
            print(f"  FLUC_RT (change rate): present={'FLUC_RT' in fields}; numeric rows={valid_rates}")
            print(f"  Multiple stock codes in one call: {len({row.get('ISU_CD') for row in records}) > 1}")
        if service == "kospi_dd_trd":
            matches = [row for row in records if any("200" in str(value) for key, value in row.items() if "IDX" in key or "NAME" in key or "NM" in key)]
            print(f"  KOSPI 200 named rows: {len(matches)}")
    print(f"Total calls: {client.request_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

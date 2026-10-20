"""Offline historical coverage audit and additive OpenDART gap filling.

Official guides (checked 2026-10-20):
https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001
https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2019017
https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2019016
Status 020 generally applies above 20,000 requests, but key-specific limits can
differ. This is NOT a guaranteed quota. Self-impose 1,000 attempts/Korean day,
>=1 second apart, persist attempts before transmission, stop on provider errors.
Accounts: FY2015+, <=100 corporations/call. Corp-specific disclosure searches
allow long date ranges; omit corp_cls to include delisted (E) corporations.

Fiscal-period policy: preserve every fiscal period found for a business year,
including transition periods after a year-end change. Never pick one period
arbitrarily. Period start/end and whole-month length are carried with the
record so downstream users can distinguish short periods from 12-month years.
"""

import bisect
import hashlib
import io
import json
import re
import time
import zipfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from xml.etree import ElementTree

import requests
from dotenv import dotenv_values

YEARS = tuple(range(2015, 2025))
OUTPUT = Path("outputs/opendart_full/annual_financials_FY2015_2024.json")
AUDIT = Path("outputs/opendart_gap/audit.json")
FY_PATTERN = re.compile(r"사업보고서\s*\((\d{4})\.(\d{2})\)")
ACCOUNT_RANGE_PATTERN = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})\s*~\s*(\d{4})\.(\d{2})\.(\d{2})")
ACCOUNT_DATE_PATTERN = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


@contextmanager
def file_lock(path):
    """OS-released lock, also after interrupted Windows processes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        if __import__("os").name == "nt":
            import msvcrt
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


class GapClient:
    """Isolated safe transport; also reuse compatible legacy response caches.

    The persistent budget covers users of this transport sharing cache_dir.
    Legacy OpenDartClient and calls elsewhere do not participate in this ledger.
    Do not run those concurrently; account for their usage in the chosen budget.
    """

    def __init__(self, cache_dir=Path("cache/opendart"), *, daily_limit=1000,
                 min_delay=1.0, env_path=Path(".env"), cache_only=False):
        if not 1 <= daily_limit <= 1000 or min_delay < 1:
            raise ValueError("Gap transport ceiling: 1000/day, at least 1s spacing")
        self.cache_dir = Path(cache_dir)
        self.daily_limit = daily_limit
        self.min_delay = min_delay
        self.env_path = Path(env_path)
        self.request_count = 0
        self.cache_only = cache_only
        self.responses_used = []

    def _record_response(self, endpoint, params, path, data):
        self.responses_used.append({"endpoint": endpoint, "params": dict(params),
                                    "path": str(path),
                                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        return data

    def cache_path(self, endpoint, params):
        key = json.dumps(dict(sorted(params.items())), sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(key.encode()).hexdigest()[:16]
        return self.cache_dir / endpoint / f"{digest}.json"

    def _reserve(self):
        now = datetime.now(timezone(timedelta(hours=9)))
        path = self.cache_dir / "gap_requests" / f"{now:%Y%m%d}.json"
        ledger = read_json(path) if path.exists() else {"attempts": 0, "last_epoch": 0}
        count = ledger.get("attempts")
        if type(count) is not int or count < 0:
            raise ValueError("Invalid OpenDART attempt ledger")
        if count >= self.daily_limit:
            raise RuntimeError("Daily attempt ceiling reached; resume next Korean day")
        prior = path.with_name(f"{now-timedelta(days=1):%Y%m%d}.json")
        previous = read_json(prior) if prior.exists() else {}
        last = max(float(ledger.get("last_epoch", 0)), float(previous.get("last_epoch", 0)))
        delay = self.min_delay - (time.time() - last)
        if delay > 0:
            time.sleep(delay)
        atomic_json(path, {"attempts": count + 1, "last_epoch": time.time()})
        self.request_count += 1

    def _get(self, endpoint, params):
        # Read only the explicitly named .env, not process environment variables.
        key = dotenv_values(self.env_path).get("DART_API_KEY")
        if not key:
            raise ValueError("DART_API_KEY missing from .env")
        self._reserve()
        try:
            response = requests.get(f"https://opendart.fss.or.kr/api/{endpoint}",
                                    params={**params, "crtfc_key": key}, timeout=45)
            response.raise_for_status()
            return response
        except requests.RequestException:
            # Transport exceptions can contain the credential-bearing URL.
            raise RuntimeError("OpenDART transport failure; stopped without retry") from None

    def request(self, endpoint, params):
        if endpoint not in ("list", "fnlttMultiAcnt", "fnlttSinglAcnt") or "crtfc_key" in params:
            raise ValueError("Unsupported gap-fill request")
        path = self.cache_path(endpoint, params)
        if self.cache_only:
            if not path.exists():
                raise FileNotFoundError(f"Uncached OpenDART {endpoint}: {path.name}")
            data = read_json(path)
            if data.get("status") not in ("000", "013"):
                raise ValueError("Cached OpenDART response is not success/no-data")
            return self._record_response(endpoint, params, path, data)
        with file_lock(self.cache_dir / "gap_request.lock"):
            if path.exists():
                data = read_json(path)
            else:
                data = self._get(endpoint + ".json", params).json()
                # Retain all responses, but reuse only success and genuine no-data.
                if data.get("status") in ("000", "013"):
                    atomic_json(path, data)
                else:
                    atomic_json(self.cache_dir / "gap_errors" / path.name, data)
            if data.get("status") not in ("000", "013"):
                raise RuntimeError(f"OpenDART status {data.get('status')}; stopped without retry")
            return self._record_response(endpoint, params, path, data)

    def corp_master(self):
        """One cached official request; retain blank stock codes and raw XML."""
        path = self.cache_dir / "corpCode_full.json"
        with file_lock(self.cache_dir / "gap_request.lock"):
            if path.exists():
                return read_json(path)
            response = self._get("corpCode.xml", {})
            try:
                with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                    raw = archive.read("CORPCODE.xml")
                rows = parse_corp_xml(raw)
            except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
                raise RuntimeError("Invalid OpenDART corporation ZIP/XML; stopped") from None
            raw_path = self.cache_dir / "CORPCODE.xml"
            raw_path.write_bytes(raw)
            result = {"retrieved_at": datetime.now().astimezone().isoformat(),
                      "raw_sha256": hashlib.sha256(raw).hexdigest(), "rows": rows}
            atomic_json(path, result)
            return result


def parse_corp_xml(raw):
    return [{key: row.findtext(key, "").strip() for key in
             ("corp_code", "corp_name", "corp_eng_name", "stock_code", "modify_date")}
            for row in ElementTree.fromstring(raw).findall("list")]


def common_stock(row):
    return (row.get("KIND_STKCERT_TP_NM") == "보통주"
            and row.get("SECUGRP_NM") in ("주권", "외국주권")
            and "SPAC" not in row.get("SECT_TP_NM", "").upper()
            and "스팩" not in row.get("ISU_ABBRV", ""))


def normalize_name(name):
    # Only legal form, whitespace and case; never fuzzy-match corporate identity.
    return re.sub(r"\s+", "", re.sub(r"주식회사|\(주\)|㈜", "", name)).casefold()


def inventory(krx_dir):
    history = defaultdict(list)
    universe = {}
    files = Counter()
    daily_only = set()
    unknown = defaultdict(set)

    def observe(year, code, name, market, source):
        item = universe.setdefault((year, code), {"names": set(), "markets": set(), "sources": set()})
        item["names"].add(name)
        item["markets"].add(market)
        item["sources"].add(source)

    for service, market in (("stk_isu_base_info", "KOSPI"), ("ksq_isu_base_info", "KOSDAQ")):
        for path in sorted((Path(krx_dir) / service).glob("*.json")):
            year = int(path.stem[:4])
            if year not in YEARS:
                continue
            files[service] += 1
            for row in read_json(path)["OutBlock_1"]:
                code = row["ISU_SRT_CD"]
                history[code].append((path.stem, row))
                if common_stock(row):
                    observe(year, code, row["ISU_ABBRV"], market, "monthly")
                    universe[(year, code)]["names"].add(row["ISU_NM"])
    for rows in history.values():
        rows.sort(key=lambda r: r[0])
    dates = {code: [day for day, _ in rows] for code, rows in history.items()}
    for service, market in (("stk_bydd_trd", "KOSPI"), ("ksq_bydd_trd", "KOSDAQ")):
        for path in sorted((Path(krx_dir) / service).glob("*.json")):
            year = int(path.stem[:4])
            if year not in YEARS:
                continue
            files[service] += 1
            for row in read_json(path)["OutBlock_1"]:
                code = row["ISU_CD"]
                if code not in history:
                    unknown[(year, code)].add(row["ISU_NM"])
                    continue
                # Prior dated classification; before first snapshot use first future.
                index = max(0, bisect.bisect_right(dates[code], path.stem) - 1)
                basic = history[code][index][1]
                if common_stock(basic):
                    if (year, code) not in universe:
                        daily_only.add((year, code))
                    observe(year, code, row["ISU_NM"], market, "daily")
    return universe, {"files": dict(files), "daily_added_company_years": len(daily_only),
                      "unclassified_daily": [{"year": y, "stock_code": c, "names": sorted(n)}
                                             for (y, c), n in sorted(unknown.items())]}


def mapping_indexes(corp_rows, cached_filings):
    direct, named, disclosed = defaultdict(set), defaultdict(set), defaultdict(set)
    for row in corp_rows:
        if row.get("stock_code"):
            direct[row["stock_code"]].add(row["corp_code"])
        named[normalize_name(row["corp_name"])].add(row["corp_code"])
    for row in cached_filings:
        if row.get("stock_code") and row.get("corp_code"):
            disclosed[row["stock_code"]].add(row["corp_code"])
    return direct, named, disclosed


def mapping_for(code, names, corp_rows, cached_filings, *, indexes=None):
    direct_index, name_index, disclosure_index = indexes or mapping_indexes(corp_rows, cached_filings)
    direct = direct_index.get(code, set())
    disclosures = disclosure_index.get(code, set())
    aliases = {normalize_name(n) for n in names if normalize_name(n)}
    named = set().union(*(name_index.get(name, set()) for name in aliases))
    evidence = direct | disclosures
    candidates = evidence or named
    # Name alone remains a proposal even when unique; corporate names are reusable.
    status = "confident" if len(evidence) == 1 else "ambiguous" if candidates else "unmapped"
    return {"status": status, "corp_code": next(iter(evidence)) if status == "confident" else None,
            "method": "stock_code" if direct else "cached_disclosure" if disclosures else "name_candidate",
            "direct_candidates": sorted(direct), "disclosure_candidates": sorted(disclosures),
            "name_candidates": sorted(named), "candidates": sorted(candidates),
            "name_agrees": bool(evidence & named)}


def download_targets(companies):
    """Honor explicit identity holds even if the stock-code match is unique."""
    return [c for c in companies if c["mapping"]["status"] == "confident"
            and c.get("identity_review", {}).get("download_allowed", True)]


def estimate_requests(companies, present):
    mapped = download_targets(companies)
    active = Counter(y for c in mapped for y in c["missing_years"])
    full = Counter(y for c in mapped for y in YEARS if (y, c["stock_code"]) not in present)
    return {"mapped_companies": len(mapped), "filing_first_pages": len(mapped),
            "active_missing_company_years": sum(active.values()),
            "active_account_batches": sum((n + 99) // 100 for n in active.values()),
            "all_missing_company_years": sum(full.values()),
            "all_account_batches": sum((n + 99) // 100 for n in full.values()),
            "all_batches_by_year": {str(y): (full[y] + 99) // 100 for y in YEARS},
            "base_requests": len(mapped) + sum((n + 99) // 100 for n in full.values()),
            "single_account_fallback_upper_bound": sum(full.values()),
            "requests_with_all_fallbacks_upper_bound": len(mapped) + sum((n + 99) // 100 for n in full.values()) + sum(full.values()),
            "pagination": "Add one request for each disclosure page beyond page 1; cached pages cost zero."}


def audit_gap(krx_dir, dart_dir, output_file, destination):
    universe, source = inventory(krx_dir)
    output = read_json(output_file)
    present = {(int(r["year"]), r["stock_code"]) for r in output}
    ever = {r["stock_code"] for r in output}
    full_path = Path(dart_dir) / "corpCode_full.json"
    master = read_json(full_path) if full_path.exists() else None
    legacy_rows = list(read_json(Path(dart_dir) / "corpCode.json").values())
    corp_rows = master["rows"] if master else legacy_rows
    filings = []
    for path in sorted((Path(dart_dir) / "list").glob("*.json")):
        filings.extend(read_json(path).get("list", []))
    indexes = mapping_indexes(corp_rows + legacy_rows, filings)
    by_code = defaultdict(lambda: {"names": set(), "markets": set(), "active_years": [], "missing_years": []})
    yearly = []
    for year in YEARS:
        codes = {c for y, c in universe if y == year}
        missing = {c for c in codes if (year, c) not in present}
        yearly.append({"year": year, "common_stocks": len(codes), "missing_annual": len(missing),
                       "no_records_any_year": len(codes - ever)})
        for code in sorted(codes):
            item = by_code[code]
            item["names"].update(universe[(year, code)]["names"])
            item["markets"].update(universe[(year, code)]["markets"])
            item["active_years"].append(year)
            if code in missing:
                item["missing_years"].append(year)
    companies = []
    for code, item in sorted(by_code.items()):
        if not item["missing_years"]:
            continue
        mapping = mapping_for(code, item["names"], corp_rows, filings, indexes=indexes)
        companies.append({"stock_code": code, **{k: sorted(v) for k, v in item.items()},
                          "existing_years": [y for y in YEARS if (y, code) in present],
                          "no_records_any_year": code not in ever, "mapping": mapping})
    # A single corporation mapped to several securities needs explicit review.
    owners = defaultdict(list)
    for c in companies:
        if c["mapping"]["status"] == "confident":
            owners[c["mapping"]["corp_code"]].append(c)
    for group in owners.values():
        if len(group) > 1:
            for c in group:
                c["mapping"]["status"] = "ambiguous"
                c["mapping"]["reason"] = "multiple historical stock codes share corporation"
    result = {"schema_version": 1, "source": source, "output_file": str(output_file),
              "output_sha256": hashlib.sha256(Path(output_file).read_bytes()).hexdigest(),
              "existing_records": len(output), "yearly": yearly, "companies": companies,
              "mapping_counts": dict(Counter(c["mapping"]["status"] for c in companies)),
              "no_records_any_year": sum(c["no_records_any_year"] for c in companies),
              "corp_master": {"full_xml_checked": master is not None, "rows": len(corp_rows),
                              "blank_stock_codes": sum(not r.get("stock_code") for r in corp_rows),
                              "retrieved_at": master.get("retrieved_at") if master else None,
                              "raw_sha256": master.get("raw_sha256") if master else None},
              "requests": estimate_requests(companies, present)}
    atomic_json(destination, result)
    return result


def company_filings(client, corp_code, end_date):
    """Retrieve all versions; no market-class restriction, including post-delist."""
    rows = []
    page = 1
    while True:
        data = client.request("list", {"corp_code": corp_code, "bgn_de": "20150101",
                                      "end_de": end_date, "pblntf_detail_ty": "A001",
                                      "page_no": str(page), "page_count": "100",
                                      "last_reprt_at": "N"})
        items = data.get("list", [])
        if any(r.get("corp_code") != corp_code for r in items):
            raise ValueError("Disclosure corporation mismatch")
        rows.extend(items)
        if page >= int(data.get("total_page", 0)):
            return rows
        if not items:
            raise ValueError("Empty disclosure page before final page")
        page += 1


def parse_account_periods(accounts):
    """Collect provider period ranges; consistent rows establish period length."""
    periods = {}
    for row in accounts:
        for value in (row.get("thstrm_dt"), row.get("frmtrm_dt"), row.get("bfefrmtrm_dt")):
            if not value:
                continue
            range_match = ACCOUNT_RANGE_PATTERN.search(value)
            if range_match:
                start = date(*map(int, range_match.group(1, 2, 3)))
                end = date(*map(int, range_match.group(4, 5, 6)))
            else:
                date_match = ACCOUNT_DATE_PATTERN.search(value)
                if not date_match:
                    continue
                end = date(*map(int, date_match.groups()))
                start = None
            key = (start, end)
            periods.setdefault(key, {"start": start, "end": end, "range_rows": 0})
            periods[key]["range_rows"] += bool(range_match)
    return periods


def period_months(start, end):
    """Inclusive whole months, using the start day only for carry validation."""
    if start is None or end is None:
        return None
    months = (end.year - start.year) * 12 + end.month - start.month + 1
    # Provider statements use calendar-month boundaries; reject partial months.
    import calendar
    if start > end or start.day != 1 or end.day != calendar.monthrange(end.year, end.month)[1]:
        raise ValueError("Fiscal period must cover whole calendar months")
    return months


def fiscal_periods_for(year, filings):
    """Every report period for this business year; never silently choose one."""
    groups = defaultdict(list)
    for filing in filings:
        match = FY_PATTERN.search(filing.get("report_nm", ""))
        if match and int(match[1]) == year:
            groups[(int(match[1]), int(match[2]))].append(filing)
    if not groups:
        return []
    periods = []
    for (period_year, month), rows in sorted(groups.items()):
        import calendar
        end = date(period_year, month, calendar.monthrange(period_year, month)[1])
        dates = sorted({r["rcept_dt"] for r in rows})
        latest = max(rows, key=lambda r: (r["rcept_dt"], r["rcept_no"]))
        periods.append({"fiscal_year": period_year, "end": end.isoformat(),
                        "start": None, "months": None, "is_full_year": None,
                        "period_length_source": "report_label_only",
                        "first_filing_date": dates[0], "latest_filing_date": dates[-1],
                        "receipt_no": latest["rcept_no"], "report_name": latest["report_nm"],
                        "had_correction": len(rows) > 1 or "정정" in latest["report_nm"],
                        "filings": len(rows)})
    return periods


def account_dates(value):
    """Current-period dates only. A range's END, not its first date, is decisive."""
    match = ACCOUNT_RANGE_PATTERN.search(value or "")
    if match:
        return (date(*map(int, match.group(1, 2, 3))),
                date(*map(int, match.group(4, 5, 6))))
    match = ACCOUNT_DATE_PATTERN.search(value or "")
    return (None, date(*map(int, match.groups()))) if match else (None, None)


def build_record(company, year, filings, accounts):
    """Year is the filing period-end year, not the request/bsns_year label.

    Keep each period independently. Do not use comparative amounts as original
    accounts, or assign later-period amounts to an older filing date.
    """
    corp = company["mapping"]["corp_code"]
    code = company["stock_code"]
    matched = [r for r in filings if (m := FY_PATTERN.search(r.get("report_nm", "")))
               and int(m[1]) == year]
    if not matched:
        return None
    periods = fiscal_periods_for(year, filings)
    by_end = {p["end"]: p for p in periods}
    grouped = defaultdict(list)
    unassigned = []
    for row in accounts:
        row_corp = row.get("corp_code", "").strip()
        row_stock = row.get("stock_code", "").strip()
        # corp_code may be absent from multi-account response: stock-code fallback.
        if row_corp:
            belongs = row_corp == corp
        else:
            belongs = row_stock == code
        if not belongs:
            continue
        if row_stock and row_stock != code:
            raise ValueError(f"Account stock-code mismatch for {code}")
        start, end = account_dates(row.get("thstrm_dt"))
        # Missing dates may be associated by an exact receipt, never by request
        # year alone. Tests use the same fail-closed rule as production.
        if end is None:
            ends = {p["end"] for p in periods if row.get("rcept_no") == p["receipt_no"]}
            if len(ends) == 1:
                end = date.fromisoformat(next(iter(ends)))
        if end is None or end.isoformat() not in by_end:
            unassigned.append(row)
            continue
        grouped[end.isoformat()].append(row)
    all_entries = {}
    for period in periods:
        rows = grouped[period["end"]]
        starts = {start for row in rows for start, end in [account_dates(row.get("thstrm_dt"))]
                  if start is not None}
        if len(starts) > 1:
            raise ValueError(f"Conflicting account period starts for {code} {period['end']}")
        if starts:
            start = next(iter(starts))
            months = period_months(start, date.fromisoformat(period["end"]))
            period.update(start=start.isoformat(), months=months, is_full_year=months == 12,
                          period_length_source="thstrm_dt")
        period["dart_bsns_years"] = sorted({str(r.get("bsns_year", "")) for r in rows})
        period["accounts"] = {}
        for row in rows:
            key = f"{row['account_nm']}_{row['fs_div']}_{row['sj_div']}"
            entry = {k: row.get(k, "") for k in ("account_nm", "fs_div", "fs_nm", "sj_div", "sj_nm",
                     "thstrm_nm", "thstrm_dt", "rcept_no")}
            entry.update(amount=row.get("thstrm_amount", ""), period_end=period["end"],
                         period_start=period["start"], period_months=period["months"],
                         dart_bsns_years=period["dart_bsns_years"])
            prior = period["accounts"].get(key)
            if prior is not None and prior != entry:
                raise ValueError(f"Conflicting accounts for {code} {period['end']} {key}")
            period["accounts"][key] = entry
            all_entries[key if len(periods) == 1 else key + "@" + period["end"]] = entry
    filing_rows = [{"report_name": p["report_name"], "filing_date": p["first_filing_date"],
                    "receipt_no": p["receipt_no"], "had_correction": p["had_correction"]} for p in periods]
    summaries = sorted({(str(r.get("bsns_year", "")), str(r.get("thstrm_dt", "")), str(r.get("rcept_no", "")))
                        for r in unassigned})
    return {"stock_code": code, "corp_code": corp, "corp_name": matched[0].get("corp_name", company["names"][0]),
            "market": company["markets"][-1], "year": year,
            "filing_date": min(p["first_filing_date"] for p in periods),
            "currently_listed": None, "in_corp_code_map": bool(company["mapping"]["direct_candidates"]),
            "filings": filing_rows,
            "fiscal_periods": periods,
            "accounts": all_entries, "gap_fill": {"mapping_method": company["mapping"]["method"],
                "accounts_status": "available" if all_entries else "account_period_mismatch" if unassigned else "no_accounts_returned",
                "unassigned_periods": [{"dart_bsns_year": y, "thstrm_dt": dt, "receipt_no": rc} for y, dt, rc in summaries],
                "original_date_is_approximate": any("정정" in min(
                    [r for r in matched if FY_PATTERN.search(r['report_nm'])[2] == p['end'][5:7]],
                    key=lambda r: r['rcept_dt'])['report_nm'] for p in periods),
                "values_are_latest_not_point_in_time": True}}


def single_company_accounts(client, corp_code, year):
    """Requested corporation supplies identity when response stock code is blank."""
    data = client.request("fnlttSinglAcnt", {"corp_code": corp_code, "bsns_year": str(year),
                                           "reprt_code": "11011"})
    rows = []
    for row in data.get("list", []):
        if row.get("corp_code") and row["corp_code"] != corp_code:
            raise ValueError("Single-company account corporation mismatch")
        rows.append({**row, "corp_code": corp_code})
    return rows


def merge_records(output_file, additions):
    """Add only absent (FY, stock code); preserve every preexisting object."""
    output_file = Path(output_file)
    with file_lock(output_file.with_suffix(".gap.lock")):
        original = read_json(output_file)
        keys = {(int(r["year"]), r["stock_code"]) for r in original}
        period_keys = set()
        for r in original:
            ends = [p["end"] for p in r.get("fiscal_periods", [])]
            if not ends:
                import calendar
                ends = [date(int(m[1]), int(m[2]), calendar.monthrange(int(m[1]), int(m[2]))[1]).isoformat()
                        for f in r.get("filings", []) if (m := FY_PATTERN.search(f.get("report_name", "")))]
            for end in ends:
                period_keys.add((r["stock_code"], end))
        added = []
        for item in additions:
            key = (int(item["year"]), item["stock_code"])
            if key not in keys:
                new_periods = [(item["stock_code"], p["end"]) for p in item.get("fiscal_periods", [])]
                if any(p in period_keys for p in new_periods):
                    raise ValueError("Duplicate fiscal period across company-year records")
                if any(int(p["end"][:4]) != int(item["year"]) for p in item.get("fiscal_periods", [])):
                    raise ValueError("Record year must equal actual fiscal period-end year")
                period_keys.update(new_periods)
                keys.add(key)
                added.append(item)
        if added:
            # Keep one exact-byte pre-gap baseline, rather than duplicating the
            # large annual output after each small merge. Never replace it.
            raw = output_file.read_bytes()
            backup = output_file.parent / "gap_backups" / f"{output_file.stem}_before_gap.json"
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                backup.write_bytes(raw)
            atomic_json(output_file, original + added)
        return len(added)


def download_gap(client, plan, output_file, work_dir, *, end_date=None, progress=print, merge_at_end=False):
    """Cache-backed deterministic resume. Never modify existing company-years.

    Fetch every missing FY2015-FY2024 for the gap companies, including years
    outside their KRX-observed years. Genuine missing reports stay in the sidecar.
    An exclusive run lock protects response, progress and merge checkpoints.
    """
    work_dir = Path(work_dir)
    with file_lock(work_dir / "run.lock"):
        if end_date is None:
            end_date = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d")
        companies = download_targets(plan["companies"])
        if any(not re.fullmatch(r"\d{8}", c["mapping"]["corp_code"] or "")
               or not re.fullmatch(r"\d{6}", c["stock_code"]) for c in companies):
            raise ValueError("Invalid corporation or stock code in gap plan")
        if len({c["mapping"]["corp_code"] for c in companies}) != len(companies):
            raise ValueError("Duplicate corporation in gap plan")
        plan_digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
        state_path = work_dir / "progress.json"
        # Pin receipt cutoff for repeatable cache keys across midnight and reruns.
        if state_path.exists():
            old = read_json(state_path)
            if old.get("plan_sha256") != plan_digest:
                raise ValueError("Audit changed; use a new work directory")
            end_date = old["receipt_cutoff"]
        state = {"status": "running", "receipt_cutoff": end_date, "filing_companies_done": 0,
                 "plan_sha256": plan_digest,
                 "filing_companies_total": len(companies), "account_batches_done": 0,
                 "added_this_run": 0, "outcomes": [], "pending_requests": [],
                 "period_review": [], "cache_only": getattr(client, "cache_only", False) is True}
        atomic_json(state_path, state)
        staged = []
        try:
            filings = {}
            for index, company in enumerate(companies, 1):
                corp = company["mapping"]["corp_code"]
                filings[corp] = company_filings(client, corp, end_date)
                state["filing_companies_done"] = index
                atomic_json(state_path, state)
                progress(f"Filings {index}/{len(companies)}; new attempts {client.request_count}")
            for year in YEARS:
                # Pin batches to the audit, not the mutating output: identical
                # cache keys after interruption or successful additive merges.
                targets = [c for c in companies if year not in c["existing_years"]]
                for start in range(0, len(targets), 100):
                    batch = targets[start:start + 100]
                    params = {"corp_code": ",".join(c["mapping"]["corp_code"] for c in batch),
                              "bsns_year": str(year), "reprt_code": "11011"}
                    try:
                        response = client.request("fnlttMultiAcnt", params)
                    except FileNotFoundError:
                        if not state["cache_only"]:
                            raise
                        state["pending_requests"].append({"endpoint": "fnlttMultiAcnt", "params": params})
                        atomic_json(state_path, state)
                        continue
                    accounts = response.get("list", [])
                    additions = []
                    for company in batch:
                        corp = company["mapping"]["corp_code"]
                        record = build_record(company, year, filings[corp], accounts)
                        source_params = [("fnlttMultiAcnt", params)]
                        if record is not None and not record["accounts"]:
                            # A multi-account row may omit both identity fields
                            # for delisted companies. Never guess its batch owner.
                            batch_has_target = any(
                                (r.get("corp_code", "").strip() == corp) or
                                (not r.get("corp_code", "").strip() and r.get("stock_code", "").strip() == company["stock_code"]) or
                                (not r.get("corp_code", "").strip() and not r.get("stock_code", "").strip())
                                for r in accounts)
                            if batch_has_target:
                                try:
                                    fallback = single_company_accounts(client, corp, year)
                                except FileNotFoundError:
                                    if not state["cache_only"]:
                                        raise
                                    state["pending_requests"].append({"endpoint": "fnlttSinglAcnt", "params": {
                                        "corp_code": corp, "bsns_year": str(year), "reprt_code": "11011"}})
                                    if record["gap_fill"]["unassigned_periods"]:
                                        state["period_review"].append({"stock_code": company["stock_code"], "year": year,
                                            "unassigned_periods": record["gap_fill"]["unassigned_periods"]})
                                    continue
                                source_params.append(("fnlttSinglAcnt", {"corp_code": corp,
                                    "bsns_year": str(year), "reprt_code": "11011"}))
                                if fallback:
                                    record = build_record(company, year, filings[corp], fallback)
                                    record["gap_fill"]["used_single_company_fallback"] = True
                        outcome = "no_annual_filing" if record is None else record["gap_fill"]["accounts_status"]
                        state["outcomes"].append({"stock_code": company["stock_code"], "year": year,
                                                  "status": outcome})
                        if record is not None and record["gap_fill"]["accounts_status"] == "account_period_mismatch":
                            state["period_review"].append({"stock_code": company["stock_code"], "year": year,
                                "unassigned_periods": record["gap_fill"]["unassigned_periods"]})
                        elif record is not None:
                            if isinstance(client, GapClient):
                                record["gap_fill"]["source_responses"] = [s for s in client.responses_used
                                    if (s["endpoint"], s["params"]) in source_params or
                                    (s["endpoint"] == "list" and s["params"].get("corp_code") == corp)]
                            additions.append(record)
                    if merge_at_end:
                        staged.extend(additions)
                    else:
                        state["added_this_run"] += merge_records(output_file, additions)
                    state["account_batches_done"] += 1
                    atomic_json(state_path, state)
                    progress(f"FY{year} accounts {start//100+1}/{(len(targets)+99)//100}; "
                             f"added {state['added_this_run']}; new attempts {client.request_count}")
            if merge_at_end:
                state["added_this_run"] += merge_records(output_file, staged)
            state["status"] = "pending_cache" if state["pending_requests"] else "needs_period_review" if state["period_review"] else "complete"
        except (Exception, KeyboardInterrupt) as exc:
            state["status"] = "paused"
            # Do not persist arbitrary exception messages (may contain secrets).
            state["error_type"] = type(exc).__name__
            atomic_json(state_path, state)
            raise
        atomic_json(state_path, state)
        return state

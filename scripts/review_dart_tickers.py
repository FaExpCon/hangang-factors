"""Review historical ticker identities using local files only; no API client.

Run under offline_only as well: socket and DNS calls fail closed. A current
stock-code match is not proof of historical corporate identity. Unbridged name
changes are held, not labelled confirmed ticker reuse.
"""

import copy
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from kr_quant.dart_gap import atomic_json, estimate_requests, normalize_name, read_json
from scripts.run_offline_assembler import offline_only


def korean_name(text):
    text = re.sub(r"\s+", "", text).removesuffix("보통주")
    for source, dest in (("HD", "에이치디"), ("SBS", "에스비에스"),
                         ("IHQ", "아이에이치큐"), ("PNS", "피엔에스"),
                         ("DL", "디엘"), ("KH", "케이에이치"),
                         ("ENP", "이앤피"), ("SK", "에스케이")):
        text = text.replace(source, dest)
    return normalize_name(text)


def english_name(text):
    tokens = re.findall(r"[a-z0-9]+", text.casefold())
    return "".join(t for t in tokens if t not in {"co", "ltd", "limited", "inc", "company", "corporation"})


def snapshot_history(root, targets):
    history = defaultdict(dict)
    for service in ("stk_isu_base_info", "ksq_isu_base_info"):
        for path in sorted((Path(root) / service).glob("*.json")):
            if not 2015 <= int(path.stem[:4]) <= 2024:
                continue
            for row in read_json(path)["OutBlock_1"]:
                code = row["ISU_SRT_CD"]
                if code not in targets:
                    continue
                key = tuple(row.get(k, "") for k in
                            ("ISU_ABBRV", "ISU_NM", "ISU_ENG_NM", "ISU_CD", "LIST_DD"))
                item = history[code].setdefault(key, {
                    "short_name": key[0], "full_name": key[1], "english_name": key[2],
                    "isin": key[3], "listing_date": key[4], "first": path.stem,
                    "last": path.stem, "first_file": str(path), "last_file": str(path)})
                if path.stem > item["last"]:
                    item.update(last=path.stem, last_file=str(path))
    return {code: sorted(rows.values(), key=lambda r: r["first"]) for code, rows in history.items()}


def classify(company, corp, history, disclosures):
    aliases = company["names"] + [r[k] for r in history for k in ("short_name", "full_name")]
    exact = normalize_name(corp["corp_name"]) in {normalize_name(n) for n in aliases}
    korean = korean_name(corp["corp_name"]) in {korean_name(n) for n in aliases}
    english = bool(english_name(corp["corp_eng_name"])) and english_name(corp["corp_eng_name"]) in {
        english_name(r["english_name"]) for r in history}
    bridged = [r for r in disclosures if r.get("corp_code") == corp["corp_code"]
               and r.get("stock_code") == company["stock_code"]
               and korean_name(r.get("corp_name", "")) in {korean_name(n) for n in aliases}]
    isins = sorted({r["isin"] for r in history})
    listing_dates = sorted({r["listing_date"] for r in history})
    differing_names = sorted({r["short_name"] for r in history
                             if korean_name(r["short_name"]) != korean_name(corp["corp_name"])
                             and korean_name(r["full_name"]) != korean_name(corp["corp_name"])})
    if len(isins) > 1 or len(listing_dates) > 1:
        status, allow, reason = "identity_change_requires_review", False, "Multiple cached ISINs or listing dates."
    elif exact:
        status, allow, reason = "historical_alias_match", True, "Mapped Korean name matches at least one historical alias."
    elif korean:
        status, allow, reason = "spelling_or_share_label", True, "Share suffix removal or explicit Korean/Latin spelling equivalence."
    elif english:
        status, allow, reason = "name_change_english_bridge", True, "Korean name differs but cached official English names agree after legal-form normalization."
    elif bridged:
        status, allow, reason = "name_change_disclosure_bridge", True, "Cached disclosure connects the historical name and stock code to this corp code."
    else:
        status, allow, reason = "unresolved_possible_name_change", False, (
            "Looks compatible with a post-listing name change, but no independent name bridge in local files. "
            "Stable ISIN/listing date and unique current stock mapping alone cannot prove identity. Hold; not confirmed reuse.")
    return {"status": status, "download_allowed": allow, "reason": reason,
            "strict_name_mismatch": not exact, "korean_equivalent": korean,
            "differing_historical_names": differing_names,
            "english_bridge": english, "disclosure_bridge_receipts": sorted({r["rcept_no"] for r in bridged}),
            "isins": isins, "listing_dates": listing_dates, "historical_snapshots": history,
            "mapped_corp_name": corp["corp_name"], "mapped_corp_english_name": corp["corp_eng_name"],
            "corp_master_modify_date": corp["modify_date"]}


def review(plan, master, history, disclosures, output):
    result = copy.deepcopy(plan)
    corp_index = {r["corp_code"]: r for r in master["rows"]}
    for company in result["companies"]:
        code = company["stock_code"]
        corp = corp_index[company["mapping"]["corp_code"]]
        if corp["stock_code"] != code or not history.get(code):
            raise ValueError(f"Missing or inconsistent local mapping evidence: {code}")
        company["identity_review"] = classify(company, corp, history[code], disclosures)
    present = {(int(r["year"]), r["stock_code"]) for r in output}
    counts = Counter(c["identity_review"]["status"] for c in result["companies"])
    result["ticker_identity_review"] = {"method": "local KRX name/English/ISIN/list-date and cached OpenDART names/disclosures",
        "counts": dict(counts), "checked": len(result["companies"]),
        "strict_mismatches": sum(c["identity_review"]["strict_name_mismatch"] for c in result["companies"]),
        "companies_with_differing_historical_names": sum(bool(c["identity_review"]["differing_historical_names"]) for c in result["companies"]),
        "allowed": sum(c["identity_review"]["download_allowed"] for c in result["companies"]),
        "confirmed_different_company": 0,
        "held": [c["stock_code"] for c in result["companies"] if not c["identity_review"]["download_allowed"]]}
    result["requests_before_identity_review"] = result["requests"]
    result["requests"] = estimate_requests(result["companies"], present)
    return result


def write_review(result, path):
    lines = ["# Offline ticker identity review", "",
             "Every original target is listed. Name history is from dated local KRX snapshots (2015–2024).",
             "A matching alias is evidence, not proof of legal succession. Holds are not confirmed ticker reuse.", "",
             "| Code | Historical snapshot names | Mapped corp code / name | Assessment | Download |",
             "| --- | --- | --- | --- | --- |"]
    for c in result["companies"]:
        r = c["identity_review"]
        names = "; ".join(dict.fromkeys(f"{h['short_name']} [{h['first']}–{h['last']}]" for h in r["historical_snapshots"]))
        lines.append(f"| {c['stock_code']} | {names} | {c['mapping']['corp_code']} / {r['mapped_corp_name']} | "
                     f"{r['status']} | {'yes' if r['download_allowed'] else 'HOLD'} |")
    lines += ["", "## Strict name mismatches", "",
              "| Code | Historical names | Mapped Korean / English name | Assessment |",
              "| --- | --- | --- | --- |"]
    for c in result["companies"]:
        r = c["identity_review"]
        if r["strict_name_mismatch"]:
            lines.append(f"| {c['stock_code']} | {', '.join(c['names'])} | {r['mapped_corp_name']} / "
                         f"{r['mapped_corp_english_name']} | {r['status']} |")
    lines += ["", "## All differing historical names after spelling/share-label equivalence", "",
              "This also includes older names where a later alias matches the mapped corporation.", "",
              "| Code | Differing historical names | Mapped name | Evidence / action |",
              "| --- | --- | --- | --- |"]
    for c in result["companies"]:
        r = c["identity_review"]
        if r["differing_historical_names"]:
            lines.append(f"| {c['stock_code']} | {', '.join(r['differing_historical_names'])} | "
                         f"{r['mapped_corp_name']} | {r['status']}; {'retain' if r['download_allowed'] else 'HOLD'} |")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    with offline_only():
        source = Path("outputs/opendart_gap/audit.json")
        plan = read_json(source)
        history = snapshot_history(Path("data/cache/krx_open_api/v1"), {c["stock_code"] for c in plan["companies"]})
        disclosures = [r for path in sorted(Path("cache/opendart/list").glob("*.json"))
                       for r in read_json(path).get("list", [])]
        result = review(plan, read_json("cache/opendart/corpCode_full.json"), history,
                        disclosures, read_json(plan["output_file"]))
        result["ticker_identity_review"]["original_plan_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        atomic_json("outputs/opendart_gap/audit_reviewed.json", result)
        write_review(result, Path("outputs/opendart_gap/ticker_review.md"))
        print(json.dumps({"review": result["ticker_identity_review"], "requests": result["requests"]}, indent=2))


if __name__ == "__main__":
    main()

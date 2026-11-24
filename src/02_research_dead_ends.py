import time
import requests
import pandas as pd

sample = pd.read_csv("data/sample_150_companies.csv")
subset = sample.groupby("tier").head(10).reset_index(drop=True)

print("=== 1. TEST CONTRACTS FINDER (UK Public Procurement OCDS API) ===")
cf_url = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
try:
    resp = requests.get(cf_url, params={"limit": 50}, timeout=15)
    data = resp.json()
    releases = data.get("releases", [])
    suppliers_checked = 0
    vat_found_cf = 0
    sample_ids = []

    for rel in releases:
        for party in rel.get("parties", []):
            if "supplier" in party.get("roles", []):
                suppliers_checked += 1
                ident = party.get("identifier", {})
                additional = party.get("additionalIdentifiers", [])
                all_ids = [ident] + additional
                sample_ids.append((party.get("name"), all_ids))
                for i in all_ids:
                    if "VAT" in str(i.get("scheme", "")).upper() or str(i.get("id", "")).startswith("GB"):
                        vat_found_cf += 1

    print(f"Anunțuri analizate: {len(releases)} | Furnizori verificați: {suppliers_checked}")
    print(f"Furnizori cu VAT identifier expus: {vat_found_cf}")
    print("Exemple de identificatori găsiți:", sample_ids[:3])
except Exception as e:
    print("Eroare Contracts Finder:", e)

print("\n=== 2. TEST THE GAZETTE (UK Insolvency & Corporate Notices) ===")
gazette_hits = 0
gazette_with_vat = 0
for _, row in subset.head(15).iterrows():
    cname = row["CompanyName"]
    url = f"https://www.thegazette.co.uk/all-notices/notice/data.json?text={requests.utils.quote(cname)}"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if r.status_code == 200:
            txt = r.text
            if cname.lower() in txt.lower():
                gazette_hits += 1
                if "vat" in txt.lower():
                    gazette_with_vat += 1
        time.sleep(0.3)
    except Exception:
        pass
print(f"Companii testate pe The Gazette: 15 | Mențiuni găsite: {gazette_hits} | Conțin termenul 'VAT': {gazette_with_vat}")

print("\n=== 3. TEST COMMON CRAWL INDEX API (CDX) ===")
cdx_index = "https://index.commoncrawl.org/CC-MAIN-2026-40-index"
try:
    r = requests.get(
        cdx_index,
        params={"url": "*.co.uk/*terms*", "output": "json", "limit": 5},
        timeout=20,
    )
    lines = [line for line in r.text.strip().split("\n") if line]
    print(f"Status CDX API: {r.status_code} | Exemplu înregistrări returnate: {len(lines)}")
    if lines:
        print("Primul rezultat CDX:", lines[0][:200] + "...")
except Exception as e:
    print("Eroare Common Crawl CDX:", e)
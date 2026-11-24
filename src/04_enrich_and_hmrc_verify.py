import io
import re
import time
from urllib.parse import urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from pypdf import PdfReader
from rapidfuzz import fuzz

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
}


def is_valid_uk_vat_checksum(vat_9_digits: str) -> bool:
    if not re.fullmatch(r"\d{9}", vat_9_digits):
        return False
    if len(set(vat_9_digits)) == 1 or vat_9_digits == "123456789":
        return False
    digits = [int(d) for d in vat_9_digits]
    weights = [8, 7, 6, 5, 4, 3, 2]
    weighted_sum = sum(d * w for d, w in zip(digits[:7], weights))
    check_digits = int(vat_9_digits[7:])
    return ((weighted_sum + check_digits) % 97 == 0) or ((weighted_sum + check_digits + 55) % 97 == 0)


def extract_vat_candidates(text: str) -> list[str]:
    candidates = set()
    explicit_matches = re.finditer(
        r"(?:VAT(?:\s*(?:Reg(?:istration)?|No|Number|ID|Code))?[\s:.\-#]*|GB\s*)(\d[\d\s\-]{7,11}\d)",
        text,
        flags=re.IGNORECASE,
    )
    for m in explicit_matches:
        raw_digits = re.sub(r"\D", "", m.group(1))
        if len(raw_digits) == 12 and raw_digits.endswith("000"):
            raw_digits = raw_digits[:9]
        if len(raw_digits) == 9 and is_valid_uk_vat_checksum(raw_digits):
            candidates.add(raw_digits)
    return sorted(candidates)


def dork_vat_for_company(company_name: str) -> tuple[list[str], str, str]:
    """
    Caută direct mențiuni VAT asociate numelui legal al companiei (snippet + pagină/PDF).
    Returnează (lista_vat, url_sursa, tip_sursa).
    """
    clean_name = re.sub(r"\b(LIMITED|LTD|PLC|LLP)\b", "", company_name, flags=re.IGNORECASE).strip()
    queries = [f'"{clean_name}" "VAT"', f'"{clean_name}" "GB" VAT']

    for q in queries:
        for attempt in range(2):
            try:
                with DDGS() as ddgs:
                    results = list(ddgs.text(q, region="uk-en", max_results=5))
                if not results:
                    time.sleep(1.5)
                    continue

                for res in results:
                    snippet = f"{res.get('title', '')} {res.get('body', '')}"
                    vats_snip = extract_vat_candidates(snippet)
                    if vats_snip:
                        return vats_snip, res.get("href", ""), "4_Search_Dork_Snippet"

                    href = res.get("href", "")
                    if not href.startswith("http"):
                        continue
                    domain = urlparse(href).netloc.lower()
                    if any(x in domain for x in ("service.gov.uk", "endole.co.uk", "opencorporates.com")):
                        continue

                    r = requests.get(href, headers=HEADERS, timeout=8)
                    if r.status_code == 200:
                        if href.lower().endswith(".pdf") or "application/pdf" in r.headers.get("Content-Type", ""):
                            reader = PdfReader(io.BytesIO(r.content))
                            txt = "\n".join(p.extract_text() or "" for p in reader.pages[:6])
                            v = extract_vat_candidates(txt)
                            if v:
                                return v, href, "3_Hosted_PDF_Document"
                        else:
                            v = extract_vat_candidates(r.text)
                            if v:
                                return v, href, "4_Search_Dork_Page"
                break
            except Exception:
                time.sleep(2.0 * (attempt + 1))

    return [], "", ""


def verify_vat_hmrc_web(vat_9_digits: str) -> dict:
    """
    Verifică numărul de TVA pe checker-ul public oficial HMRC (fără API token).
    """
    session = requests.Session()
    session.headers.update(HEADERS)
    form_url = "https://www.tax.service.gov.uk/check-vat-number/enter-vat-details"

    try:
        r_get = session.get(form_url, timeout=12)
        soup = BeautifulSoup(r_get.text, "lxml")
        csrf_input = soup.find("input", {"name": "csrfToken"})
        csrf_token = csrf_input["value"] if csrf_input else ""

        payload = {"csrfToken": csrf_token, "vatNumber": vat_9_digits}
        r_post = session.post(form_url, data=payload, timeout=12, allow_redirects=True)
        page_text = r_post.text

        if "Invalid UK VAT number" in page_text or "does not match a registered" in page_text:
            return {"hmrc_status": "INVALID_NOT_REGISTERED", "hmrc_name": "", "hmrc_postcode": ""}

        res_soup = BeautifulSoup(page_text, "lxml")
        # Extragem câmpurile din tabelul/lista de definiții <dl class="govuk-summary-list">
        rows = res_soup.find_all("div", class_="govuk-summary-list__row")
        extracted = {}
        for row in rows:
            dt = row.find("dt")
            dd = row.find("dd")
            if dt and dd:
                extracted[dt.get_text(" ", strip=True).lower()] = dd.get_text("\n", strip=True)

        reg_name = ""
        reg_addr = ""
        for k, v in extracted.items():
            if "name" in k:
                reg_name = v
            elif "address" in k:
                reg_addr = v

        if reg_name or "Valid UK VAT number" in page_text:
            lines = [line.strip() for line in reg_addr.split("\n") if line.strip()]
            postcode = lines[-1] if lines else ""
            return {
                "hmrc_status": "VALID",
                "hmrc_name": reg_name,
                "hmrc_postcode": postcode,
            }

        return {"hmrc_status": f"UNKNOWN_RESP_{r_post.status_code}", "hmrc_name": "", "hmrc_postcode": ""}
    except Exception as e:
        return {"hmrc_status": f"ERR_{type(e).__name__}", "hmrc_name": "", "hmrc_postcode": ""}


def main():
    df = pd.read_csv("results/poc_150_results.csv").fillna("")
    print(f"Încărcat {len(df)} rânduri. VAT-uri deja găsite: {(df['chosen_vat'] != '').sum()}")

    # Pas A: Dorking țintit pentru companiile fără VAT (prioritizăm Large/Medium și Small, plus un eșantion din Micro)
    updated_count = 0
    for idx, row in df.iterrows():
        if row["chosen_vat"] != "":
            continue
        # Pe 3_Micro testăm primele 25 ca să nu pierdem timp inutil pe rate-limit
        if row["tier"] == "3_Micro" and idx > 125:
            continue

        vats, url_found, src_type = dork_vat_for_company(row["CompanyName"])
        if vats:
            df.at[idx, "chosen_vat"] = vats[0]
            df.at[idx, "candidate_vats"] = ";".join(vats)
            df.at[idx, "discovery_source"] = src_type
            if not row["website"] and url_found:
                df.at[idx, "website"] = url_found
            updated_count += 1
            print(f"[NEW] {row['tier']} | {row['CompanyName'][:30]:30s} -> VAT: {vats[0]} ({src_type})")
        time.sleep(1.2)

    print(f"\nTotal VAT-uri noi descoperite prin Dorking țintit: {updated_count}")

    # Pas B: Verificare oficială HMRC pentru TOATE numerele VAT descoperite
    print("\n=== VERIFICARE OFICIALĂ HMRC ===")
    for idx, row in df.iterrows():
        vat = str(row["chosen_vat"]).strip()
        if not vat:
            continue

        hmrc = verify_vat_hmrc_web(vat)
        df.at[idx, "hmrc_status"] = hmrc["hmrc_status"]
        df.at[idx, "hmrc_name"] = hmrc["hmrc_name"]
        df.at[idx, "hmrc_postcode"] = hmrc["hmrc_postcode"]

        if hmrc["hmrc_name"]:
            sim = fuzz.token_sort_ratio(row["CompanyName"].upper(), hmrc["hmrc_name"].upper())
            df.at[idx, "name_similarity"] = sim

        pc1 = re.sub(r"\s+", "", str(row["CH_PostCode"]).upper())
        pc2 = re.sub(r"\s+", "", str(hmrc["hmrc_postcode"]).upper())
        df.at[idx, "postcode_match"] = bool(pc1 and pc2 and pc1 == pc2)

        print(
            f"VAT {vat} | CH: {row['CompanyName'][:25]:25s} | HMRC: {hmrc['hmrc_status']} ({hmrc['hmrc_name'][:25]}) | Sim: {df.at[idx, 'name_similarity']}"
        )
        time.sleep(1.0)

    df.to_csv("results/poc_150_verified.csv", index=False)
    print("\nSalvat rezultatele finale validate în results/poc_150_verified.csv!")
    print(df[df["chosen_vat"] != ""][["tier", "CompanyName", "chosen_vat", "discovery_source", "hmrc_status", "hmrc_name", "name_similarity", "postcode_match"]])


if __name__ == "__main__":
    main()
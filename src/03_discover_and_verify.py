import io
import os
import re
import time
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from pypdf import PdfReader
from rapidfuzz import fuzz

# Domenii agregatoare pe care le ignorăm când căutăm site-ul oficial al firmei
AGGREGATOR_DOMAINS = {
    "service.gov.uk",
    "endole.co.uk",
    "companycheck.co.uk",
    "duedil.com",
    "dnb.com",
    "opencorporates.com",
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "yell.com",
    "yelp.co.uk",
    "checkatrade.com",
    "trustpilot.com",
    "glassdoor.co.uk",
    "indeed.com",
    "wikipedia.org",
    "bloomberg.com",
    "zoominfo.com",
    "bizdb.co.uk",
    "ukcompanydata.com",
    "companieslist.co.uk",
}

KEYWORDS_URL = (
    "contact",
    "about",
    "terms",
    "condition",
    "privacy",
    "legal",
    "policy",
    "imprint",
    "invoice",
    "supplier",
    "quality",
    "compliance",
    "cookie",
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
}


def is_valid_uk_vat_checksum(vat_9_digits: str) -> bool:
    """
    Algoritmul oficial HMRC Modulus 97 pentru numere de TVA din UK (9 cifre).
    Include atât schema pre-2010 (mod 97 == 0), cât și schema post-2010 (diferența de 55).
    """
    if not re.fullmatch(r"\d{9}", vat_9_digits):
        return False
    # Evităm șiruri triviale gen 000000000 sau 123456789
    if len(set(vat_9_digits)) == 1 or vat_9_digits == "123456789":
        return False

    digits = [int(d) for d in vat_9_digits]
    weights = [8, 7, 6, 5, 4, 3, 2]
    weighted_sum = sum(d * w for d, w in zip(digits[:7], weights))
    check_digits = int(vat_9_digits[7:])

    # Regula standard (pre-2010)
    rem_old = (weighted_sum + check_digits) % 97
    # Regula nouă (post-2010: se scade 55)
    rem_new = (weighted_sum + check_digits + 55) % 97

    return rem_old == 0 or rem_new == 0


def extract_vat_candidates(text: str) -> list[str]:
    """
    Extrage numere de 9 cifre din text și le filtrează prin context + checksum.
    """
    candidates = set()
    # Pattern 1: Precedat explicit de GB sau VAT (ex: GB 123 4567 89, VAT No: 123456789)
    explicit_matches = re.finditer(
        r"(?:VAT(?:\s*(?:Reg(?:istration)?|No|Number|ID|Code))?[\s:.\-#]*|GB\s*)(\d[\d\s\-]{7,11}\d)",
        text,
        flags=re.IGNORECASE,
    )
    for m in explicit_matches:
        raw_digits = re.sub(r"\D", "", m.group(1))
        # Dacă e cod EORI (12 cifre: 9 cifre VAT + 000), extragem primele 9 cifre
        if len(raw_digits) == 12 and raw_digits.endswith("000"):
            raw_digits = raw_digits[:9]
        if len(raw_digits) == 9 and is_valid_uk_vat_checksum(raw_digits):
            candidates.add(raw_digits)

    return sorted(candidates)


def find_official_website(company_name: str, postcode: str) -> str | None:
    clean_name = re.sub(r"\b(LIMITED|LTD|PLC|LLP)\b", "", company_name, flags=re.IGNORECASE).strip()
    query = f'"{clean_name}" UK'
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, region="uk-en", max_results=6))
        for res in results:
            href = res.get("href", "")
            if not href.startswith("http"):
                continue
            domain = urlparse(href).netloc.lower().replace("www.", "")
            if any(agg in domain for agg in AGGREGATOR_DOMAINS):
                continue
            return f"{urlparse(href).scheme}://{urlparse(href).netloc}"
    except Exception:
        pass
    return None


def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages_text = []
        for page in reader.pages[:8]:  # maxim primele 8 pagini
            t = page.extract_text()
            if t:
                pages_text.append(t)
        return "\n".join(pages_text)
    except Exception:
        return ""


def crawl_site_for_vat(base_url: str) -> tuple[list[str], str]:
    """
    Caută VAT pe Homepage, apoi pe subpagini legale HTML, apoi în PDF-uri interne.
    Returnează (lista_vat_gasite, tipul_sursei).
    """
    try:
        resp = requests.get(base_url, headers=HEADERS, timeout=10, allow_redirects=True)
        if resp.status_code != 200:
            return [], "HTTP_Error"
    except Exception:
        return [], "Connection_Error"

    # 1. Verificăm Homepage
    home_vats = extract_vat_candidates(resp.text)
    if home_vats:
        return home_vats, "1_Homepage_HTML"

    soup = BeautifulSoup(resp.text, "lxml")
    base_domain = urlparse(resp.url).netloc.lower()

    html_links = []
    pdf_links = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        full_url = urljoin(resp.url, href)
        parsed = urlparse(full_url)
        if parsed.netloc.lower() != base_domain:
            continue

        path_lower = parsed.path.lower()
        anchor_text = a.get_text(" ", strip=True).lower()

        if path_lower.endswith(".pdf"):
            if any(k in path_lower or k in anchor_text for k in KEYWORDS_URL) or len(pdf_links) < 3:
                if full_url not in pdf_links:
                    pdf_links.append(full_url)
        elif any(k in path_lower or k in anchor_text for k in KEYWORDS_URL):
            if full_url not in html_links and full_url != resp.url:
                html_links.append(full_url)

    # 2. Verificăm maxim 5 subpagini legale HTML
    for link in html_links[:5]:
        try:
            r_sub = requests.get(link, headers=HEADERS, timeout=8)
            if r_sub.status_code == 200:
                sub_vats = extract_vat_candidates(r_sub.text)
                if sub_vats:
                    return sub_vats, "2_Legal_Subpage_HTML"
        except Exception:
            continue

    # 3. Verificăm maxim 3 fișiere PDF găzduite pe domeniu
    for pdf_url in pdf_links[:3]:
        try:
            r_pdf = requests.get(pdf_url, headers=HEADERS, timeout=12)
            if r_pdf.status_code == 200 and len(r_pdf.content) < 5_000_000:
                pdf_text = extract_text_from_pdf_bytes(r_pdf.content)
                pdf_vats = extract_vat_candidates(pdf_text)
                if pdf_vats:
                    return pdf_vats, "3_Hosted_PDF_Document"
        except Exception:
            continue

    return [], "Not_Found_On_Site"


def verify_vat_hmrc(vat_9_digits: str, token: str | None) -> dict:
    """
    Verifică numărul VAT la HMRC API (dacă există token).
    """
    if not token:
        return {"hmrc_status": "PENDING_TOKEN", "hmrc_name": "", "hmrc_postcode": ""}

    url = f"https://api.service.hmrc.gov.uk/organisations/vat/check-vat-number/lookup/{vat_9_digits}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.hmrc.2.0+json",
    }
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json().get("target", {})
            addr = data.get("address", {})
            return {
                "hmrc_status": "VALID",
                "hmrc_name": data.get("name", ""),
                "hmrc_postcode": addr.get("postcode", ""),
            }
        elif r.status_code == 404:
            return {"hmrc_status": "INVALID_NOT_REGISTERED", "hmrc_name": "", "hmrc_postcode": ""}
        else:
            return {"hmrc_status": f"API_ERR_{r.status_code}", "hmrc_name": "", "hmrc_postcode": ""}
    except Exception as e:
        return {"hmrc_status": f"ERR_{type(e).__name__}", "hmrc_name": "", "hmrc_postcode": ""}


def main():
    sample = pd.read_csv("data/sample_150_companies.csv")
    hmrc_token = os.getenv("HMRC_ACCESS_TOKEN")

    results = []
    print(f"Încep procesarea a {len(sample)} companii... (HMRC Token activ: {bool(hmrc_token)})")

    for idx, row in sample.iterrows():
        cname = row["CompanyName"]
        cnum = row["CompanyNumber"]
        postcode = str(row["RegAddress.PostCode"])
        tier = row["tier"]

        website = find_official_website(cname, postcode)
        vats = []
        source_type = "No_Website"

        if website:
            vats, source_type = crawl_site_for_vat(website)

        chosen_vat = vats[0] if vats else ""
        hmrc_info = {"hmrc_status": "", "hmrc_name": "", "hmrc_postcode": ""}
        name_similarity = 0
        postcode_match = False

        if chosen_vat:
            hmrc_info = verify_vat_hmrc(chosen_vat, hmrc_token)
            if hmrc_info["hmrc_name"]:
                name_similarity = fuzz.token_sort_ratio(
                    cname.upper(), hmrc_info["hmrc_name"].upper()
                )
            if hmrc_info["hmrc_postcode"] and postcode:
                pc1 = re.sub(r"\s+", "", postcode.upper())
                pc2 = re.sub(r"\s+", "", hmrc_info["hmrc_postcode"].upper())
                postcode_match = pc1 == pc2

        results.append(
            {
                "tier": tier,
                "CompanyNumber": cnum,
                "CompanyName": cname,
                "CH_PostCode": postcode,
                "website": website or "",
                "candidate_vats": ";".join(vats),
                "chosen_vat": chosen_vat,
                "discovery_source": source_type,
                "hmrc_status": hmrc_info["hmrc_status"],
                "hmrc_name": hmrc_info["hmrc_name"],
                "hmrc_postcode": hmrc_info["hmrc_postcode"],
                "name_similarity": name_similarity,
                "postcode_match": postcode_match,
            }
        )

        if (idx + 1) % 10 == 0 or chosen_vat:
            print(
                f"[{idx+1}/150] {tier} | {cname[:28]:28s} | Web: {bool(website)} | VAT: {chosen_vat or '-':9s} | Sursa: {source_type}"
            )
        time.sleep(0.4)

    out_df = pd.DataFrame(results)
    out_df.to_csv("results/poc_150_results.csv", index=False)
    print("\n=== SUMAR FINAL ===")
    print(out_df.groupby(["tier", "discovery_source"]).size())


if __name__ == "__main__":
    main()
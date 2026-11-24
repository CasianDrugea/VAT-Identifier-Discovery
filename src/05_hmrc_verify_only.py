import re
import time
import pandas as pd
from playwright.sync_api import sync_playwright
from rapidfuzz import fuzz


def main():
    df = pd.read_csv("results/poc_150_verified.csv", dtype=str).fillna("")
    df["chosen_vat"] = df["chosen_vat"].apply(
        lambda x: str(int(float(x))).zfill(9) if x.strip() != "" else ""
    )

    unique_vats = sorted(set(v for v in df["chosen_vat"] if v))
    print(f"Verific {len(unique_vats)} numere VAT unice pe GOV.UK HMRC Checker...")

    cache = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="en-GB")
        page = context.new_page()

        for i, vat in enumerate(unique_vats, 1):
            try:
                page.goto(
                    "https://www.tax.service.gov.uk/check-vat-number/enter-vat-details",
                    wait_until="commit",
                    timeout=15000,
                )
                textbox = page.locator("form input[type='text']").first
                textbox.wait_for(state="visible", timeout=8000)
                textbox.fill(vat)
                page.locator("form button[type='submit']").first.click()

                # Așteptăm să plece de pe /enter-vat-details spre /known sau /unknown
                page.wait_for_url(re.compile(r"/check-vat-number/(known|unknown)"), timeout=10000)
                page.wait_for_timeout(400)

                if page.url.endswith("/unknown"):
                    cache[vat] = {"hmrc_status": "INVALID_NOT_REGISTERED", "hmrc_name": "", "hmrc_postcode": ""}
                elif page.url.endswith("/known"):
                    body_lines = [
                        line.strip()
                        for line in page.inner_text("body").split("\n")
                        if line.strip()
                    ]
                    reg_name = ""
                    postcode = ""
                    for idx_l, line in enumerate(body_lines):
                        if line.lower() in ("name", "registered name", "business name") and idx_l + 1 < len(body_lines):
                            reg_name = body_lines[idx_l + 1]
                        if re.fullmatch(r"[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}", line.upper()):
                            postcode = line.upper()

                    cache[vat] = {"hmrc_status": "VALID", "hmrc_name": reg_name, "hmrc_postcode": postcode}
                else:
                    cache[vat] = {"hmrc_status": f"OTHER_URL_{page.url}", "hmrc_name": "", "hmrc_postcode": ""}
            except Exception as e:
                cache[vat] = {"hmrc_status": f"ERR_{type(e).__name__}", "hmrc_name": "", "hmrc_postcode": ""}

            res = cache[vat]
            print(f"[{i}/{len(unique_vats)}] VAT {vat} -> {res['hmrc_status']} | {res['hmrc_name']} | {res['hmrc_postcode']}")

        browser.close()

    for idx, row in df.iterrows():
        vat = row["chosen_vat"]
        if not vat:
            continue
        hmrc = cache[vat]
        df.at[idx, "hmrc_status"] = hmrc["hmrc_status"]
        df.at[idx, "hmrc_name"] = hmrc["hmrc_name"]
        df.at[idx, "hmrc_postcode"] = hmrc["hmrc_postcode"]
        sim = 0
        if hmrc["hmrc_name"]:
            sim = fuzz.token_sort_ratio(row["CompanyName"].upper(), hmrc["hmrc_name"].upper())
        df.at[idx, "name_similarity"] = str(round(sim, 1))
        pc1 = re.sub(r"\s+", "", str(row["CH_PostCode"]).upper())
        pc2 = re.sub(r"\s+", "", str(hmrc["hmrc_postcode"]).upper())
        df.at[idx, "postcode_match"] = str(bool(pc1 and pc2 and pc1 == pc2))

    df.to_csv("results/poc_150_verified.csv", index=False)
    print("\nSalvat results/poc_150_verified.csv cu date reale HMRC!")


if __name__ == "__main__":
    main()
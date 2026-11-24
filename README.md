# UK VAT Identifier Discovery

Can we reliably build a UK company VAT dataset from the open web to enrich a 40,000-supplier procurement list?

**Short answer:** Yes, for active mid-to-large B2B suppliers (~40–44% discovery rate), **provided** you aggressively filter out cross-company collisions. For micro-entities, open-web discovery drops to ~8%, mostly because they sit below the £90k VAT registration threshold.

---

## Part 1: Research & Dead Ends

Before writing the main pipeline, I tested several public registries and web sources on subsets of UK companies to see what actually holds VAT data and what doesn't.

### What didn't work (Dead Ends)
* **Companies House Bulk Snapshot (`BasicCompanyDataAsOneFile-2026-11-01.csv`):**
  * Parsed all **5,711,779** records (**4,765,570** active Ltd/PLC companies).
  * Gives exact legal names, `CompanyNumber` (CRN), postcodes, SIC cods, and account types, **but zero websites and zero VAT numbers**. Used it purely as my sampling frame.
* **Contracts Finder (UK Public Procurement OCDS API) — *0/48 hits*:**
  * Checked 50 recent public procurement notices covering 48 suppliers (`src/02_research_dead_ends.py`).
  * **Result:** Every single supplier record uses `scheme: "GB-COH"` (Companies House number), never a VAT number. Completely useless for VAT discovery.
* **The Gazette (Insolvency & Corporate Notices) — *3/15 mentions, wrong bias*:**
  * Queried 15 companies via their JSON feed. Only 3 contained the word `"VAT"`, mostly in liquidation/winding-up notices. Finding a supplier's VAT only after they go bankrupt doesn't help an active procurement team.
* **Sequential DuckDuckGo Scraping (`ddgs`):**
  * Hitting search sequentially without backoff triggered silent connection resets (`h2 connection driver error`) after ~35–40 queries, returning false `No_Website` misses on 81/150 companies in my first pass.

### What actually works
* **Legal pages & hosted PDFs:** UK law doesn't force companies to put their VAT number on the homepage, so homepages rarely have it. However, B2B suppliers routinely include it in **Terms & Conditions, Privacy Policies, Credit Application forms, and ISO/quality PDFs** hosted on their domain.
* **EORI numbers (`GB` + 9-digit VAT + `000`):** UK importers/exporters expose their EORI number on shipping/customs pages. Stripping the trailing `000` gives the exact VAT number.
* **HMRC Modulus 97 Checksum:** Filtering 9-digit regex matches with HMRC's checksum (both the pre-2010 and post-2010 `-55` offset rules) wipes out phone numbers and 8-digit company numbers before touching HMRC's verifier.

---

## Part 2: Proof of Concept ($N = 150$)

### How I built the sample (`src/01_sample_companies.py`)
Pulling 150 random firms from all 4.76M active UK companies would mostly hit dormant shells and sole-director micro-firms. To mirror a real manufacturer's supplier base, I filtered Companies House to **active Ltd/PLC companies in supply-chain SIC sectors** (`20–33` Manufacturing, `41–43` Construction, `46` Wholesale, `49–52` Transport/Logistics — **928,641 companies** total) and took a stratified random sample (`seed=42`) of **50 companies per tier**:
1. `1_Large_Medium` (`FULL`, `MEDIUM`, `GROUP` accounts)
2. `2_Small` (`SMALL` accounts)
3. `3_Micro` (`MICRO ENTITY` accounts)

### Pipeline (`src/03_discover_and_verify.py` $\rightarrow$ `src/05_analyze_results.py`)
1. Crawl homepage + internal legal links + hosted `.pdf` files (`pypdf`), backed by parallel targeted search dorks (`"{CompanyName}" "VAT"`).
2. Extract `GB` / `VAT` / `EORI` candidates and validate with the Modulus 97 checksum.
3. Verify every candidate against GOV.UK's official HMRC VAT checker via headless Chromium (`src/05_hmrc_verify_only.py`, checking redirect to `/known` vs. `/unknown`).
4. Flag **collisions** (same VAT appearing across multiple unrelated companies) and verify domain/entity alignment.

### Results (`results/poc_150_final.csv`)

| Tier | Sample | Checksum OK | HMRC Valid | HMRC Rejected | Collision False Positives | Needs Review | **Verified True Positives** | **Hit Rate** |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **1_Large_Medium** | 50 | 26 | 26 | 0 | 3 | 1 | **22** | **44.0%** |
| **2_Small** | 50 | 25 | 25 | 0 | 4 | 1 | **20** | **40.0%** |
| **3_Micro** | 50 | 12 | 10 | 2 | 4 | 2 | **4** | **8.0%** |
| **Total** | **150** | **63** | **61** | **2** | **11** | **4** | **46** | **30.7%** |

### False-Positive Analysis
* **Raw error rate before deduplication:** Out of **63** mathematically valid candidates extracted from the web, **13 were wrong** (`2` deregistered/invalid on HMRC + `11` collision false positives) $\rightarrow$ **20.6% raw false-positive rate**.
* **The collision trap:** VAT `225936591` showed up 9 times across completely unrelated firms (`COVERWORLD UK LIMITED`, `IN VINO LIMITED`, `NISHANE UK LTD`, etc.), and `731331179` appeared twice. Catching multi-entity collisions dropped false positives to near zero.
* **Blind spots:** The unauthenticated GOV.UK web checker confirms `VALID` vs. `INVALID` without login, but hides the registered business name/address unless you authenticate with your own UK VAT number. Also, legitimate **VAT Groups** (subsidiaries sharing a parent's VAT) currently get flagged by a strict uniqueness filter unless cross-referenced with Companies House ownership data.

---

## Part 3: Scaling With Real Resources

Querying search engines for 4.76M companies doesn't scale (too expensive, too slow, hits rate limits). With a real budget, I would **invert the pipeline**:

1. **Bottom-up extraction from Common Crawl (~$350 compute):** Run a Spark/Ray job over `.uk` / `.co.uk` pages and PDFs in Common Crawl to extract all `(domain, vat_candidate)` pairs passing Modulus 97 (~1.5M–2M unique VATs).
2. **Authenticated HMRC Bulk API Validation ($0 API cost):** Use an approved HMRC Production API key (authenticated with Veridion's VAT) to validate all unique candidates. Since the authenticated API returns the **official registered legal name and postcode**, we don't need to guess websites first.
3. **Entity Resolution against Companies House:** Join `(HMRC Legal Name, Postcode, Source Domain)` directly onto Companies House bulk data using fuzzy token matching + postcode alignment, using corporate group/PSC filings to separate real **VAT Groups** from web-agency footer leaks.
4. **Targeted Proxy Crawl (~$450) + QA (~$1,200):** Run headless Playwright + UK residential proxies only on the unmatched high-priority tail (e.g., the customer's 40,000 suppliers), and manually annotate ~2,000 edge cases.

**Rough cost:** **~$2,000** for a national UK build (~**$0.0004/company**), or **~$0.05/supplier** for a custom 40,000-supplier run.

**What breaks first & monitoring:**
* **HMRC API rate limits (`429`):** Must throttle validation through an async token-bucket queue (~3–5 req/sec), not burst workers.
* **Virtual office postcodes:** Thousands of UK firms share an accountant's postcode (`EC1V` / `W1W`), so postcode matches must be secondary to legal name + group hierarchy.
* **Production checks:** Alert if any VAT maps to $>1$ unrelated CRN, track coverage by SIC/account tier (any spike in `DORMANT` firms = scraper bug), and benchmark every release against a 1,000-company human-verified golden set.

---

## Debate Topics

* **Brute-forcing HMRC with the Modulus 97 checksum:**
  * The checksum narrows $10^9$ combinations down to ~**20M** mathematically valid numbers (~2.18M active). Technically small enough to enumerate, practically a bad idea: it violates HMRC's API *Terms of Use*, triggers WAF bans, risks legal issues under the UK *Computer Misuse Act*, and still leaves ~1M sole traders and VAT groups unresolved.
* **Keeping the dataset fresh:**
  * Ingest daily/monthly Companies House snapshots to drop dissolved companies, re-verify active VATs against HMRC on a rolling quarterly schedule to catch deregistrations, and scan monthly Common Crawl deltas for new entrants.
* **Detecting silent errors without ground truth:**
  * Maintain a 1,000-company stratified golden set ("canaries"), enforce graph invariants (flag high fan-out VATs shared across unrelated domains), and check base-rate sanity bounds (`Large > Small >> Micro >> Dormant`).
* **Sources I wouldn't ship in a commercial product:**
  * Third-party lookup directories (`vat-lookup.co.uk`, `gbrbusiness.com`, `endole.co.uk`) that showed up in search snippets. Scraping and reselling aggregator data brings copyright/database-right issues, ToS violations, and stale circular errors. Stick to primary open-web domains + Companies House (Open Government Licence) + HMRC verification.

---

## Beyond the UK

### Germany (`DE`) vs. UK
* **Easier (Discovery):** Germany's **Impressumspflicht** (§ 5 DDG) legally requires commercial websites to display an *Impressum* with their legal name, *Handelsregister* ID (`HRB`/`HRA`), and **USt-IdNr** (`DE` + 9 digits). Discovery rates on German B2B sites easily reach **75–85%**.
* **Harder (Verification):** German companies have both a local *Steuernummer* and an EU *USt-IdNr*, and EU **VIES** checks for `DE` return **only `Valid: True/False`** (no name or address due to German privacy laws). You lose verifier-side entity matching and must rely on parsing the `HRB` number directly from the *Impressum*.

### A country where VAT numbers are barely "discovered" at all: Sweden (`SE`)
* Swedish VAT numbrs (*Momsregistreringsnummer*) are **derived deterministically** from the 10-digit company registration number (*Organisationsnummer*): `SE` + `OrgNr` (no hyphen) + `01` (same pattern in **Denmark** `DK` + CVR and **Belgium** `BE0` + enterprise number).
* **Takeaway:** Spend **$0** crawling the web for deterministic-ID countries—just compute the VAT from the corporate registry and ping VIES to check if it's active. Save crawling budgets for decoupled-ID markets (**UK, Germany, France, Netherlands**).

---

## Repo Structure

```text
├── src/
│   ├── 01_sample_companies.py      # Stratified sampling from Companies House CSV
│   ├── 02_research_dead_ends.py    # Empirical checks on Contracts Finder & The Gazette
│   ├── 03_disover_and_verify.py   # Homepage + legal subpage + PDF crawler & Modulus 97 filter
│   ├── 04_enrich_and_hmrc_verify.py# Parallel targeted search dorking
│   ├── 05_hmrc_verify_only.py      # Headless GOV.UK HMRC VAT checker verification
│   └── 05_analyze_results.py       # Collision detection & final metrics
├── data/
│   └── sample_150_companies.csv    # Reproducible N=150 stratified sample (seed=42)
└── results/
    └── poc_150_final.csv           # Verified dataset with HMRC status & collision flags
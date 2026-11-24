import glob
import pandas as pd

csv_files = glob.glob("data/BasicCompanyDataAsOneFile-*.csv")
if not csv_files:
    raise FileNotFoundError("Nu am găsit CSV-ul Companies House în data/")

csv_path = csv_files[0]
print(f"Citesc {csv_path}...")

target_cols = {
    "CompanyName",
    "CompanyNumber",
    "RegAddress.PostCode",
    "CompanyCategory",
    "CompanyStatus",
    "IncorporationDate",
    "Accounts.AccountCategory",
    "SICCode.SicText_1",
}

relevant_prefixes = tuple(
    [str(i) for i in range(20, 34)] + ["41", "42", "43", "46", "49", "50", "51", "52"]
)

chunks = []
total_rows = 0
active_rows = 0

for chunk in pd.read_csv(
    csv_path,
    usecols=lambda c: c.strip() in target_cols,
    chunksize=200_000,
    low_memory=False,
):
    chunk.columns = chunk.columns.str.strip()
    total_rows += len(chunk)

    active = chunk[
        (chunk["CompanyStatus"] == "Active")
        & (chunk["CompanyCategory"].isin(["Private Limited Company", "Public Limited Company"]))
    ].copy()
    active_rows += len(active)

    active["sic_prefix"] = active["SICCode.SicText_1"].astype(str).str[:2]
    b2b = active[active["sic_prefix"].isin(relevant_prefixes)]
    chunks.append(b2b)

df_b2b = pd.concat(chunks, ignore_index=True)

print(f"Total înregistrări în Companies House: {total_rows:,}")
print(f"Total companii Active (Ltd/PLC): {active_rows:,}")
print(f"Total companii B2B/Supply-Chain (SIC filtrat): {len(df_b2b):,}")
print("\nDistribuție după categoria contabilă:")
print(df_b2b["Accounts.AccountCategory"].value_counts())

SEED = 42

tier_large = df_b2b[df_b2b["Accounts.AccountCategory"].isin(["FULL", "MEDIUM", "GROUP"])].sample(
    n=50, random_state=SEED
)
tier_large["tier"] = "1_Large_Medium"

tier_small = df_b2b[df_b2b["Accounts.AccountCategory"] == "SMALL"].sample(
    n=50, random_state=SEED
)
tier_small["tier"] = "2_Small"

tier_micro = df_b2b[df_b2b["Accounts.AccountCategory"] == "MICRO ENTITY"].sample(
    n=50, random_state=SEED
)
tier_micro["tier"] = "3_Micro"

sample_df = pd.concat([tier_large, tier_small, tier_micro], ignore_index=True)
sample_df.to_csv("data/sample_150_companies.csv", index=False)
print("\nSalvat eșantionul în data/sample_150_companies.csv (150 companii).")
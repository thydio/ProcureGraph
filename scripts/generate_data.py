"""
Synthetic procurement data generator for ProcureGraph.

Produces OCDS-like flat CSVs (extended with the fields the seven detector
subsystems need: bank accounts, directors, registration facts, amendments,
approval thresholds, context justifications).

Everything here is invented. Names, PANs, bank accounts and prices are fake.

Output: data/raw/*.csv
"""

import csv
import math
import os
import random
from datetime import date, timedelta

SEED = 20260917
random.seed(SEED)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "raw")
os.makedirs(OUT, exist_ok=True)

TODAY = date(2026, 9, 17)
START = date(2022, 9, 1)

L = 100000.0  # one lakh, in rupees


# --------------------------------------------------------------------------
# reference data
# --------------------------------------------------------------------------

GOVERNMENTS = [
    # (government_level, government_name, state_code, region factor)
    ("State Government", "Government of Karnataka", "KA", 1.00),
    ("State Government", "Government of Maharashtra", "MH", 1.08),
    ("State Government", "Government of Rajasthan", "RJ", 0.95),
    ("State Government", "Government of Kerala", "KL", 1.05),
    ("State Government", "Government of Odisha", "OD", 0.92),
    ("Union Government", "Government of India", "IN", 1.03),
]

DEPARTMENTS = {
    "Public Works Department": ["road_resurfacing", "building_construction", "borewell_drilling"],
    "Rural Development & Panchayat Raj": ["road_resurfacing", "solar_streetlight", "borewell_drilling"],
    "Water Resources Department": ["water_pipeline", "borewell_drilling"],
    "Health & Family Welfare": ["drugs_generic", "medical_supplies", "biomedical_service", "ambulance_vehicle"],
    "School Education Department": ["school_uniforms", "office_furniture", "building_construction"],
    "Urban Development Department": ["sanitation_services", "solar_streetlight", "road_resurfacing"],
    "IT, BT & e-Governance": ["laptops_it", "security_services"],
    "Transport Department": ["ambulance_vehicle", "security_services", "building_construction"],
}

# category -> (label, unit, scope unit, base unit rate INR, kind, typical qty range)
CATEGORIES = {
    "road_resurfacing": ("Road resurfacing works", "sq.m", "sq.m", 178.0, "works", (8000, 60000)),
    "building_construction": ("Building construction works", "sq.m", "sq.m", 1650.0, "works", (400, 4000)),
    "water_pipeline": ("Water supply pipeline works", "metre", "metre", 2200.0, "works", (300, 6000)),
    "borewell_drilling": ("Borewell drilling", "metre", "metre", 1450.0, "works", (200, 2500)),
    "solar_streetlight": ("Solar street lighting", "unit", "unit", 12500.0, "goods", (40, 900)),
    "drugs_generic": ("Generic drug supply", "strip", "strip", 22.0, "goods", (20000, 900000)),
    "medical_supplies": ("Medical consumables supply", "unit", "unit", 480.0, "goods", (2000, 60000)),
    "ambulance_vehicle": ("Ambulance procurement", "unit", "unit", 1850000.0, "goods", (2, 30)),
    "laptops_it": ("Laptop and IT hardware supply", "unit", "unit", 42000.0, "goods", (25, 900)),
    "office_furniture": ("Office and school furniture", "unit", "unit", 7800.0, "goods", (100, 3000)),
    "school_uniforms": ("School uniform supply", "set", "set", 620.0, "goods", (3000, 90000)),
    "sanitation_services": ("Sanitation and waste services", "month", "month", 145000.0, "services", (6, 36)),
    "security_services": ("Manned security services", "man-month", "man-month", 21000.0, "services", (24, 600)),
    "biomedical_service": ("Biomedical equipment servicing", "year", "year", 1450000.0, "services", (1, 5)),
}

DISTRICTS = {
    "KA": ["Dakshina Kannada", "Belagavi", "Kalaburagi", "Mysuru", "Tumakuru"],
    "MH": ["Nashik", "Amravati", "Solapur", "Ratnagiri", "Nagpur"],
    "RJ": ["Bikaner", "Alwar", "Kota", "Barmer", "Udaipur"],
    "KL": ["Palakkad", "Kollam", "Wayanad", "Thrissur"],
    "OD": ["Sambalpur", "Ganjam", "Koraput", "Balasore"],
    "IN": ["New Delhi", "Chennai", "Guwahati", "Bhopal"],
}

FIRST = ["Arka", "Bhoomi", "Chakra", "Dhruva", "Ekta", "Garuda", "Hansa", "Indra", "Jyoti", "Kaveri",
         "Lakshmi", "Meru", "Nandi", "Oja", "Pragati", "Rudra", "Sagar", "Tarang", "Ujjwal", "Vayu",
         "Yamuna", "Zenith", "Amba", "Bharat", "Chetak", "Deep", "Eshan", "Falgun", "Girija", "Hemant",
         "Ira", "Kalpa", "Lohit", "Mandar", "Nirmal", "Omkar", "Parth", "Rohan", "Shakti", "Trishul",
         "Utkal", "Vidhi", "Yuvan", "Anant", "Bindu", "Charan", "Devi", "Gokul", "Harsha", "Ishan"]

SECOND = ["Infra", "Constructions", "Builders", "Roadways", "Enterprises", "Engineering", "Traders",
          "Industries", "Solutions", "Technologies", "Agencies", "Contractors", "Systems", "Supplies",
          "Services", "Associates", "Ventures", "Works", "Projects", "Corporation"]

SUFFIX = ["Pvt Ltd", "Private Limited", "LLP", "Ltd", "& Co", ""]

SURNAMES = ["Menon", "Rao", "Shetty", "Patil", "Sharma", "Iyer", "Naik", "Desai", "Kulkarni", "Reddy",
            "Bhat", "Joshi", "Pillai", "Mehta", "Chauhan", "Nair", "Gowda", "Sahoo", "Mishra", "Kamath"]

INITIALS = ["A", "B", "D", "G", "H", "K", "M", "N", "P", "R", "S", "T", "V"]

DISQ_REASONS = ["EMD not submitted", "Bid form unsigned", "Technical bid incomplete",
                "Validity period short", "Turnover certificate missing"]

AMEND_REASONS = ["additional_quantities", "scope_change_approved", "price_variation_clause",
                 "site_condition_change", "time_extension_cost"]


def d(days_from_start):
    return START + timedelta(days=days_from_start)


def rnd_date(a, b):
    return a + timedelta(days=random.randint(0, max(1, (b - a).days)))


def price_index(dt):
    """Simple cost inflation index, base 2022."""
    years = (dt - date(2022, 1, 1)).days / 365.25
    return 1.0 + 0.055 * years


def money(x):
    return round(float(x), 2)


# --------------------------------------------------------------------------
# buyers
# --------------------------------------------------------------------------

buyers = []
bid_seq = 0

for lvl, gov, code, rfac in GOVERNMENTS:
    depts = random.sample(list(DEPARTMENTS), 5 if code != "IN" else 4)
    for dep in depts:
        n_units = random.choice([1, 1, 2])
        for k in range(n_units):
            district = random.choice(DISTRICTS[code])
            unit_name = {
                "Public Works Department": "District Roads Division",
                "Rural Development & Panchayat Raj": "Zilla Panchayat Engineering Wing",
                "Water Resources Department": "Water Supply Sub-Division",
                "Health & Family Welfare": "District Health Society",
                "School Education Department": "District Education Office",
                "Urban Development Department": "City Municipal Corporation",
                "IT, BT & e-Governance": "e-Governance Procurement Cell",
                "Transport Department": "Transport Commissionerate Stores",
            }[dep]
            bid_seq += 1
            buyers.append({
                "buyer_id": f"B-{bid_seq:03d}",
                "buyer_name": f"{unit_name}, {district}",
                "government_level": lvl,
                "government_name": gov,
                "state_code": code,
                "department": dep,
                "district": district,
                "region_factor": rfac * random.uniform(0.96, 1.05),
                "categories": DEPARTMENTS[dep],
            })

# --------------------------------------------------------------------------
# vendors
# --------------------------------------------------------------------------

used_names = set()


def vendor_name():
    while True:
        n = f"{random.choice(FIRST)} {random.choice(SECOND)}"
        suf = random.choice(SUFFIX)
        full = (n + " " + suf).strip()
        if full not in used_names:
            used_names.add(full)
            return full


def person_name():
    return f"{random.choice(INITIALS)}. {random.choice(SURNAMES)}"


vendors = []
people = []          # vendor_id, person_id, name, role, start
persons_pool = {}    # person_id -> name

MASS_ADDRESS = "ADDR-BLD-9001"     # business centre, hundreds of registrations
CONSULTANT_EMAIL = "filings@sahaytaconsultants.example"
CONSULTANT_PHONE = "9008811220"
INTERMEDIARY_ACCT = "ACCT-INTERMED-77001"


def make_vendor(vid, profile="normal", state=None, cats=None, inc=None):
    state = state or random.choice([g[2] for g in GOVERNMENTS])
    cats = cats or random.sample(list(CATEGORIES), random.choice([1, 1, 2, 3]))
    if inc is None:
        inc = rnd_date(date(2005, 1, 1), date(2024, 6, 1))

    size = random.choices(["micro", "small", "mid", "large"], [0.30, 0.35, 0.25, 0.10])[0]
    cap = {"micro": (100000, 500000), "small": (500000, 2500000),
           "mid": (2500000, 20000000), "large": (20000000, 250000000)}[size]
    capital = random.uniform(*cap)
    turnover = capital * random.uniform(2.5, 14.0)

    building = MASS_ADDRESS if random.random() < 0.06 else f"ADDR-BLD-{random.randint(1000, 8999)}"
    unit = f"{building}-U{random.randint(1, 40)}"

    phone = CONSULTANT_PHONE if (size in ("micro", "small") and random.random() < 0.08) else \
        f"9{random.randint(100000000, 999999999)}"
    email_dom = "gmail.example" if random.random() < 0.35 else \
        f"{vid.lower().replace('-', '')}.example"
    email = CONSULTANT_EMAIL if (phone == CONSULTANT_PHONE and random.random() < 0.6) else \
        f"contact@{email_dom}"
    bank = INTERMEDIARY_ACCT if random.random() < 0.05 else f"ACCT-{random.randint(100000, 999999)}"

    pan = f"AA{random.choice('ABCDEFGH')}PS{random.randint(1000, 9999)}{random.choice('ABCDEFGHJK')}"
    v = {
        "vendor_id": vid,
        "legal_name": vendor_name(),
        "pan": pan,
        "gstin": f"{random.randint(10, 37)}{pan}1Z{random.randint(0, 9)}",
        "home_state": state,
        "categories": cats,
        "incorporation_date": inc.isoformat(),
        "paid_up_capital": money(capital),
        "annual_turnover": money(turnover),
        "size_tier": size,
        "address_building_id": building,
        "address_unit_id": unit,
        "phone": phone,
        "email": email,
        "bank_account": bank,
        "has_website": int(random.random() < (0.25 if size in ("micro", "small") else 0.85)),
        "has_employee_records": int(random.random() < (0.55 if size == "micro" else 0.93)),
        "registry_status": "active",
        "msme_registered": int(size in ("micro", "small") and random.random() < 0.6),
        "startup_registered": int(inc > date(2022, 1, 1) and random.random() < 0.35),
        "is_govt_owned": 0,
        "is_spv": 0,
        "profile": profile,
        "activity_code": random.choice(cats),
        "struck_off_date": "",
    }
    vendors.append(v)

    n_dir = 1 if size == "micro" else random.choice([1, 2, 2, 3])
    for i in range(n_dir):
        pid = f"P-{len(persons_pool) + 1:04d}"
        persons_pool[pid] = person_name()
        people.append({"vendor_id": vid, "person_id": pid, "person_name": persons_pool[pid],
                       "role": "Director" if i == 0 else random.choice(["Director", "Partner", "Authorised Signatory"]),
                       "appointed_on": rnd_date(inc, TODAY).isoformat()})
    return v


N_VENDORS = 240
for i in range(1, N_VENDORS + 1):
    make_vendor(f"V-{i:04d}")

by_id = {v["vendor_id"]: v for v in vendors}
vendors_by_cat = {}
for v in vendors:
    for c in v["categories"]:
        vendors_by_cat.setdefault(c, []).append(v["vendor_id"])

# a few government-owned suppliers (S3 must exclude these)
for vid in random.sample(list(by_id), 4):
    by_id[vid]["is_govt_owned"] = 1
    by_id[vid]["legal_name"] = by_id[vid]["legal_name"].split()[0] + " Public Sector Corporation Ltd"

# --------------------------------------------------------------------------
# collusion structures
# --------------------------------------------------------------------------

def link_bank(members):
    acct = f"ACCT-{random.randint(100000, 999999)}"
    for m in members:
        by_id[m]["bank_account"] = acct


def link_director(members):
    pid = f"P-{len(persons_pool) + 1:04d}"
    persons_pool[pid] = person_name()
    for m in members:
        people.append({"vendor_id": m, "person_id": pid, "person_name": persons_pool[pid],
                       "role": "Director", "appointed_on": rnd_date(date(2019, 1, 1), date(2023, 1, 1)).isoformat()})


def link_address(members):
    b = f"ADDR-BLD-{random.randint(1000, 8999)}"
    u = f"{b}-U{random.randint(1, 20)}"
    for m in members:
        by_id[m]["address_building_id"] = b
        by_id[m]["address_unit_id"] = u


def link_phone(members):
    p = f"9{random.randint(100000000, 999999999)}"
    for m in members:
        by_id[m]["phone"] = p


def pick_group(cat, n, state=None):
    pool = [v for v in vendors_by_cat.get(cat, []) if by_id[v]["profile"] == "normal"
            and (state is None or by_id[v]["home_state"] == state)]
    if len(pool) < n:
        pool = [v for v in vendors_by_cat.get(cat, []) if by_id[v]["profile"] == "normal"]
    chosen = random.sample(pool, min(n, len(pool)))
    for c in chosen:
        by_id[c]["profile"] = "ring"
    return chosen


rings = []          # cover-bidding rings, with a designated winner
rotations = []      # take-turns groups
allocations = []    # territory-splitting groups

ring_specs = [
    ("road_resurfacing", "KA", 3, "bank"),
    ("water_pipeline", "MH", 4, "director"),
    ("medical_supplies", "RJ", 3, "phone"),
    ("building_construction", "KL", 3, "address"),
    ("laptops_it", "IN", 3, "bank"),
]
for cat, st, n, ltype in ring_specs:
    g = pick_group(cat, n, st)
    if len(g) < 3:
        continue
    {"bank": link_bank, "director": link_director,
     "address": link_address, "phone": link_phone}[ltype](g)
    if ltype == "bank" and len(g) > 2:
        link_director(g[1:])       # second, weaker link inside the ring
    rings.append({"cat": cat, "state": st, "members": g, "winner": g[0], "link": ltype})

for cat, st, n in [("solar_streetlight", "KA", 4), ("school_uniforms", "OD", 4), ("sanitation_services", "MH", 5)]:
    g = pick_group(cat, n, st)
    if len(g) >= 3:
        rotations.append({"cat": cat, "state": st, "members": g})

for cat, st, n in [("borewell_drilling", "RJ", 4), ("office_furniture", "KL", 3)]:
    g = pick_group(cat, n, st)
    if len(g) >= 3:
        allocations.append({"cat": cat, "state": st, "members": g})

# shells: cheap paper companies used as cover bidders, plus two that win
shells = []
for i in range(1, 11):
    vid = f"V-S{i:03d}"
    inc = rnd_date(date(2024, 6, 1), date(2026, 3, 1))
    v = make_vendor(vid, profile="shell", inc=inc)
    v["paid_up_capital"] = money(random.choice([100000, 100000, 250000, 500000]))
    v["annual_turnover"] = ""
    v["has_website"] = 0
    v["has_employee_records"] = 0
    v["email"] = f"contact@gmail.example"
    v["address_building_id"] = MASS_ADDRESS
    v["address_unit_id"] = f"{MASS_ADDRESS}-U{random.randint(1, 6)}"
    v["size_tier"] = "micro"
    v["msme_registered"] = 0
    v["startup_registered"] = 0
    shells.append(vid)
by_id = {v["vendor_id"]: v for v in vendors}

# attach 4 shells to rings (they bid as losers), 2 shells win contracts outright
for i, r in enumerate(rings[:4]):
    s = shells[i]
    r["members"].append(s)
    by_id[s]["categories"] = [r["cat"]]
    by_id[s]["home_state"] = r["state"]
    if r["link"] == "bank":
        by_id[s]["bank_account"] = by_id[r["members"][0]]["bank_account"]

shell_winners = shells[6:8]
for s in shell_winners:
    by_id[s]["profile"] = "shell_winner"

# honest young MSE / startup firms that look shell-like but are not
honest_young = []
for i in range(1, 9):
    vid = f"V-Y{i:03d}"
    v = make_vendor(vid, profile="young_honest", inc=rnd_date(date(2025, 1, 1), date(2026, 2, 1)))
    v["paid_up_capital"] = money(random.uniform(300000, 900000))
    v["msme_registered"] = 1
    v["startup_registered"] = 1
    v["has_employee_records"] = 1
    honest_young.append(vid)

# one declared SPV for a large project
spv_id = "V-SPV01"
spv = make_vendor(spv_id, profile="spv", inc=date(2025, 3, 10))
spv["is_spv"] = 1
spv["paid_up_capital"] = money(2500000)
spv["categories"] = ["building_construction"]

by_id = {v["vendor_id"]: v for v in vendors}
vendors_by_cat = {}
for v in vendors:
    for c in v["categories"]:
        vendors_by_cat.setdefault(c, []).append(v["vendor_id"])

# --------------------------------------------------------------------------
# buyer-level behaviour assignments
# --------------------------------------------------------------------------

random.shuffle(buyers)
favour_pairs = []      # (buyer_id, vendor_id)
for b in buyers[:5]:
    cat = random.choice(b["categories"])
    pool = vendors_by_cat.get(cat, [])
    if not pool:
        continue
    fav = random.choice(pool)
    favour_pairs.append((b["buyer_id"], fav))
    b["favourite"] = fav
    b["favourite_cat"] = cat

single_bid_heavy = {b["buyer_id"] for b in buyers[5:9]}
splitting_buyers = [b for b in buyers[9:13]]
proprietary_buyer = next((b for b in buyers if "Health" in b["department"]), buyers[0])
emergency_district = ("KL", "Palakkad", date(2025, 7, 10), date(2025, 9, 30))
framework_buyer = next((b for b in buyers if "e-Governance" in b["department"]), buyers[1])

# approval thresholds (time-versioned), used by module 4D
thresholds = [
    {"threshold_id": "T-WORKS-1", "kind": "works", "effective_from": "2022-01-01", "value": money(25 * L),
     "rule": "Open tender required above this value for works"},
    {"threshold_id": "T-WORKS-2", "kind": "works", "effective_from": "2025-04-01", "value": money(40 * L),
     "rule": "Open tender required above this value for works"},
    {"threshold_id": "T-GOODS-1", "kind": "goods", "effective_from": "2022-01-01", "value": money(10 * L),
     "rule": "Open tender required above this value for goods"},
    {"threshold_id": "T-SERV-1", "kind": "services", "effective_from": "2022-01-01", "value": money(15 * L),
     "rule": "Open tender required above this value for services"},
]


def threshold_for(kind, dt):
    best = None
    for t in thresholds:
        if t["kind"] == kind and date.fromisoformat(t["effective_from"]) <= dt:
            if best is None or t["effective_from"] > best["effective_from"]:
                best = t
    return best


# --------------------------------------------------------------------------
# tender generation
# --------------------------------------------------------------------------

tenders, bids, awards, contracts, amendments, items = [], [], [], [], [], []
tno = 0


def new_tid():
    global tno
    tno += 1
    return f"T-{tno:04d}"


def bidders_for(cat, state, n, exclude=()):
    pool = [v for v in vendors_by_cat.get(cat, []) if v not in exclude]
    local = [v for v in pool if by_id[v]["home_state"] == state]
    picks = []
    random.shuffle(local)
    picks += local[:n]
    if len(picks) < n:
        rest = [v for v in pool if v not in picks]
        random.shuffle(rest)
        picks += rest[: n - len(picks)]
    return picks[:n]


def emit_tender(buyer, cat, pub_date, scenario, forced_bidders=None, forced_winner=None,
                qty=None, est_mult=None, method="open", n_bids=None, ctx=None):
    label, unit, scope_unit, base_rate, kind, qty_range = CATEGORIES[cat]
    tid = new_tid()
    ocid = f"ocds-pgraph-{tid}"
    qty = qty or random.randint(*qty_range)
    idx = price_index(pub_date)
    fair_rate = base_rate * idx * buyer["region_factor"] * random.uniform(0.94, 1.08)
    fair_value = fair_rate * qty

    # buyer's own estimate
    est_mult = est_mult if est_mult is not None else random.uniform(0.98, 1.12)
    estimate = fair_value * est_mult

    ctx = ctx or {}
    emergency = ctx.get("emergency", 0)
    proprietary = ctx.get("proprietary", 0)
    framework = ctx.get("framework", "")
    reserved = ctx.get("reserved", 0)

    if emergency:
        fair_value *= 1.38
        estimate *= 1.30

    # --- decide bidders and bid values -----------------------------------
    disq = {}
    if forced_bidders:
        bidders = list(forced_bidders)
    else:
        n = n_bids or random.choices([1, 2, 3, 4, 5, 6, 7], [0.07, 0.10, 0.19, 0.24, 0.19, 0.13, 0.08])[0]
        bidders = bidders_for(cat, buyer["state_code"], n)
    if not bidders:
        return None

    values = {}
    if scenario == "cover":
        winner = forced_winner or bidders[0]
        w_val = estimate * random.uniform(1.06, 1.18)
        values[winner] = w_val
        cover_base = w_val * random.uniform(1.14, 1.22)
        tight = random.uniform(0.0015, 0.006)
        for b in bidders:
            if b == winner:
                continue
            values[b] = cover_base * (1 + random.uniform(-tight, tight))
    elif scenario == "fixed_markup":
        winner = forced_winner or bidders[0]
        w_val = estimate * random.uniform(1.02, 1.10)
        values[winner] = w_val
        steps = [0.05, 0.10, 0.15, 0.20]
        for i, b in enumerate([x for x in bidders if x != winner]):
            values[b] = w_val * (1 + steps[i % len(steps)])
    elif scenario == "rotation" or scenario == "allocation":
        winner = forced_winner or bidders[0]
        values[winner] = fair_value * random.uniform(1.00, 1.12)
        for b in bidders:
            if b == winner:
                continue
            values[b] = values[winner] * random.uniform(1.03, 1.16)
    elif scenario == "single":
        winner = bidders[0]
        values[winner] = estimate * random.uniform(0.98, 1.12)
    else:  # competitive
        for b in bidders:
            values[b] = fair_value * random.uniform(0.86, 1.14)
        if emergency:
            for b in bidders:
                values[b] *= random.uniform(0.98, 1.06)
        winner = min(values, key=values.get)

    if forced_winner and forced_winner in values and scenario == "competitive":
        winner = forced_winner
        values[winner] = min(values.values()) * random.uniform(0.94, 0.99)

    # trivial disqualification of a perpetual loser inside rings
    if scenario == "cover" and random.random() < 0.28:
        cand = [b for b in bidders if b != winner]
        if cand:
            disq[random.choice(cand)] = random.choice(DISQ_REASONS[:2])

    # data-quality variations
    hide_estimate = random.random() < 0.10
    only_award = random.random() < 0.05 and len(bidders) > 2
    itemised = kind == "goods" and random.random() < 0.7

    award_value = values[winner]
    aid = f"A-{tid[2:]}"
    period_days = random.choice([7, 10, 14, 14, 21, 21, 30])
    if buyer["buyer_id"] in single_bid_heavy and random.random() < 0.5:
        period_days = random.choice([5, 6, 7])

    tenders.append({
        "ocid": ocid,
        "tender_id": tid,
        "lot": 1,
        "buyer_id": buyer["buyer_id"],
        "title": f"{label} - {buyer['district']} ({pub_date.strftime('%b %Y')})",
        "category_code": cat,
        "category_label": label,
        "procurement_kind": kind,
        "procurement_method": method,
        "publish_date": pub_date.isoformat(),
        "tender_period_days": period_days,
        "estimate_value": "" if hide_estimate else money(estimate),
        "quantity": qty,
        "unit": unit,
        "scope_metric_value": qty,
        "scope_metric_unit": scope_unit,
        "n_bids_published": 0 if only_award else len(bidders),
        "bids_published": 0 if only_award else 1,
        "status": "complete",
        "award_id": aid,
        "awarded_vendor_id": winner,
        "awarded_value": money(award_value),
        "award_date": (pub_date + timedelta(days=period_days + random.randint(10, 45))).isoformat(),
        "emergency_declared": emergency,
        "proprietary_certificate": proprietary,
        "framework_ref": framework,
        "reserved_for_mse": reserved,
        "justification_note": ctx.get("note", ""),
        "scenario_truth": scenario,
    })

    if not only_award:
        for b in bidders:
            sub = pub_date + timedelta(days=period_days, hours=0)
            minute = random.randint(0, 660)
            if scenario in ("cover", "fixed_markup") and b != winner and random.random() < 0.5:
                minute = 420 + random.randint(0, 3)   # near-simultaneous submissions
            bids.append({
                "ocid": ocid,
                "tender_id": tid,
                "vendor_id": b,
                "bid_value": money(values[b]),
                "submitted_at": f"{sub.isoformat()}T{minute // 60 + 8:02d}:{minute % 60:02d}:00",
                "status": "disqualified" if b in disq else "valid",
                "disqualification_reason": disq.get(b, ""),
                "is_winner": int(b == winner),
            })

    awards.append({"award_id": aid, "ocid": ocid, "tender_id": tid, "vendor_id": winner,
                   "value": money(award_value),
                   "award_date": (pub_date + timedelta(days=period_days + 20)).isoformat()})

    # contract + amendments
    sign = pub_date + timedelta(days=period_days + random.randint(25, 60))
    dur = random.choice([90, 120, 180, 240, 365])
    cid = f"C-{tid[2:]}"
    final = award_value
    growth_scenario = random.random()
    if scenario in ("cover", "rotation") and growth_scenario < 0.45:
        growth = random.uniform(1.15, 1.42)
    elif growth_scenario < 0.16:
        growth = random.uniform(1.05, 1.18)
    else:
        growth = 1.0
    if growth > 1.0:
        n_am = random.choice([1, 1, 2, 3])
        remaining = growth - 1.0
        val = award_value
        for j in range(n_am):
            share = remaining / n_am
            val = val * (1 + share)
            reason = random.choice(AMEND_REASONS)
            offset = int(dur * (0.12 if scenario == "cover" else random.uniform(0.2, 0.8))) + j * 30
            amendments.append({
                "amendment_id": f"{cid}-AM{j + 1}",
                "contract_id": cid,
                "tender_id": tid,
                "date": (sign + timedelta(days=offset)).isoformat(),
                "previous_value": money(final if j else award_value),
                "new_value": money(val),
                "reason_code": reason,
                "approval_reference": f"APR/{random.randint(1000, 9999)}" if reason == "scope_change_approved" else "",
                "description": reason.replace("_", " ").capitalize(),
            })
            final = val
    contracts.append({"contract_id": cid, "award_id": aid, "tender_id": tid, "vendor_id": winner,
                      "original_value": money(award_value), "final_value": money(final),
                      "sign_date": sign.isoformat(), "duration_days": dur})

    if itemised:
        items.append({"tender_id": tid, "item_id": f"{tid}-I1", "description": label,
                      "category_code": cat, "quantity": qty, "unit": unit,
                      "unit_price_awarded": money(award_value / qty)})
    return tid


# ---- 1. background of ordinary procurement --------------------------------
for b in buyers:
    n_t = random.randint(14, 30)
    for _ in range(n_t):
        cat = random.choice(b["categories"])
        pub = rnd_date(START, TODAY - timedelta(days=60))
        scenario = "competitive"
        n_bids = None
        method = "open"
        if b["buyer_id"] in single_bid_heavy and random.random() < 0.42:
            scenario, n_bids = "single", 1
            method = random.choice(["limited", "direct", "open"])
        elif random.random() < 0.06:
            scenario, n_bids = "single", 1
        fav = b.get("favourite")
        forced_winner = None
        if fav and cat == b.get("favourite_cat") and random.random() < 0.72:
            forced_winner = fav
            fb = bidders_for(cat, b["state_code"], max(2, random.randint(2, 4)), exclude=(fav,))
            emit_tender(b, cat, pub, "competitive", forced_bidders=[fav] + fb,
                        forced_winner=fav, method=method)
            continue
        emit_tender(b, cat, pub, scenario, n_bids=n_bids, method=method)

# ---- 2. cover-bidding rings ----------------------------------------------
for r in rings:
    host_buyers = [b for b in buyers if r["cat"] in b["categories"] and b["state_code"] == r["state"]]
    if not host_buyers:
        host_buyers = [b for b in buyers if r["cat"] in b["categories"]]
    host = random.choice(host_buyers)
    r["buyer_id"] = host["buyer_id"]
    for i in range(random.randint(8, 12)):
        pub = rnd_date(date(2023, 6, 1), TODAY - timedelta(days=90))
        members = list(r["members"])
        random.shuffle(members)
        take = members[:random.randint(3, min(4, len(members)))]
        if r["winner"] not in take:
            take[0] = r["winner"]
        scen = "fixed_markup" if random.random() < 0.25 else "cover"
        emit_tender(host, r["cat"], pub, scen, forced_bidders=take, forced_winner=r["winner"])

# ---- 3. rotation groups ---------------------------------------------------
for g in rotations:
    hosts = [b for b in buyers if g["cat"] in b["categories"]]
    if not hosts:
        continue
    order = list(g["members"])
    for i in range(random.randint(10, 14)):
        host = random.choice(hosts)
        pub = rnd_date(date(2023, 3, 1), TODAY - timedelta(days=60))
        winner = order[i % len(order)]
        take = list(g["members"])
        random.shuffle(take)
        emit_tender(host, g["cat"], pub, "rotation", forced_bidders=take, forced_winner=winner)

# ---- 4. market allocation groups -----------------------------------------
for g in allocations:
    hosts = [b for b in buyers if g["cat"] in b["categories"]]
    if len(hosts) < 2:
        continue
    home = {m: hosts[i % len(hosts)]["buyer_id"] for i, m in enumerate(g["members"])}
    for i in range(random.randint(12, 18)):
        host = random.choice(hosts)
        pub = rnd_date(date(2023, 3, 1), TODAY - timedelta(days=60))
        locals_ = [m for m in g["members"] if home[m] == host["buyer_id"]]
        winner = locals_[0] if locals_ else random.choice(g["members"])
        take = list(g["members"])
        random.shuffle(take)
        emit_tender(host, g["cat"], pub, "allocation", forced_bidders=take, forced_winner=winner)

# ---- 5. shell winners -----------------------------------------------------
for s in shell_winners:
    cat = by_id[s]["categories"][0]
    hosts = [b for b in buyers if cat in b["categories"]]
    if not hosts:
        continue
    host = random.choice(hosts)
    inc = date.fromisoformat(by_id[s]["incorporation_date"])
    for i in range(random.randint(2, 4)):
        pub = rnd_date(inc + timedelta(days=20), min(TODAY - timedelta(days=60), inc + timedelta(days=420)))
        others = bidders_for(cat, host["state_code"], 2, exclude=(s,))
        emit_tender(host, cat, pub, "competitive", forced_bidders=[s] + others, forced_winner=s)
    by_id[s]["registry_status"] = random.choice(["struck_off", "dormant"])
    by_id[s]["struck_off_date"] = (TODAY - timedelta(days=random.randint(30, 200))).isoformat()

# ---- 6. honest young MSE winners (false-positive pressure) ----------------
for y in honest_young:
    cat = by_id[y]["categories"][0]
    hosts = [b for b in buyers if cat in b["categories"]]
    if not hosts:
        continue
    host = random.choice(hosts)
    for i in range(random.randint(1, 3)):
        pub = rnd_date(date(2025, 6, 1), TODAY - timedelta(days=45))
        others = bidders_for(cat, host["state_code"], 3, exclude=(y,))
        emit_tender(host, cat, pub, "competitive", forced_bidders=[y] + others,
                    forced_winner=y, qty=random.randint(50, 300),
                    ctx={"reserved": 1, "note": "Reserved for micro and small enterprises"})

# ---- 7. SPV (declared) ----------------------------------------------------
spv_host = next(b for b in buyers if "building_construction" in b["categories"])
for i in range(2):
    pub = rnd_date(date(2025, 5, 1), TODAY - timedelta(days=60))
    others = bidders_for("building_construction", spv_host["state_code"], 3, exclude=(spv_id,))
    emit_tender(spv_host, "building_construction", pub, "competitive",
                forced_bidders=[spv_id] + others, forced_winner=spv_id,
                ctx={"note": "Declared special purpose vehicle for the project"})
by_id[spv_id]["is_spv"] = 1

# ---- 8. threshold splitting ----------------------------------------------
for b in splitting_buyers:
    cat = random.choice([c for c in b["categories"]])
    kind = CATEGORIES[cat][4]
    base = rnd_date(date(2024, 1, 1), TODAY - timedelta(days=200))
    T = threshold_for(kind, base)["value"]
    pool = vendors_by_cat.get(cat, [])
    if not pool:
        continue
    vend = random.choice(pool)
    rate = CATEGORIES[cat][3] * price_index(base) * b["region_factor"]
    for i in range(random.randint(3, 5)):
        target = T * random.uniform(0.84, 0.97)
        qty = max(1, int(target / rate))
        emit_tender(b, cat, base + timedelta(days=i * random.randint(8, 18)), "single",
                    forced_bidders=[vend], forced_winner=vend, qty=qty,
                    method="limited", n_bids=1)

# ---- 9. documented context: proprietary / OEM ----------------------------
prop_cat = "biomedical_service"
prop_vendor = random.choice(vendors_by_cat.get(prop_cat, list(by_id))[:5])
for i in range(5):
    pub = date(2022, 11, 1) + timedelta(days=i * 350)
    if pub > TODAY - timedelta(days=45):
        break
    emit_tender(proprietary_buyer, prop_cat, pub, "single",
                forced_bidders=[prop_vendor], forced_winner=prop_vendor,
                method="direct", est_mult=1.0,
                ctx={"proprietary": 1, "note": "Proprietary article certificate on record (OEM-authorised service)"})

# ---- 10. declared emergency window ---------------------------------------
em_state, em_district, em_from, em_to = emergency_district
em_buyers = [b for b in buyers if b["state_code"] == em_state and
             ("Health" in b["department"] or "Urban" in b["department"])]
for b in em_buyers or buyers[:2]:
    for i in range(random.randint(3, 5)):
        pub = rnd_date(em_from, em_to)
        cat = random.choice([c for c in b["categories"] if CATEGORIES[c][4] != "services"] or b["categories"])
        emit_tender(b, cat, pub, "competitive", n_bids=random.choice([1, 2, 2, 3]),
                    method="limited",
                    ctx={"emergency": 1, "note": f"Declared flood emergency, {em_district}, Jul-Sep 2025"})

# ---- 11. framework call-offs ---------------------------------------------
fw_ref = "FW-ITHW-2024/07"
fw_vendor = random.choice(vendors_by_cat.get("laptops_it", list(by_id)))
for i in range(6):
    pub = rnd_date(date(2024, 8, 1), TODAY - timedelta(days=45))
    emit_tender(framework_buyer, "laptops_it", pub, "single",
                forced_bidders=[fw_vendor], forced_winner=fw_vendor,
                method="framework_call_off", n_bids=1,
                ctx={"framework": fw_ref, "note": "Call-off against rate framework FW-ITHW-2024/07"})

# ---- 12. a few overpriced-but-unlinked outliers ---------------------------
for _ in range(14):
    b = random.choice(buyers)
    cat = random.choice(b["categories"])
    pub = rnd_date(date(2023, 6, 1), TODAY - timedelta(days=60))
    tid = emit_tender(b, cat, pub, "competitive", n_bids=random.choice([2, 3, 4]))
    if tid:
        t = tenders[-1]
        mult = random.uniform(1.45, 2.1)
        t["awarded_value"] = money(float(t["awarded_value"]) * mult)
        awards[-1]["value"] = t["awarded_value"]
        contracts[-1]["original_value"] = t["awarded_value"]
        contracts[-1]["final_value"] = money(float(contracts[-1]["final_value"]) * mult)
        for bd in bids:
            if bd["tender_id"] == tid and bd["is_winner"]:
                bd["bid_value"] = t["awarded_value"]
        for it in items:
            if it["tender_id"] == tid:
                it["unit_price_awarded"] = money(float(t["awarded_value"]) / max(1, it["quantity"]))

# --------------------------------------------------------------------------
# derived vendor facts + write CSVs
# --------------------------------------------------------------------------

addr_counts = {}
for v in vendors:
    addr_counts[v["address_unit_id"]] = addr_counts.get(v["address_unit_id"], 0) + 1
bld_counts = {}
for v in vendors:
    bld_counts[v["address_building_id"]] = bld_counts.get(v["address_building_id"], 0) + 1
# the mass-registration building hosts far more entities than our vendor table shows
bld_counts[MASS_ADDRESS] = bld_counts.get(MASS_ADDRESS, 0) + 31

dir_counts = {}
for p in people:
    dir_counts[p["person_id"]] = dir_counts.get(p["person_id"], 0) + 1

for v in vendors:
    v["address_unit_share_count"] = addr_counts[v["address_unit_id"]]
    v["address_building_share_count"] = bld_counts[v["address_building_id"]]
    v["categories"] = "|".join(v["categories"])
    v.pop("profile", None)


def write(name, rows, fields=None):
    path = os.path.join(OUT, name)
    if not rows:
        return
    fields = fields or list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"{name:26s} {len(rows):6d} rows")


for b in buyers:
    b["categories"] = "|".join(b["categories"])
    b["region_factor"] = round(b["region_factor"], 4)
    b.pop("favourite", None)
    b.pop("favourite_cat", None)

write("buyers.csv", buyers, ["buyer_id", "buyer_name", "government_level", "government_name",
                             "state_code", "department", "district", "region_factor", "categories"])
write("vendors.csv", vendors, ["vendor_id", "legal_name", "pan", "gstin", "home_state", "categories",
                               "incorporation_date", "paid_up_capital", "annual_turnover", "size_tier",
                               "address_building_id", "address_unit_id", "address_unit_share_count",
                               "address_building_share_count", "phone", "email", "bank_account",
                               "has_website", "has_employee_records", "registry_status", "struck_off_date",
                               "msme_registered", "startup_registered", "is_govt_owned", "is_spv",
                               "activity_code"])
write("vendor_persons.csv", people)
write("tenders.csv", tenders)
write("bids.csv", bids)
write("awards.csv", awards)
write("contracts.csv", contracts)
write("amendments.csv", amendments)
write("tender_items.csv", items)
write("thresholds.csv", thresholds)

suppression = [
    {"entry_id": "SUP-001", "kind": "bank_account", "value": INTERMEDIARY_ACCT,
     "reason": "Registered invoice-discounting intermediary; many unrelated vendors route receipts here",
     "added_by": "audit.admin", "added_on": "2026-01-12", "expires_on": "2027-01-12"},
    {"entry_id": "SUP-002", "kind": "address_building", "value": MASS_ADDRESS,
     "reason": "Commercial business centre hosting hundreds of registrations",
     "added_by": "audit.admin", "added_on": "2026-02-02", "expires_on": "2027-02-02"},
    {"entry_id": "SUP-003", "kind": "email", "value": CONSULTANT_EMAIL,
     "reason": "Filing consultant used by many small firms",
     "added_by": "audit.admin", "added_on": "2026-02-02", "expires_on": "2027-02-02"},
    {"entry_id": "SUP-004", "kind": "phone", "value": CONSULTANT_PHONE,
     "reason": "Filing consultant contact number",
     "added_by": "audit.admin", "added_on": "2026-02-02", "expires_on": "2027-02-02"},
]
write("suppression_list.csv", suppression)

print("\nbuyers", len(buyers), "vendors", len(vendors), "tenders", len(tenders), "bids", len(bids))
print("rings", len(rings), "rotations", len(rotations), "allocations", len(allocations),
      "shells", len(shells))

"""
ProcureGraph analysis pipeline: subsystems S1-S7.

Reads the SQLite database produced by load_sqlite.py, runs the five detectors,
the market-context layer and the fusion layer, and writes the results back into
the same database as analysis tables the web app reads.

Everything is deterministic: the same database in gives the same scores out.
"""

import json
import math
import os
import random
import sqlite3
from collections import defaultdict

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "procuregraph.db")
RULES_VERSION = "rules-2026.09.1"

random.seed(7)
np.random.seed(7)


# --------------------------------------------------------------------------
# building blocks (section 1.6 of the design document)
# --------------------------------------------------------------------------

def ramp(v, start, full):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return 0.0
    if full == start:
        return 1.0 if v >= full else 0.0
    return float(min(1.0, max(0.0, (v - start) / (full - start))))


def noisy_or(pairs):
    """pairs: iterable of (weight, evidence). Returns 1 - prod(1 - w*e)."""
    acc = 1.0
    for w, e in pairs:
        acc *= (1.0 - max(0.0, min(1.0, w * e)))
    return 1.0 - acc


def robust_z(x, values):
    vals = np.asarray([v for v in values if v is not None and not math.isnan(v)], dtype=float)
    if len(vals) < 5:
        return None
    med = float(np.median(vals))
    mad = float(np.median(np.abs(vals - med)))
    if mad <= 1e-12:
        mad = float(np.std(vals)) or 1e-9
        return (x - med) / (mad * 1.0)
    return (x - med) / (1.4826 * mad)


def percentile_of(x, values, high_is_suspicious=True):
    vals = np.asarray([v for v in values if v is not None and not math.isnan(v)], dtype=float)
    if len(vals) == 0:
        return None
    if high_is_suspicious:
        return float(np.mean(vals <= x))
    return float(np.mean(vals >= x))


def smooth_rate(wins, bids, alpha=1.0, beta=4.0):
    return (wins + alpha) / (bids + alpha + beta)


def rate_bounds(wins, bids, alpha=1.0, beta=4.0, z=1.645):
    """Normal approximation to the smoothed Beta posterior; conservative enough
    for triage and easy to explain to an investigator."""
    n = bids + alpha + beta
    p = (wins + alpha) / n
    se = math.sqrt(max(p * (1 - p), 1e-9) / n)
    return max(0.0, p - z * se), min(1.0, p + z * se)


def inr(x):
    """Format rupees as lakh/crore text."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    x = float(x)
    if x >= 1e7:
        return f"Rs {x / 1e7:.2f} cr"
    return f"Rs {x / 1e5:.2f} lakh"


def pct(x, nd=1):
    return f"{100 * x:.{nd}f}%"


# --------------------------------------------------------------------------
# load
# --------------------------------------------------------------------------

con = sqlite3.connect(DB)
tenders = pd.read_sql("SELECT * FROM tenders", con)
bids = pd.read_sql("SELECT * FROM bids", con)
vendors = pd.read_sql("SELECT * FROM vendors", con)
persons = pd.read_sql("SELECT * FROM vendor_persons", con)
buyers = pd.read_sql("SELECT * FROM buyers", con)
contracts = pd.read_sql("SELECT * FROM contracts", con)
amendments = pd.read_sql("SELECT * FROM amendments", con)
items = pd.read_sql("SELECT * FROM tender_items", con)
thresholds = pd.read_sql("SELECT * FROM thresholds", con)
suppression = pd.read_sql("SELECT * FROM suppression_list", con)

tenders["publish_date"] = pd.to_datetime(tenders["publish_date"])
tenders["award_date"] = pd.to_datetime(tenders["award_date"])
tenders["estimate_value"] = pd.to_numeric(tenders["estimate_value"], errors="coerce")
tenders["awarded_value"] = pd.to_numeric(tenders["awarded_value"], errors="coerce")
tenders["year"] = tenders["publish_date"].dt.year

buyer_by_id = buyers.set_index("buyer_id").to_dict("index")
vendor_by_id = vendors.set_index("vendor_id").to_dict("index")
tender_by_id = tenders.set_index("tender_id").to_dict("index")
contract_by_tender = contracts.set_index("tender_id").to_dict("index")

bids_by_tender = defaultdict(list)
for r in bids.to_dict("records"):
    bids_by_tender[r["tender_id"]].append(r)

tenders_by_vendor = defaultdict(list)
for r in bids.to_dict("records"):
    tenders_by_vendor[r["vendor_id"]].append(r["tender_id"])

suppressed = set(suppression["value"].tolist())

print(f"loaded {len(tenders)} tenders, {len(bids)} bids, {len(vendors)} vendors")

# precomputed lookups: these are read inside per-tender and per-vendor loops, and
# rebuilding them from the DataFrames each time dominated the runtime
vendors_with_people = set(persons["vendor_id"])
item_tenders = set(items["tender_id"])
awards_by_vendor = defaultdict(list)
for _r in tenders.to_dict("records"):
    awards_by_vendor[_r["awarded_vendor_id"]].append(_r)



# ==========================================================================
# S6 (part 1) - peer groups and baselines
# ==========================================================================

def size_band(value):
    if value is None or math.isnan(value) or value <= 0:
        return "unknown"
    return str(int(math.floor(math.log10(value))))


tenders["size_band"] = tenders["awarded_value"].map(size_band)
tenders["state_code"] = tenders["buyer_id"].map(lambda b: buyer_by_id[b]["state_code"])
tenders["department"] = tenders["buyer_id"].map(lambda b: buyer_by_id[b]["department"])
tenders["district"] = tenders["buyer_id"].map(lambda b: buyer_by_id[b]["district"])
tenders["region_factor"] = tenders["buyer_id"].map(lambda b: buyer_by_id[b]["region_factor"])

T = tenders.set_index("tender_id")

LADDER = [
    ("category + state + size band + 24 months", 0),
    ("size band dropped", 1),
    ("window widened to 48 months", 2),
    ("region widened to national", 3),
    ("all periods, all regions", 4),
]
MIN_PEERS, HARD_FLOOR = 30, 10


def peer_index(tid):
    """Walk the backoff ladder until at least 30 comparable tenders are found."""
    row = T.loc[tid]
    cat = row["category_code"]
    same_cat = tenders[(tenders["category_code"] == cat) & (tenders["tender_id"] != tid)]
    pub = row["publish_date"]

    def window(df, months):
        lo = pub - pd.Timedelta(days=30 * months)
        hi = pub + pd.Timedelta(days=30 * months)
        return df[(df["publish_date"] >= lo) & (df["publish_date"] <= hi)]

    steps = []
    s0 = window(same_cat[(same_cat["state_code"] == row["state_code"]) &
                         (same_cat["size_band"] == row["size_band"])], 24)
    steps.append(s0)
    s1 = window(same_cat[same_cat["state_code"] == row["state_code"]], 24)
    steps.append(s1)
    s2 = window(same_cat[same_cat["state_code"] == row["state_code"]], 48)
    steps.append(s2)
    s3 = window(same_cat, 48)
    steps.append(s3)
    steps.append(same_cat)

    for level, df in enumerate(steps):
        if len(df) >= MIN_PEERS:
            return df, level, len(df)
    last = steps[-1]
    return last, len(steps) - 1, len(last)


# per-tender bid screens, computed once
screen_rows = {}
for tid, grp in bids.groupby("tender_id", sort=False):
    valid = grp[grp["status"] == "valid"]
    vals = np.sort(valid["bid_value"].to_numpy(dtype=float))
    rec = {"n_valid": len(vals), "n_all": len(grp)}
    if len(vals) >= 2:
        losers = vals[1:]
        mu = float(np.mean(losers))
        sd = float(np.std(losers, ddof=1)) if len(losers) > 1 else 0.0
        rec["gap_pct"] = float((vals[1] - vals[0]) / vals[0])
        rec["cv_losers"] = (sd / mu) if (len(losers) > 1 and mu > 0) else None
        if len(vals) >= 3:
            floor_sd = 0.001 * mu
            rec["rd"] = float((vals[1] - vals[0]) / max(sd, floor_sd))
        else:
            rec["rd"] = None
    screen_rows[tid] = rec

screens = pd.DataFrame.from_dict(screen_rows, orient="index")
screens.index.name = "tender_id"

# unit price model per category: ln(unit price) ~ ln(qty) + region + year
price_resid = {}
for cat, grp in tenders.groupby("category_code", sort=False):
    g = grp[(grp["awarded_value"] > 0) & (grp["quantity"] > 0)].copy()
    if len(g) < 12:
        continue
    y = np.log(g["awarded_value"].to_numpy(float) / g["quantity"].to_numpy(float))
    X = np.column_stack([
        np.ones(len(g)),
        np.log(g["quantity"].to_numpy(float)),
        g["region_factor"].to_numpy(float),
        (g["year"].to_numpy(float) - 2022.0),
    ])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    for tid, r in zip(g["tender_id"], resid):
        price_resid[tid] = float(r)

# same model applied to the buyer's estimate, for signal 4B.3
est_resid = {}
for cat, grp in tenders.groupby("category_code", sort=False):
    g = grp[(grp["estimate_value"] > 0) & (grp["quantity"] > 0)].copy()
    if len(g) < 12:
        continue
    y = np.log(g["estimate_value"].to_numpy(float) / g["quantity"].to_numpy(float))
    X = np.column_stack([
        np.ones(len(g)),
        np.log(g["quantity"].to_numpy(float)),
        g["region_factor"].to_numpy(float),
        (g["year"].to_numpy(float) - 2022.0),
    ])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    for tid, r in zip(g["tender_id"], resid):
        est_resid[tid] = float(r)

# amendment growth per contract
growth = {}
first_amend_frac = {}
for tid, c in contract_by_tender.items():
    orig, fin = float(c["original_value"]), float(c["final_value"])
    growth[tid] = fin / orig if orig > 0 else 1.0
am_by_tender = defaultdict(list)
for a in amendments.to_dict("records"):
    am_by_tender[a["tender_id"]].append(a)
for tid, ams in am_by_tender.items():
    c = contract_by_tender.get(tid)
    if not c:
        continue
    sign = pd.Timestamp(c["sign_date"])
    first = min(pd.Timestamp(a["date"]) for a in ams)
    dur = max(1, int(c["duration_days"]))
    first_amend_frac[tid] = (first - sign).days / dur

print("computed screens, price residuals and amendment growth")


# ==========================================================================
# S1 - relationship graph and linked-vendor detector
# ==========================================================================

ATTR_SPEC = {
    "bank_account": ("Bank account", 0.95, 0.80),
    "director": ("Shared director or partner", 0.90, 0.60),
    "phone": ("Phone number", 0.70, 0.05),
    "email": ("Email address", 0.70, 0.05),
    "address_unit": ("Registered address (unit level)", 0.50, 0.05),
    "address_building": ("Registered address (building only)", 0.20, 0.05),
}

attr_to_vendors = defaultdict(set)
for v in vendors.to_dict("records"):
    vid = v["vendor_id"]
    if v["bank_account"] and v["bank_account"] not in suppressed:
        attr_to_vendors[("bank_account", v["bank_account"])].add(vid)
    if v["phone"] and str(v["phone"]) not in suppressed:
        attr_to_vendors[("phone", str(v["phone"]))].add(vid)
    if v["email"] and v["email"] not in suppressed and "gmail" not in str(v["email"]):
        attr_to_vendors[("email", v["email"])].add(vid)
    if v["address_unit_id"]:
        attr_to_vendors[("address_unit", v["address_unit_id"])].add(vid)
    if v["address_building_id"] and v["address_building_id"] not in suppressed:
        attr_to_vendors[("address_building", v["address_building_id"])].add(vid)
for p in persons.to_dict("records"):
    attr_to_vendors[("director", f"{p['person_id']}|{p['person_name']}")].add(p["vendor_id"])

# pairwise link strengths, only for vendors that actually share something
pair_attrs = defaultdict(list)
for (kind, value), vset in attr_to_vendors.items():
    if len(vset) < 2 or len(vset) > 60:
        continue
    label, w_type, floor = ATTR_SPEC[kind]
    n = len(vset)
    rarity = max(floor, min(1.0, 3.0 / n))
    eff = w_type * rarity
    vl = sorted(vset)
    for i in range(len(vl)):
        for j in range(i + 1, len(vl)):
            pair_attrs[(vl[i], vl[j])].append({
                "kind": kind, "label": label, "value": value, "shared_by": n,
                "w_type": w_type, "rarity": round(rarity, 3), "eff": round(eff, 4),
            })

link_strength = {}
for pair, attrs in pair_attrs.items():
    best_per_kind = {}
    for a in attrs:
        if a["eff"] > best_per_kind.get(a["kind"], {"eff": -1})["eff"]:
            best_per_kind[a["kind"]] = a
    strength = noisy_or([(a["eff"], 1.0) for a in best_per_kind.values()])
    link_strength[pair] = {"strength": strength, "attrs": list(best_per_kind.values())}

# clusters: connected components over links >= 0.5
parent = {}


def find(x):
    parent.setdefault(x, x)
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def union(a, b):
    ra, rb = find(a), find(b)
    if ra != rb:
        parent[ra] = rb


for (a, b), info in link_strength.items():
    if info["strength"] >= 0.5:
        union(a, b)

cluster_of = {}
for v in vendors["vendor_id"]:
    find(v)
for v in vendors["vendor_id"]:
    cluster_of[v] = find(v)
cluster_members = defaultdict(list)
for v, c in cluster_of.items():
    cluster_members[c].append(v)
# only clusters of 2+ count as relationship clusters
real_clusters = {c: m for c, m in cluster_members.items() if len(m) > 1}

# how often do members of one cluster bid in the same tender?
cluster_cobid = defaultdict(set)
for tid, grp in bids.groupby("tender_id", sort=False):
    vs = list(grp["vendor_id"])
    seen = defaultdict(int)
    for v in vs:
        seen[cluster_of.get(v, v)] += 1
    for c, n in seen.items():
        if n >= 2 and c in real_clusters:
            cluster_cobid[c].add(tid)

attr_coverage_fields = ["pan", "address_unit_id", "phone", "bank_account"]

s1_results = {}
tender_links = defaultdict(list)

for tid in tenders["tender_id"]:
    bl = bids_by_tender.get(tid, [])
    bidders = [b["vendor_id"] for b in bl]
    winner = T.loc[tid, "awarded_vendor_id"]
    if len(bidders) < 2:
        s1_results[tid] = {"status": "not_applicable" if bidders else "insufficient_data",
                           "score": 0.0, "confidence": 0.0, "signals": [],
                           "note": "Fewer than two bidders, so no vendor-to-vendor link can be scored."}
        continue

    best = None
    for i in range(len(bidders)):
        for j in range(i + 1, len(bidders)):
            a, b = sorted([bidders[i], bidders[j]])
            info = link_strength.get((a, b))
            if not info or info["strength"] < 0.15:
                continue
            tender_links[tid].append({"a": a, "b": b, "strength": round(info["strength"], 4),
                                      "attrs": info["attrs"]})
            if best is None or info["strength"] > best["strength"]:
                best = {"a": a, "b": b, "strength": info["strength"], "attrs": info["attrs"]}

    signals = []
    if best:
        omega = 1.0 if winner in (best["a"], best["b"]) else 0.8
        basis = ", ".join(f"{a['label'].lower()} (shared by {a['shared_by']} vendors)" for a in best["attrs"])
        signals.append({
            "signal_id": "S1.a", "family": "linked_bidders", "label": "Linked co-bidders",
            "observed": round(best["strength"], 3),
            "observed_text": f"link strength {best['strength']:.2f} between {vendor_by_id[best['a']]['legal_name']} and {vendor_by_id[best['b']]['legal_name']}",
            "baseline_text": "independent bidders share none of these attributes",
            "evidence": round(best["strength"], 4), "weight": omega,
            "explanation": (f"Two bidders in this tender are connected through {basis}."
                            + (" One of them won the tender." if omega == 1.0 else " Neither of them won.")),
        })
        cl = cluster_of.get(best["a"])
        k = len(cluster_cobid.get(cl, set()))
        if k >= 2:
            e_rep = ramp(k - 1, 0, 5)
            signals.append({
                "signal_id": "S1.b", "family": "repetition", "label": "Repeated co-bidding",
                "observed": k, "observed_text": f"{k} tenders",
                "baseline_text": "unrelated firms meet occasionally, not systematically",
                "evidence": round(e_rep, 4), "weight": 0.4,
                "explanation": f"Members of this connected group have bid in the same tender {k} times.",
            })

    score = noisy_or([(s["weight"], s["evidence"]) for s in signals])
    cov = []
    for v in bidders:
        vrec = vendor_by_id.get(v, {})
        have = sum(1 for f in attr_coverage_fields if str(vrec.get(f, "")).strip() not in ("", "nan"))
        have += 1 if v in vendors_with_people else 0
        cov.append(1.0 if have >= 2 else 0.5)
    conf = float(np.mean(cov)) * 0.9  # no bid-submission IP/device data in this dataset
    s1_results[tid] = {"status": "scored", "score": score, "confidence": conf, "signals": signals,
                       "cluster": cluster_of.get(bidders[0]) if best else None,
                       "note": "" if best else "No detectable link between the bidders. That is not the same as proven independence."}

print(f"S1 done: {sum(1 for r in s1_results.values() if r['score'] > 0.5)} tenders with a strong link")


# ==========================================================================
# S6 (part 2) - peer groups per tender, used by S2 and S4
# ==========================================================================

peer_info = {}
for tid in tenders["tender_id"]:
    df, level, n = peer_index(tid)
    peer_info[tid] = {"df": df, "level": level, "n": n,
                      "c_peer": round(0.85 ** level, 4),
                      "label": LADDER[min(level, len(LADDER) - 1)][0],
                      "sufficient": n >= HARD_FLOOR}

print("S6 peer groups built")


# ==========================================================================
# S2 - cover bidding detector
# ==========================================================================

# behavioural history: how often has vendor X lost to winner Y?
loss_pairs = defaultdict(int)
bid_counts = defaultdict(int)
win_counts = defaultdict(int)
disq_pairs = defaultdict(int)
for tid, grp in bids.groupby("tender_id", sort=False):
    w = grp[grp["is_winner"] == 1]
    if w.empty:
        continue
    winner = w.iloc[0]["vendor_id"]
    for r in grp.to_dict("records"):
        bid_counts[r["vendor_id"]] += 1
        if r["is_winner"]:
            win_counts[r["vendor_id"]] += 1
        else:
            loss_pairs[(r["vendor_id"], winner)] += 1
            if r["status"] == "disqualified":
                disq_pairs[(r["vendor_id"], winner)] += 1

win_rate = {v: smooth_rate(win_counts.get(v, 0), bid_counts.get(v, 0)) for v in bid_counts}
win_rate_values = list(win_rate.values())

s2_results = {}
for tid in tenders["tender_id"]:
    row = T.loc[tid]
    bl = bids_by_tender.get(tid, [])
    sc = screen_rows.get(tid, {"n_valid": 0})
    pi = peer_info[tid]

    if not bl:
        s2_results[tid] = {"status": "insufficient_data", "score": 0.0, "confidence": 0.0, "signals": [],
                           "note": "Only the award was published; there are no losing bids to analyse."}
        continue
    if sc["n_valid"] < 2:
        s2_results[tid] = {"status": "not_applicable", "score": 0.0, "confidence": 0.0, "signals": [],
                           "note": "Single valid bid, so no bid distribution exists. The lack of competition is analysed by S5."}
        continue

    families = defaultdict(list)
    peers = pi["df"]
    peer_ids = list(peers["tender_id"])

    if sc.get("rd") is not None:
        peer_rd = [screen_rows.get(p, {}).get("rd") for p in peer_ids]
        u = percentile_of(sc["rd"], peer_rd, True)
        if u is not None:
            p99 = float(np.nanpercentile([v for v in peer_rd if v is not None], 99)) if any(
                v is not None for v in peer_rd) else float("nan")
            families["bid_distribution"].append({
                "signal_id": "S2.a", "family": "bid_distribution", "label": "Relative distance",
                "observed": round(sc["rd"], 2),
                "observed_text": f"winner sits {sc['rd']:.1f} loser-spreads below the next bid",
                "baseline_text": f"99th percentile of {pi['n']} peer tenders is {p99:.1f}",
                "evidence": round(ramp(u, 0.90, 0.99), 4), "weight": 0.6,
                "explanation": (f"The jump from the winning bid to the next bid is {sc['rd']:.1f} times "
                                f"the spread among the losing bids; {pct(u)} of comparable tenders are less extreme."),
            })
    if sc.get("cv_losers") is not None:
        peer_cv = [screen_rows.get(p, {}).get("cv_losers") for p in peer_ids]
        u = percentile_of(sc["cv_losers"], peer_cv, False)
        if u is not None:
            families["bid_distribution"].append({
                "signal_id": "S2.b", "family": "bid_distribution", "label": "Tight losing bids",
                "observed": round(sc["cv_losers"], 5),
                "observed_text": f"losing bids differ by {pct(sc['cv_losers'], 2)} of their mean",
                "baseline_text": "typical peer spread is several percent",
                "evidence": round(ramp(u, 0.90, 0.99), 4), "weight": 0.4,
                "explanation": (f"The losing bids sit within {pct(sc['cv_losers'], 2)} of one another, "
                                f"tighter than {pct(u)} of comparable tenders."),
            })
    if sc["n_valid"] == 2 and sc.get("gap_pct") is not None:
        peer_gap = [screen_rows.get(p, {}).get("gap_pct") for p in peer_ids
                    if screen_rows.get(p, {}).get("n_valid") == 2]
        u = percentile_of(sc["gap_pct"], peer_gap, True)
        if u is not None:
            families["bid_distribution"].append({
                "signal_id": "S2.c", "family": "bid_distribution", "label": "Two-bid gap",
                "observed": round(sc["gap_pct"], 4),
                "observed_text": f"second bid is {pct(sc['gap_pct'])} above the winner",
                "baseline_text": "compared with other two-bid tenders in the peer group",
                "evidence": round(ramp(u, 0.90, 0.99), 4), "weight": 0.3,
                "explanation": f"With only two bids, the gap of {pct(sc['gap_pct'])} is wider than {pct(u)} of two-bid peers.",
            })

    est = row["estimate_value"]
    if not pd.isna(est) and est > 0:
        vals = sorted([b["bid_value"] for b in bl if b["status"] == "valid"])
        w_over = (vals[0] - est) / est
        losers_over = [(v - est) / est for v in vals[1:]]
        if losers_over and w_over <= 0.15 and min(losers_over) > 0.25:
            families["estimate_anchor"].append({
                "signal_id": "S2.d", "family": "estimate_anchor", "label": "Estimate-anchored pattern",
                "observed": round(w_over, 4),
                "observed_text": f"winner {pct(w_over)} above the estimate, every loser more than {pct(min(losers_over))} above",
                "baseline_text": "winner within +15% while all losers exceed +25%",
                "evidence": 1.0, "weight": 0.3,
                "explanation": ("The winning bid lands just above the buyer's confidential estimate while every "
                                "losing bid sits far above it, the shape expected when the winner is known in advance."),
            })

    # fixed round-percentage markups over the winner
    vals = sorted([b["bid_value"] for b in bl if b["status"] == "valid"])
    if len(vals) >= 3:
        markups = [(v - vals[0]) / vals[0] * 100 for v in vals[1:]]
        rounds = sum(1 for m in markups if abs(m - round(m / 5.0) * 5.0) < 0.1 and m > 1)
        if rounds >= 2:
            families["arithmetic_pattern"].append({
                "signal_id": "S2.e", "family": "arithmetic_pattern", "label": "Fixed-ratio markups",
                "observed": rounds,
                "observed_text": "losing bids at " + ", ".join(f"+{m:.1f}%" for m in markups),
                "baseline_text": "independent pricing rarely lands on round percentages",
                "evidence": round(ramp(rounds, 1, 3), 4), "weight": 0.5,
                "explanation": (f"{rounds} losing bids are exactly round percentages above the winning bid, "
                                "which is what a single spreadsheet produces."),
            })

    # perpetual losers
    winner = row["awarded_vendor_id"]
    worst = None
    for b in bl:
        v = b["vendor_id"]
        if v == winner:
            continue
        k = loss_pairs.get((v, winner), 0)
        if k >= 4:
            own_pct = percentile_of(win_rate.get(v, 0.2), win_rate_values, True) or 0.5
            e = ramp(k, 4, 10) * (1 - own_pct)
            if worst is None or e > worst["evidence"]:
                worst = {
                    "signal_id": "S2.g", "family": "behaviour", "label": "Perpetual loser",
                    "observed": k, "observed_text": f"{vendor_by_id[v]['legal_name']} has lost to this winner {k} times",
                    "baseline_text": f"its own win rate sits at the {pct(own_pct, 0)} mark among all bidders",
                    "evidence": round(e, 4), "weight": 0.5,
                    "explanation": (f"{vendor_by_id[v]['legal_name']} has bid against {vendor_by_id[winner]['legal_name']} "
                                    f"{k} times and won none of them, while winning little elsewhere."),
                }
    if worst:
        families["behaviour"].append(worst)

    # near-simultaneous submissions
    times = sorted([pd.Timestamp(b["submitted_at"]) for b in bl])
    close = sum(1 for a, b in zip(times, times[1:]) if (b - a).total_seconds() <= 120)
    if close >= 1:
        families["behaviour"].append({
            "signal_id": "S2.h", "family": "behaviour", "label": "Near-simultaneous submissions",
            "observed": close, "observed_text": f"{close} pair(s) submitted within two minutes",
            "baseline_text": "independent firms rarely file within the same two minutes",
            "evidence": round(ramp(close, 0, 3), 4), "weight": 0.2,
            "explanation": f"{close} pair(s) of supposedly independent bids were submitted within two minutes of each other.",
        })

    # trivial disqualifications
    dq = [b for b in bl if b["status"] == "disqualified"]
    if dq:
        v = dq[0]["vendor_id"]
        k = disq_pairs.get((v, winner), 0)
        if k >= 1:
            families["behaviour"].append({
                "signal_id": "S2.i", "family": "behaviour", "label": "Trivial disqualification",
                "observed": k, "observed_text": f"{dq[0]['disqualification_reason']} ({k} time(s) against this winner)",
                "baseline_text": "avoidable defects, repeated in tenders won by the same firm",
                "evidence": round(ramp(k, 1, 4), 4), "weight": 0.4,
                "explanation": (f"{vendor_by_id[v]['legal_name']} was disqualified for an easily avoidable reason "
                                f"({dq[0]['disqualification_reason'].lower()}) in a tender won by {vendor_by_id[winner]['legal_name']}."),
            })

    chosen = []
    for fam, sigs in families.items():
        chosen.append(max(sigs, key=lambda s: s["weight"] * s["evidence"]))
    score = noisy_or([(s["weight"], s["evidence"]) for s in chosen])

    n_valid = sc["n_valid"]
    c_n = 0.4 if n_valid == 2 else (0.7 if n_valid == 3 else 1.0)
    c_quality = 1.0
    if tid not in item_tenders:
        c_quality *= 0.85
    if pd.isna(est):
        c_quality *= 0.85
    conf = c_n * pi["c_peer"] * c_quality
    s2_results[tid] = {"status": "scored", "score": score, "confidence": conf,
                       "signals": sorted(chosen, key=lambda s: -s["weight"] * s["evidence"]),
                       "note": ""}

print(f"S2 done: {sum(1 for r in s2_results.values() if r['score'] > 0.5)} tenders above 0.5")


# ==========================================================================
# S3 - shell company detector
# ==========================================================================

person_count = defaultdict(int)
for p in persons.to_dict("records"):
    person_count[p["person_id"]] += 1
persons_by_vendor = defaultdict(list)
for p in persons.to_dict("records"):
    persons_by_vendor[p["vendor_id"]].append(p)

vendor_first_bid = {}
for r in bids.to_dict("records"):
    tid = r["tender_id"]
    pub = T.loc[tid, "publish_date"]
    cur = vendor_first_bid.get(r["vendor_id"])
    if cur is None or pub < cur:
        vendor_first_bid[r["vendor_id"]] = pub

vendor_buyers = defaultdict(set)
vendor_contract_total = defaultdict(float)
for a in tenders.to_dict("records"):
    vendor_buyers[a["awarded_vendor_id"]].add(a["buyer_id"])
    vendor_contract_total[a["awarded_vendor_id"]] += float(a["awarded_value"] or 0)

s3_vendor = {}
for v in vendors.to_dict("records"):
    vid = v["vendor_id"]
    if int(v["is_govt_owned"] or 0) == 1:
        s3_vendor[vid] = {"status": "not_applicable", "score": 0.0, "confidence": 0.0, "signals": [],
                          "note": "Government-owned supplier, excluded from shell-company scoring."}
        continue

    fam = defaultdict(list)
    inc = pd.Timestamp(v["incorporation_date"])
    first_bid = vendor_first_bid.get(vid)
    biggest_bid = 0.0
    for tid in tenders_by_vendor.get(vid, []):
        for b in bids_by_tender.get(tid, []):
            if b["vendor_id"] == vid:
                biggest_bid = max(biggest_bid, float(b["bid_value"]))

    have = set()
    if first_bid is not None:
        have.add("F1")
        months = (first_bid - inc).days / 30.44
        x = ramp(12 - months, 0, 12)
        w = 0.5
        if int(v["msme_registered"] or 0) or int(v["startup_registered"] or 0):
            w = 0.25  # policy relaxes prior-experience norms for MSEs and start-ups
        if x > 0:
            fam["F1_age"].append({
                "signal_id": "S3.A1", "family": "F1 Age", "label": "Young at first bid",
                "observed": round(months, 1), "observed_text": f"{months:.0f} months old when it first bid",
                "baseline_text": "peer winners of work this size are years old",
                "evidence": round(x, 4), "weight": w,
                "explanation": f"The firm was {months:.0f} months old when it first bid for public work.",
            })

    cap = float(v["paid_up_capital"] or 0)
    turn = pd.to_numeric(pd.Series([v["annual_turnover"]]), errors="coerce").iloc[0]
    if cap > 0 and biggest_bid > 0:
        have.add("F2")
        x = min(1.0, max(0.0, math.log10(biggest_bid / cap) / 3.0))
        fam["F2_capacity"].append({
            "signal_id": "S3.B1", "family": "F2 Financial capacity", "label": "Bid value against paid-up capital",
            "observed": round(biggest_bid / cap, 1),
            "observed_text": f"largest bid is {biggest_bid / cap:.0f}x its paid-up capital of {inr(cap)}",
            "baseline_text": "low capital is legal and common; the ratio is what is unusual",
            "evidence": round(x, 4), "weight": 0.6,
            "explanation": f"The firm bid {inr(biggest_bid)} against paid-up capital of {inr(cap)}.",
        })
    if not pd.isna(turn) and turn and biggest_bid > 0:
        have.add("F2")
        x = ramp(biggest_bid / float(turn), 2, 20)
        if x > 0:
            fam["F2_capacity"].append({
                "signal_id": "S3.B2", "family": "F2 Financial capacity", "label": "Bid value against turnover",
                "observed": round(biggest_bid / float(turn), 2),
                "observed_text": f"bid is {biggest_bid / float(turn):.1f}x declared annual turnover",
                "baseline_text": "peer winners bid well inside their turnover",
                "evidence": round(x, 4), "weight": 0.6,
                "explanation": f"The bid of {inr(biggest_bid)} exceeds the firm's declared annual turnover of {inr(turn)}.",
            })

    unit_n = int(v["address_unit_share_count"] or 1)
    bld_n = int(v["address_building_share_count"] or 1)
    have.add("F3")
    x_unit = ramp(unit_n, 5, 50)
    x_bld = ramp(bld_n, 10, 100)
    if max(x_unit, x_bld) > 0:
        if x_unit >= x_bld:
            fam["F3_address"].append({
                "signal_id": "S3.C1", "family": "F3 Address", "label": "Shared registered address",
                "observed": unit_n, "observed_text": f"{unit_n} entities registered at the same unit",
                "baseline_text": "ramp from 5 to 50 entities at unit level",
                "evidence": round(x_unit, 4), "weight": 0.5,
                "explanation": f"The registered address is shared by {unit_n} other entities at the same unit.",
            })
        else:
            fam["F3_address"].append({
                "signal_id": "S3.C1b", "family": "F3 Address", "label": "Mass-registration building",
                "observed": bld_n, "observed_text": f"{bld_n} entities registered in the same building",
                "baseline_text": "ramp from 10 to 100 entities at building level",
                "evidence": round(x_bld, 4), "weight": 0.5,
                "explanation": f"The registered address sits in a building holding {bld_n} registrations.",
            })

    dirs = persons_by_vendor.get(vid, [])
    if dirs:
        have.add("F4")
        k = max(person_count[p["person_id"]] for p in dirs)
        x = ramp(k, 10, 50)
        if x > 0:
            fam["F4_management"].append({
                "signal_id": "S3.D1", "family": "F4 Management", "label": "Director with many directorships",
                "observed": k, "observed_text": f"a director sits on {k} boards in this dataset",
                "baseline_text": "ramp from 10 to 50 directorships",
                "evidence": round(x, 4), "weight": 0.4,
                "explanation": f"One of its directors holds {k} directorships.",
            })

    have.add("F5")
    if int(v["has_employee_records"] or 0) == 0:
        fam["F5_footprint"].append({
            "signal_id": "S3.E1", "family": "F5 Operating footprint", "label": "No employee or filing record",
            "observed": 1, "observed_text": "no employee or regular filing record found",
            "baseline_text": "operating firms of this size file regularly",
            "evidence": 1.0, "weight": 0.5,
            "explanation": "No evidence of employees or regular statutory filings across the contract period.",
        })
    cats = str(v["categories"] or "").split("|")
    if v["activity_code"] and cats and v["activity_code"] not in cats:
        fam["F5_footprint"].append({
            "signal_id": "S3.E2", "family": "F5 Operating footprint", "label": "Activity unrelated to contract",
            "observed": 1, "observed_text": f"registered activity: {v['activity_code']}",
            "baseline_text": "registered activity normally matches the work bid for",
            "evidence": 1.0, "weight": 0.4,
            "explanation": f"Its registered activity ({v['activity_code']}) does not match the work it bids for.",
        })
    if int(v["has_website"] or 0) == 0 and "gmail" in str(v["email"]):
        fam["F5_footprint"].append({
            "signal_id": "S3.E3", "family": "F5 Operating footprint", "label": "No web presence",
            "observed": 1, "observed_text": "free webmail, no website",
            "baseline_text": "weak on its own; many honest small firms look the same",
            "evidence": 1.0, "weight": 0.1,
            "explanation": "The firm uses free webmail and has no website.",
        })

    have.add("F6")
    if str(v["registry_status"]) in ("struck_off", "dormant") and v["struck_off_date"]:
        fam["F6_lifecycle"].append({
            "signal_id": "S3.G1", "family": "F6 Life-cycle", "label": "Burst then vanish",
            "observed": 1, "observed_text": f"registry status {v['registry_status']} since {v['struck_off_date']}",
            "baseline_text": "active suppliers stay active after delivering",
            "evidence": 1.0, "weight": 0.6,
            "explanation": f"The firm won work and then went {v['registry_status'].replace('_', ' ')} shortly afterwards.",
        })
    if len(vendor_buyers.get(vid, set())) == 1 and len(awards_by_vendor.get(vid, [])) >= 3:
        fam["F6_lifecycle"].append({
            "signal_id": "S3.G2", "family": "F6 Life-cycle", "label": "Single-buyer revenue",
            "observed": 1, "observed_text": "every contract comes from one buyer",
            "baseline_text": "suppliers of this size usually serve several buyers",
            "evidence": 1.0, "weight": 0.3,
            "explanation": "All of this firm's public contracts come from a single buyer.",
        })

    chosen = [max(sigs, key=lambda s: s["weight"] * s["evidence"]) for sigs in fam.values() if sigs]
    raw = noisy_or([(s["weight"], s["evidence"]) for s in chosen])
    strong = sum(1 for s in chosen if s["weight"] * s["evidence"] >= 0.15)
    score = min(raw, 0.35) if strong < 2 else raw
    conf = len(have) / 6.0
    s3_vendor[vid] = {"status": "scored", "score": score, "confidence": conf,
                      "signals": sorted(chosen, key=lambda s: -s["weight"] * s["evidence"]),
                      "capped": strong < 2,
                      "note": ("Only one trait fired, so the score is capped at 0.35: young, small or thinly "
                               "capitalised firms are common and legal.") if strong < 2 else ""}

s3_results = {}
for tid in tenders["tender_id"]:
    bl = bids_by_tender.get(tid, [])
    cands = [b["vendor_id"] for b in bl] or [T.loc[tid, "awarded_vendor_id"]]
    best_v, best = None, None
    for v in cands:
        r = s3_vendor.get(v)
        if not r or r["status"] != "scored":
            continue
        if best is None or r["score"] > best["score"]:
            best, best_v = r, v
    if best is None:
        s3_results[tid] = {"status": "insufficient_data", "score": 0.0, "confidence": 0.0, "signals": [], "note": ""}
        continue
    won = best_v == T.loc[tid, "awarded_vendor_id"]
    s3_results[tid] = {
        "status": "scored", "score": best["score"], "confidence": best["confidence"],
        "signals": best["signals"], "subject_vendor": best_v,
        "subject_name": vendor_by_id[best_v]["legal_name"], "subject_won": bool(won),
        "note": (best["note"] + (" The most shell-like firm here is the winner, which is a money-at-risk problem."
                                 if won else " The most shell-like firm here is a losing bidder, which points to staged competition.")).strip(),
    }

print(f"S3 done: {sum(1 for r in s3_vendor.values() if r['score'] > 0.5)} vendors above 0.5")


# ==========================================================================
# S4 - price and contract value detector
# ==========================================================================

# peer 90th percentile of amendment growth, per category
growth_p90 = {}
for cat, grp in tenders.groupby("category_code", sort=False):
    vals = [growth.get(t, 1.0) for t in grp["tender_id"]]
    growth_p90[cat] = max(1.10, float(np.percentile(vals, 90)))

# estimate-leak history per buyer-vendor pair
leak_pairs = defaultdict(int)
for t in tenders.to_dict("records"):
    est, aw = t["estimate_value"], t["awarded_value"]
    if est and not pd.isna(est) and est > 0 and abs(aw - est) / est <= 0.005:
        leak_pairs[(t["buyer_id"], t["awarded_vendor_id"])] += 1

# threshold splitting sequences
thr_rows = thresholds.to_dict("records")


def threshold_for(kind, dt):
    best = None
    for t in thr_rows:
        if t["kind"] == kind and pd.Timestamp(t["effective_from"]) <= dt:
            if best is None or t["effective_from"] > best["effective_from"]:
                best = t
    return best


split_groups = defaultdict(list)
for t in sorted(tenders.to_dict("records"), key=lambda r: r["publish_date"]):
    thr = threshold_for(t["procurement_kind"], t["publish_date"])
    if not thr:
        continue
    Tv = float(thr["value"])
    if 0.8 * Tv <= float(t["awarded_value"]) < Tv:
        key = (t["buyer_id"], cluster_of.get(t["awarded_vendor_id"], t["awarded_vendor_id"]), t["category_code"])
        split_groups[key].append(t)

split_hits = {}
for key, rows in split_groups.items():
    rows = sorted(rows, key=lambda r: r["publish_date"])
    i = 0
    while i < len(rows):
        window = [rows[i]]
        j = i + 1
        while j < len(rows) and (rows[j]["publish_date"] - rows[i]["publish_date"]).days <= 60:
            window.append(rows[j])
            j += 1
        if len(window) >= 2:
            thr = threshold_for(window[0]["procurement_kind"], window[0]["publish_date"])
            total = sum(float(r["awarded_value"]) for r in window)
            if total >= float(thr["value"]):
                for r in window:
                    split_hits[r["tender_id"]] = {
                        "count": len(window), "total": total, "threshold": float(thr["value"]),
                        "rule": thr["rule"], "peers": [r2["tender_id"] for r2 in window],
                        "days": (window[-1]["publish_date"] - window[0]["publish_date"]).days,
                    }
        i = j if j > i + 1 else i + 1

s4_results = {}
for t in tenders.to_dict("records"):
    tid = t["tender_id"]
    pi = peer_info[tid]
    modules = {}

    r = price_resid.get(tid)
    if r is not None:
        peer_r = [price_resid.get(p) for p in pi["df"]["tender_id"] if price_resid.get(p) is not None]
        z = robust_z(r, peer_r)
        if z is not None:
            unit_price = float(t["awarded_value"]) / max(1, float(t["quantity"]))
            peer_unit = float(np.median([float(T.loc[p, "awarded_value"]) / max(1, float(T.loc[p, "quantity"]))
                                         for p in pi["df"]["tender_id"]]))
            if z > 0:
                e, w, lbl = ramp(z, 3, 6), 0.8, "Unit price above comparable purchases"
            else:
                e, w, lbl = ramp(-z, 3, 6), 0.3, "Unit price far below comparable purchases"
            if e > 0:
                over = unit_price / peer_unit - 1
                modules["4A"] = {
                    "signal_id": "S4.4A", "family": "unit_price", "label": lbl,
                    "observed": round(unit_price, 2),
                    "observed_text": f"Rs {unit_price:,.0f} per {t['unit']} against a peer median of Rs {peer_unit:,.0f}",
                    "baseline_text": f"robust z = {z:+.1f} against {pi['n']} peers ({pi['label']})",
                    "evidence": round(e, 4), "weight": w,
                    "explanation": (f"The awarded unit price is {pct(abs(over), 0)} "
                                    f"{'above' if over > 0 else 'below'} the modelled peer price for this quantity, "
                                    f"region and year."),
                }

    est = t["estimate_value"]
    est_sigs = []
    if est and not pd.isna(est) and est > 0:
        ratio = float(t["awarded_value"]) / float(est)
        e = ramp(ratio, 1.10, 1.30)
        if e > 0:
            est_sigs.append({
                "signal_id": "S4.4B1", "family": "estimate", "label": "Award above estimate",
                "observed": round(ratio, 3), "observed_text": f"award is {pct(ratio - 1)} above the estimate",
                "baseline_text": "ramp from +10% to +30%",
                "evidence": round(e, 4), "weight": 0.5,
                "explanation": f"The contract was awarded at {inr(t['awarded_value'])} against an estimate of {inr(est)}.",
            })
        k = leak_pairs.get((t["buyer_id"], t["awarded_vendor_id"]), 0)
        if k >= 2 and abs(float(t["awarded_value"]) - float(est)) / float(est) <= 0.005:
            est_sigs.append({
                "signal_id": "S4.4B2", "family": "estimate", "label": "Bid matches confidential estimate",
                "observed": k, "observed_text": f"{k} awards to this vendor land within 0.5% of the estimate",
                "baseline_text": "the estimate is confidential until bids are opened",
                "evidence": round(ramp(k, 2, 6), 4), "weight": 0.6,
                "explanation": (f"The winning bid matches the buyer's confidential estimate almost exactly, "
                                f"and this has happened {k} times for this buyer-vendor pair."),
            })
        er = est_resid.get(tid)
        if er is not None:
            peer_er = [est_resid.get(p) for p in pi["df"]["tender_id"] if est_resid.get(p) is not None]
            ez = robust_z(er, peer_er)
            if ez is not None and ramp(ez, 3, 6) > 0:
                est_sigs.append({
                    "signal_id": "S4.4B3", "family": "estimate", "label": "Inflated estimate",
                    "observed": round(ez, 2), "observed_text": f"estimate itself sits at robust z = {ez:+.1f}",
                    "baseline_text": "compared with peer estimates for the same work",
                    "evidence": round(ramp(ez, 3, 6), 4), "weight": 0.4,
                    "explanation": ("The buyer's own estimate is far above peer prices, which makes an inflated "
                                    "award look normal. This points at the buyer side, not only the vendor."),
                })
    if est_sigs:
        modules["4B"] = max(est_sigs, key=lambda s: s["weight"] * s["evidence"])

    g = growth.get(tid, 1.0)
    if g > 1.0:
        p90 = growth_p90.get(t["category_code"], 1.10)
        e = ramp(g, p90, p90 + 0.30)
        frac = first_amend_frac.get(tid)
        early = frac is not None and frac <= 0.2
        if early:
            e = min(1.0, e * 1.2)
        if e > 0:
            c = contract_by_tender.get(tid, {})
            modules["4C"] = {
                "signal_id": "S4.4C", "family": "amendment", "label": "Amendment inflation",
                "observed": round(g, 3),
                "observed_text": f"contract grew {pct(g - 1)} from {inr(c.get('original_value'))} to {inr(c.get('final_value'))}",
                "baseline_text": f"peer 90th percentile growth is {pct(p90 - 1)}",
                "evidence": round(e, 4), "weight": 0.6,
                "explanation": (f"The signed contract was later amended upward by {pct(g - 1)}"
                                + (", with the first increase inside the first fifth of the contract period."
                                   if early else ".")),
            }

    sp = split_hits.get(tid)
    if sp:
        modules["4D"] = {
            "signal_id": "S4.4D", "family": "threshold", "label": "Purchases split below a threshold",
            "observed": sp["count"],
            "observed_text": f"{sp['count']} contracts in {sp['days']} days, each just below {inr(sp['threshold'])}, totalling {inr(sp['total'])}",
            "baseline_text": sp["rule"],
            "evidence": round(ramp(sp["count"], 2, 5), 4), "weight": 0.7,
            "explanation": (f"The same buyer placed {sp['count']} contracts with the same supplier for the same "
                            f"category within {sp['days']} days, each sized just under the {inr(sp['threshold'])} "
                            f"approval threshold, together worth {inr(sp['total'])}."),
        }

    chosen = list(modules.values())
    score = noisy_or([(s["weight"], s["evidence"]) for s in chosen])
    has_items = tid in item_tenders
    c_gran = 1.0 if has_items else (0.7 if t["scope_metric_value"] else 0.4)
    conf = pi["c_peer"] * c_gran
    status = "scored" if pi["sufficient"] else "insufficient_data"
    s4_results[tid] = {"status": status, "score": score if status == "scored" else 0.0,
                       "confidence": conf if status == "scored" else 0.0,
                       "signals": sorted(chosen, key=lambda s: -s["weight"] * s["evidence"]),
                       "note": "" if status == "scored" else
                       f"Only {pi['n']} comparable tenders exist even at the widest peer definition."}

print(f"S4 done: {sum(1 for r in s4_results.values() if r['score'] > 0.5)} tenders above 0.5")


# ==========================================================================
# S5 - award concentration, rotation and competition health
# ==========================================================================

pair_bids = defaultdict(int)
pair_wins = defaultdict(int)
vendor_bids_total = defaultdict(int)
vendor_wins_total = defaultdict(int)
for tid, grp in bids.groupby("tender_id", sort=False):
    buyer = T.loc[tid, "buyer_id"]
    for r in grp.to_dict("records"):
        pair_bids[(buyer, r["vendor_id"])] += 1
        vendor_bids_total[r["vendor_id"]] += 1
        if r["is_winner"]:
            pair_wins[(buyer, r["vendor_id"])] += 1
            vendor_wins_total[r["vendor_id"]] += 1

# buyer competition profile
buyer_profile = {}
for bid_, grp in tenders.groupby("buyer_id", sort=False):
    n = len(grp)
    single = sum(1 for t in grp["tender_id"] if screen_rows.get(t, {}).get("n_all", 0) <= 1)
    nonopen = sum(1 for m in grp["procurement_method"] if m in ("limited", "direct"))
    short = sum(1 for dsr in grp["tender_period_days"] if dsr <= 7)
    buyer_profile[bid_] = {"n": n, "single_rate": single / n, "nonopen_rate": nonopen / n,
                           "short_rate": short / n}
all_single = [p["single_rate"] for p in buyer_profile.values()]
all_nonopen = [p["nonopen_rate"] for p in buyer_profile.values()]
all_short = [p["short_rate"] for p in buyer_profile.values()]

# rotation groups: S1 clusters, plus frequent co-bidders
cobid_counts = defaultdict(int)
for tid, grp in bids.groupby("tender_id", sort=False):
    vs = sorted(set(grp["vendor_id"]))
    for i in range(len(vs)):
        for j in range(i + 1, len(vs)):
            cobid_counts[(vs[i], vs[j])] += 1

groups = []
for c, members in real_clusters.items():
    if len(cluster_cobid.get(c, set())) >= 5:
        groups.append({"id": f"CL-{c}", "members": members, "from_links": True})
seen_pairs = set()
for (a, b), k in cobid_counts.items():
    if k >= 6 and cluster_of.get(a) != cluster_of.get(b):
        rate = k / max(1, min(vendor_bids_total[a], vendor_bids_total[b]))
        if rate > 0.6:
            key = tuple(sorted([a, b]))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            members = set(key)
            for (x, y), k2 in cobid_counts.items():
                if k2 >= 6 and (x in members or y in members):
                    members |= {x, y}
            groups.append({"id": f"CB-{sorted(members)[0]}", "members": sorted(members), "from_links": False})

seen_ids = set()
uniq_groups = []
for g in groups:
    key = tuple(sorted(g["members"]))
    if key in seen_ids or len(key) < 3:
        continue
    seen_ids.add(key)
    uniq_groups.append(g)

group_result = {}
for g in uniq_groups:
    members = set(g["members"])
    gt = []
    for tid, grp in bids.groupby("tender_id", sort=False):
        present = members & set(grp["vendor_id"])
        if len(present) >= 2:
            gt.append((tid, T.loc[tid, "awarded_vendor_id"]))
    if len(gt) < 5:
        continue
    winners = [w for _, w in gt]
    inside = [w for w in winners if w in members]
    shares = defaultdict(int)
    for w in inside:
        shares[w] += 1
    if not shares or len(shares) < 2:
        continue
    tot = sum(shares.values())
    H = -sum((s / tot) * math.log(s / tot) for s in shares.values()) / math.log(max(2, len(members)))
    if H < 0.8:
        continue
    seq = [w for w in winners if w in members]
    repeats = sum(1 for a, b in zip(seq, seq[1:]) if a == b)
    perm = []
    for _ in range(1000):
        s = seq[:]
        random.shuffle(s)
        perm.append(sum(1 for a, b in zip(s, s[1:]) if a == b))
    p = (sum(1 for x in perm if x <= repeats) + 1) / (len(perm) + 1)
    e1 = ramp(-math.log10(max(p, 1e-6)), 1.3, 3)
    outsider_share = 1 - len(inside) / len(winners)
    e2 = ramp(1 - outsider_share, 0.85, 1.0)
    group_result[g["id"]] = {
        "members": sorted(members), "tenders": [t for t, _ in gt], "entropy": H,
        "repeats": repeats, "p": p, "e1": e1, "e2": e2, "from_links": g["from_links"],
        "evidence": max(e1, e2),
    }

# market allocation
alloc_result = {}
for gid, g in group_result.items():
    gaps = []
    for m in g["members"]:
        wins = awards_by_vendor.get(m, [])
        bidsm = [T.loc[t, "district"] for t in tenders_by_vendor.get(m, [])]
        if not wins or not bidsm:
            continue
        wd = defaultdict(int)
        for w in wins:
            wd[buyer_by_id[w["buyer_id"]]["district"]] += 1
        top = max(wd, key=wd.get)
        w_share = wd[top] / len(wins)
        b_share = sum(1 for x in bidsm if x == top) / len(bidsm)
        gaps.append(w_share - b_share)
    if gaps:
        alloc_result[gid] = float(np.mean(gaps))

s5_results = {}
for t in tenders.to_dict("records"):
    tid, buyer, vend = t["tender_id"], t["buyer_id"], t["awarded_vendor_id"]
    modules = {}

    nb, nw = pair_bids[(buyer, vend)], pair_wins[(buyer, vend)]
    eb = vendor_bids_total[vend] - nb
    ew = vendor_wins_total[vend] - nw
    if nb >= 4:
        lo_here, _ = rate_bounds(nw, nb)
        if eb >= 3:
            _, hi_else = rate_bounds(ew, eb)
            lift = lo_here / max(hi_else, 1e-6)
            e = min(1.0, max(0.0, math.log2(max(lift, 1e-6)) / 3.0))
            if e > 0:
                modules["5A"] = {
                    "signal_id": "S5.5A", "family": "favouritism", "label": "Buyer-vendor favouritism",
                    "observed": round(lift, 2),
                    "observed_text": f"{nw} wins from {nb} bids here, {ew} from {eb} elsewhere",
                    "baseline_text": f"conservative lift {lift:.2f}x after smoothing and confidence bounds",
                    "evidence": round(e, 4), "weight": 0.7,
                    "explanation": (f"{vendor_by_id[vend]['legal_name']} wins {nw} of {nb} bids with this buyer "
                                    f"but only {ew} of {eb} elsewhere. Even on a conservative reading it wins "
                                    f"{lift:.1f} times more often here than anywhere else."),
                }
        elif nb >= 5 and nw / nb > 0.6:
            modules["5A"] = {
                "signal_id": "S5.5A-noelse", "family": "favouritism", "label": "Concentration without outside history",
                "observed": round(nw / nb, 2),
                "observed_text": f"{nw} wins from {nb} bids, and almost no bidding elsewhere",
                "baseline_text": "no outside record to compare against",
                "evidence": round(ramp(nw / nb, 0.5, 0.95), 4), "weight": 0.5,
                "explanation": (f"{vendor_by_id[vend]['legal_name']} bids almost exclusively for this buyer "
                                f"and wins {nw} of {nb} times, so there is no outside record to compare with."),
            }

    for gid, g in group_result.items():
        if tid in g["tenders"] and (vend in g["members"]):
            e = g["evidence"]
            if e > 0:
                modules["5B"] = {
                    "signal_id": "S5.5B", "family": "rotation", "label": "Bid rotation",
                    "observed": round(g["p"], 4),
                    "observed_text": f"{len(g['members'])} firms, wins split evenly (entropy {g['entropy']:.2f}), repeat winners p = {g['p']:.3f}",
                    "baseline_text": "1,000 shuffles of the winner order",
                    "evidence": round(e, 4), "weight": 0.7,
                    "explanation": (f"A group of {len(g['members'])} firms meets in {len(g['tenders'])} tenders and "
                                    f"the wins rotate between them far more evenly than chance would produce."),
                }
            gap = alloc_result.get(gid)
            if gap and ramp(gap, 0.30, 0.70) > 0:
                modules["5C"] = {
                    "signal_id": "S5.5C", "family": "allocation", "label": "Market allocation",
                    "observed": round(gap, 3),
                    "observed_text": f"members win {pct(gap)} more often in their own district than they bid there",
                    "baseline_text": "honest firms win roughly where they bid",
                    "evidence": round(ramp(gap, 0.30, 0.70), 4), "weight": 0.5,
                    "explanation": ("The firms in this group bid widely but each wins almost only in its own "
                                    "district, the shape of a territory split."),
                }
            break

    bp = buyer_profile[buyer]
    health = []
    u = percentile_of(bp["single_rate"], all_single, True)
    if u and ramp(u, 0.90, 0.99) > 0:
        health.append({
            "signal_id": "S5.5D1", "family": "competition_health", "label": "Single-bid rate",
            "observed": round(bp["single_rate"], 3),
            "observed_text": f"{pct(bp['single_rate'], 0)} of this buyer's tenders drew exactly one bid",
            "baseline_text": f"higher than {pct(u, 0)} of peer buyers",
            "evidence": round(ramp(u, 0.90, 0.99), 4), "weight": 0.5,
            "explanation": f"{pct(bp['single_rate'], 0)} of this buyer's tenders attract a single bid, more than {pct(u, 0)} of comparable buyers.",
        })
    u = percentile_of(bp["nonopen_rate"], all_nonopen, True)
    if u and ramp(u, 0.90, 0.99) > 0:
        health.append({
            "signal_id": "S5.5D2", "family": "competition_health", "label": "Non-open procedures",
            "observed": round(bp["nonopen_rate"], 3),
            "observed_text": f"{pct(bp['nonopen_rate'], 0)} of tenders use limited or direct procedures",
            "baseline_text": f"higher than {pct(u, 0)} of peer buyers",
            "evidence": round(ramp(u, 0.90, 0.99), 4), "weight": 0.4,
            "explanation": f"This buyer awards {pct(bp['nonopen_rate'], 0)} of its tenders through limited or direct procedures.",
        })
    u = percentile_of(bp["short_rate"], all_short, True)
    if u and ramp(u, 0.90, 0.99) > 0:
        health.append({
            "signal_id": "S5.5D3", "family": "competition_health", "label": "Short bidding windows",
            "observed": round(bp["short_rate"], 3),
            "observed_text": f"{pct(bp['short_rate'], 0)} of tenders gave bidders a week or less",
            "baseline_text": f"higher than {pct(u, 0)} of peer buyers",
            "evidence": round(ramp(u, 0.90, 0.99), 4), "weight": 0.4,
            "explanation": f"{pct(bp['short_rate'], 0)} of this buyer's tenders close within a week of publication.",
        })
    if health:
        modules["5D"] = max(health, key=lambda s: s["weight"] * s["evidence"])

    chosen = list(modules.values())
    score = noisy_or([(s["weight"], s["evidence"]) for s in chosen])
    conf = ramp(bp["n"], 5, 20)
    if "5B" in modules:
        for gid, g in group_result.items():
            if tid in g["tenders"] and not g["from_links"]:
                conf *= 0.8
                break
    s5_results[tid] = {"status": "scored" if bp["n"] >= 5 else "insufficient_data",
                       "score": score, "confidence": conf,
                       "signals": sorted(chosen, key=lambda s: -s["weight"] * s["evidence"]), "note": ""}

print(f"S5 done: {sum(1 for r in s5_results.values() if r['score'] > 0.5)} tenders above 0.5")


# ==========================================================================
# S6 (part 3) - context rules
# ==========================================================================

CONTEXT_RULES = []


def _text(v):
    """CSV blanks arrive from pandas as NaN; str(NaN) is the truthy string 'nan'."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "none") else s


def _flag(v):
    try:
        return int(float(v)) == 1
    except (TypeError, ValueError):
        return False


def context_for(t):
    """Returns {detector: (m, rule_text, evidence_text)} for one tender.

    Several rules can hit one detector. The strongest dampening wins, and every
    rule that fired is named, because the dossier has to show all of them.
    """
    hits = defaultdict(list)   # detector -> [(m, rule, evidence)]

    if _flag(t["proprietary_certificate"]):
        ev = _text(t["justification_note"]) or "Proprietary article certificate recorded against the tender"
        hits["S5"].append((0.3, "Proprietary or OEM-authorised item", ev))
        hits["S4"].append((0.3, "Proprietary or OEM-authorised item", ev))
    if _flag(t["emergency_declared"]):
        ev = _text(t["justification_note"]) or "Official emergency declaration covering the date and region"
        hits["S4"].append((0.4, "Declared emergency", ev))
        hits["S5"].append((0.4, "Declared emergency", ev))
    fw = _text(t["framework_ref"])
    if fw:
        hits["S5"].append((0.2, "Framework call-off", f"Call-off under framework agreement {fw}"))
        hits["S4"].append((0.2, "Framework call-off", f"Call-off under framework agreement {fw}"))
    if _flag(t["reserved_for_mse"]):
        hits["S5"].append((0.5, "Reserved procurement",
                           "Tender reserved for micro and small enterprises"))
    v = vendor_by_id.get(t["awarded_vendor_id"], {})
    if _flag(v.get("is_spv", 0)):
        hits["S3"].append((0.4, "Declared special purpose vehicle",
                           "SPV status stated in the tender documents"))
    ams = am_by_tender.get(t["tender_id"], [])
    if ams and all(a["reason_code"] == "scope_change_approved" and _text(a["approval_reference"])
                   for a in ams):
        hits["S4"].append((0.5, "Approved scope change",
                           f"Amendment reason code with approval reference {_text(ams[0]['approval_reference'])}"))

    out = {}
    for det, rules in hits.items():
        m = min(r[0] for r in rules)
        out[det] = (m, "; ".join(r[1] for r in rules), " | ".join(r[2] for r in rules))
    out["S1"] = out.get("S1", (1.0, "", ""))
    return out


# ==========================================================================
# S7 - fusion, prioritisation and explainability
# ==========================================================================

WK = {"S1": 0.90, "S2": 0.75, "S3": 0.60, "S4": 0.60, "S5": 0.50}
DETECTOR_NAMES = {
    "S1": "Relationships", "S2": "Cover bidding", "S3": "Shell company",
    "S4": "Price & value", "S5": "Concentration",
}

INNOCENT = {
    "S1": ["The firms may be openly related and permitted to bid under the tender rules.",
           "Family-run firms and firms sharing an accountant can look linked without acting together."],
    "S2": ["A genuinely efficient bidder can legitimately sit far below the rest.",
           "Schedule-of-rates work produces naturally tight bids."],
    "S3": ["The firm may be a recognised start-up or MSE bidding under relaxed eligibility criteria.",
           "A special purpose vehicle created for one project is young and thinly capitalised by design."],
    "S4": ["The price may reflect terrain, haulage or site conditions the model does not capture.",
           "The amendment may cover an approved scope change or a price-variation clause."],
    "S5": ["Only one supplier may be capable or authorised for this item.",
           "Firms that can handle one project at a time may alternate honestly."],
}

rows, sig_rows, det_rows, link_rows, innocent_rows, dq_rows = [], [], [], [], [], []

analysis = {}
for t in tenders.to_dict("records"):
    tid = t["tender_id"]
    ctx = context_for(t)
    dets = {"S1": s1_results[tid], "S2": s2_results[tid], "S3": s3_results[tid],
            "S4": s4_results[tid], "S5": s5_results[tid]}
    xs = {}
    for k, r in dets.items():
        m = ctx.get(k, (1.0, "", ""))[0]
        if k == "S1":
            m = max(m, 0.7)  # relationship evidence is never dampened below 70%
        x = WK[k] * r["confidence"] * m * r["score"]
        xs[k] = x
        r["m"] = m
        r["x"] = x
        r["context_rule"] = ctx.get(k, (1.0, "", ""))[1]
        r["context_evidence"] = ctx.get(k, (1.0, "", ""))[2]

    R = noisy_or([(1.0, x) for x in xs.values()])
    n_strong = sum(1 for x in xs.values() if x >= 0.25)
    capped = False
    if n_strong <= 1 and R > 0.55:
        R, capped = 0.55, True

    tot = sum(-math.log(max(1e-9, 1 - min(x, 0.999))) for x in xs.values())
    contrib = {k: (-math.log(max(1e-9, 1 - min(xs[k], 0.999))) / tot if tot > 0 else 0.0) for k in xs}
    c_case = sum(contrib[k] * dets[k]["confidence"] for k in xs)

    value_at_risk = float(contract_by_tender.get(tid, {}).get("final_value", t["awarded_value"]) or 0)
    I = min(1.0, max(0.1, math.log10(max(value_at_risk / 1e5, 1.0)) / 4.0))
    P = R * (0.5 + 0.5 * I)

    if P >= 0.50 and c_case < 0.4:
        tier, action = "Data gap", "Fix the data first"
    elif P >= 0.75 and c_case >= 0.6:
        tier, action = "Tier 1", "Investigate now"
    elif P >= 0.50:
        tier, action = "Tier 2", "Queue for review"
    elif P >= 0.30:
        tier, action = "Tier 3", "Monitor"
    else:
        tier, action = "Tier 4", "No action"

    # headline
    lead = max(contrib, key=contrib.get) if tot > 0 else "S1"
    lead_sig = dets[lead]["signals"][0]["explanation"] if dets[lead]["signals"] else ""
    buyer = buyer_by_id[t["buyer_id"]]
    if tot > 0 and lead_sig:
        headline = (f"{t['category_label']} worth {inr(t['awarded_value'])} awarded by "
                    f"{buyer['buyer_name']}. {lead_sig}")
    else:
        headline = (f"{t['category_label']} worth {inr(t['awarded_value'])} awarded by "
                    f"{buyer['buyer_name']}. No detector found an unusual pattern.")

    analysis[tid] = {"dets": dets, "xs": xs, "R": R, "P": P, "I": I, "tier": tier, "action": action,
                     "contrib": contrib, "c_case": c_case, "capped": capped,
                     "value_at_risk": value_at_risk, "headline": headline, "ctx": ctx,
                     "n_detectors": sum(1 for x in xs.values() if x >= 0.05)}

# --- cases: group tenders that share a cluster and a buyer --------------
case_key_of = {}
for t in tenders.to_dict("records"):
    tid = t["tender_id"]
    bidders = [b["vendor_id"] for b in bids_by_tender.get(tid, [])] or [t["awarded_vendor_id"]]
    cl = None
    for v in bidders:
        c = cluster_of.get(v)
        if c in real_clusters and len(cluster_cobid.get(c, set())) >= 2:
            cl = c
            break
    if cl is None:
        gid = next((g for g, gr in group_result.items() if tid in gr["tenders"]), None)
        cl = gid
    case_key_of[tid] = (cl, t["buyer_id"]) if cl else ("solo", tid)

case_members = defaultdict(list)
for tid, key in case_key_of.items():
    case_members[key].append(tid)

cases = {}
for i, (key, members) in enumerate(sorted(case_members.items(), key=lambda kv: str(kv[0])), start=1):
    members = sorted(members, key=lambda t: -analysis[t]["P"])[:50]
    cid = f"CASE-{i:04d}"
    best = {k: max(analysis[t]["dets"][k]["score"] * analysis[t]["dets"][k]["confidence"] *
                   analysis[t]["dets"][k]["m"] for t in members) for k in WK}
    xs = {k: WK[k] * best[k] for k in WK}
    R = noisy_or([(1.0, x) for x in xs.values()])
    n_strong = sum(1 for x in xs.values() if x >= 0.25)
    if n_strong <= 1:
        R = min(R, 0.55)
    var = sum(float(contract_by_tender.get(t, {}).get("final_value", 0) or 0) for t in members)
    I = min(1.0, max(0.1, math.log10(max(var / 1e5, 1.0)) / 4.0))
    P = R * (0.5 + 0.5 * I)
    tot = sum(-math.log(max(1e-9, 1 - min(x, 0.999))) for x in xs.values())
    contrib = {k: (-math.log(max(1e-9, 1 - min(xs[k], 0.999))) / tot if tot > 0 else 0.0) for k in xs}
    lead_t = members[0]
    cases[cid] = {"case_id": cid, "tenders": members, "n_tenders": len(members),
                  "buyer_id": T.loc[lead_t, "buyer_id"], "R": R, "P": P,
                  "value_at_risk": var, "contrib": contrib,
                  "headline": analysis[lead_t]["headline"], "lead_tender": lead_t}
    for t in members:
        analysis[t]["case_id"] = cid

# Impact is measured on the whole case, not the single contract: a tender that
# belongs to an 11-contract scheme carries the scheme's value at risk.
for cid, c in cases.items():
    for tid in c["tenders"]:
        a = analysis[tid]
        a["value_at_risk"] = c["value_at_risk"]
        a["I"] = min(1.0, max(0.1, math.log10(max(c["value_at_risk"] / 1e5, 1.0)) / 4.0))
        a["P"] = a["R"] * (0.5 + 0.5 * a["I"])
        if a["P"] >= 0.50 and a["c_case"] < 0.4:
            a["tier"], a["action"] = "Data gap", "Fix the data first"
        elif a["P"] >= 0.75 and a["c_case"] >= 0.6:
            a["tier"], a["action"] = "Tier 1", "Investigate now"
        elif a["P"] >= 0.50:
            a["tier"], a["action"] = "Tier 2", "Queue for review"
        elif a["P"] >= 0.30:
            a["tier"], a["action"] = "Tier 3", "Monitor"
        else:
            a["tier"], a["action"] = "Tier 4", "No action"

# --- flatten to tables -------------------------------------------------
for t in tenders.to_dict("records"):
    tid = t["tender_id"]
    a = analysis[tid]
    buyer = buyer_by_id[t["buyer_id"]]
    pi = peer_info[tid]
    winner = vendor_by_id[t["awarded_vendor_id"]]
    rows.append({
        "tender_id": tid, "ocid": t["ocid"], "buyer_id": t["buyer_id"],
        "buyer_name": buyer["buyer_name"], "government_level": buyer["government_level"],
        "government_name": buyer["government_name"], "department": buyer["department"],
        "district": buyer["district"], "state_code": buyer["state_code"],
        "title": t["title"], "category_code": t["category_code"], "category_label": t["category_label"],
        "procurement_method": t["procurement_method"], "publish_date": str(t["publish_date"].date()),
        "award_date": str(pd.Timestamp(t["award_date"]).date()),
        "estimate_value": None if pd.isna(t["estimate_value"]) else float(t["estimate_value"]),
        "awarded_value": float(t["awarded_value"]),
        "final_contract_value": float(contract_by_tender.get(tid, {}).get("final_value", t["awarded_value"]) or 0),
        "awarded_vendor_id": t["awarded_vendor_id"], "awarded_vendor_name": winner["legal_name"],
        "n_bids": int(screen_rows.get(tid, {}).get("n_all", 0)),
        "risk_r": round(a["R"], 4), "priority_p": round(a["P"], 4), "impact_i": round(a["I"], 4),
        "tier": a["tier"], "action": a["action"], "case_confidence": round(a["c_case"], 4),
        "n_detectors": a["n_detectors"], "corroboration_capped": int(a["capped"]),
        "value_at_risk": a["value_at_risk"], "case_id": a.get("case_id", ""),
        "headline": a["headline"], "peer_level": pi["level"], "peer_n": pi["n"],
        "peer_label": pi["label"], "c_peer": pi["c_peer"], "rules_version": RULES_VERSION,
        "scenario_truth": t["scenario_truth"],
    })

    for k, r in a["dets"].items():
        det_rows.append({
            "tender_id": tid, "detector": k, "detector_name": DETECTOR_NAMES[k],
            "status": r["status"], "score": round(r["score"], 4),
            "confidence": round(r["confidence"], 4), "m_factor": round(r["m"], 3),
            "weight": WK[k], "x": round(r["x"], 4),
            "contribution_pct": round(100 * a["contrib"].get(k, 0), 2),
            "context_rule": r.get("context_rule", ""), "context_evidence": r.get("context_evidence", ""),
            "note": r.get("note", ""),
            "subject": r.get("subject_name", ""),
        })
        for s in r["signals"]:
            sig_rows.append({
                "tender_id": tid, "detector": k, "signal_id": s["signal_id"],
                "family": s["family"], "label": s["label"],
                "observed": s.get("observed"), "observed_text": s.get("observed_text", ""),
                "baseline_text": s.get("baseline_text", ""),
                "evidence": round(float(s["evidence"]), 4), "weight": float(s["weight"]),
                "strength": round(float(s["evidence"]) * float(s["weight"]), 4),
                "explanation": s["explanation"],
            })

    for lk in tender_links.get(tid, []):
        link_rows.append({
            "tender_id": tid, "vendor_a": lk["a"], "vendor_b": lk["b"],
            "vendor_a_name": vendor_by_id[lk["a"]]["legal_name"],
            "vendor_b_name": vendor_by_id[lk["b"]]["legal_name"],
            "strength": lk["strength"],
            "basis": "; ".join(f"{x['label']} shared by {x['shared_by']} vendors" for x in lk["attrs"]),
            "basis_json": json.dumps(lk["attrs"]),
        })

    seen_expl = set()
    for k, r in a["dets"].items():
        if r["x"] >= 0.05:
            for text in INNOCENT[k]:
                if text not in seen_expl:
                    seen_expl.add(text)
                    innocent_rows.append({"tender_id": tid, "detector": k, "text": text})

    notes = []
    if pd.isna(t["estimate_value"]):
        notes.append("The buyer's estimate was not published, so estimate-based screens could not run.")
    if int(screen_rows.get(tid, {}).get("n_all", 0)) == 0:
        notes.append("Only the award was published; individual bids are missing.")
    if tid not in item_tenders:
        notes.append("No itemised price schedule, so prices are compared at total level against a scope metric.")
    if pi["level"] > 0:
        notes.append(f"The peer group needed {pi['level']} widening step(s) ({pi['label']}), which lowers confidence.")
    notes.append("Bid submission IP and device data are not collected in this dataset.")
    for n in notes:
        dq_rows.append({"tender_id": tid, "note": n})

case_rows = [{"case_id": c["case_id"], "lead_tender_id": c["lead_tender"], "buyer_id": c["buyer_id"],
              "buyer_name": buyer_by_id[c["buyer_id"]]["buyer_name"],
              "n_tenders": c["n_tenders"], "value_at_risk": c["value_at_risk"],
              "risk_r": round(c["R"], 4), "priority_p": round(c["P"], 4),
              "headline": c["headline"]} for c in cases.values()]
case_tender_rows = [{"case_id": c["case_id"], "tender_id": t} for c in cases.values() for t in c["tenders"]]

out = {
    "tender_analysis": pd.DataFrame(rows),
    "detector_results": pd.DataFrame(det_rows),
    "signals": pd.DataFrame(sig_rows),
    "vendor_links": pd.DataFrame(link_rows),
    "cases": pd.DataFrame(case_rows),
    "case_tenders": pd.DataFrame(case_tender_rows),
    "innocent_explanations": pd.DataFrame(innocent_rows),
    "data_quality": pd.DataFrame(dq_rows),
}
for name, df in out.items():
    df.to_sql(name, con, if_exists="replace", index=False)
con.execute("CREATE INDEX IF NOT EXISTS ix_an_tid ON tender_analysis(tender_id)")
con.execute("CREATE INDEX IF NOT EXISTS ix_sig_tid ON signals(tender_id)")
con.execute("CREATE INDEX IF NOT EXISTS ix_det_tid ON detector_results(tender_id)")
con.commit()

ta = out["tender_analysis"]
print("\ntier distribution")
print(ta["tier"].value_counts().to_string())
print("\ntop 10 by priority")
print(ta.sort_values("priority_p", ascending=False)[
          ["tender_id", "tier", "risk_r", "priority_p", "scenario_truth", "buyer_name"]].head(10).to_string(index=False))
print("\nmean priority by planted scenario")
print(ta.groupby("scenario_truth")["priority_p"].agg(["count", "mean"]).round(3).to_string())
con.close()

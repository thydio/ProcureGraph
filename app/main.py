"""ProcureGraph web app: FastAPI backend over the analysed SQLite database."""

import os
import sqlite3

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "procuregraph.db")
STATIC = os.path.join(ROOT, "app", "static")

app = FastAPI(title="ProcureGraph", docs_url="/api/docs")


@app.middleware("http")
async def revalidate_static(request, call_next):
    response = await call_next(request)
    if not request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def q(sql, params=()):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def one(sql, params=()):
    rows = q(sql, params)
    return rows[0] if rows else None


@app.get("/api/filters")
def filters():
    return {
        "governments": q("""SELECT DISTINCT government_level, government_name
                            FROM tender_analysis ORDER BY government_level, government_name"""),
        "departments": q("""SELECT DISTINCT government_name, department
                            FROM tender_analysis ORDER BY department"""),
        "categories": q("""SELECT category_code, category_label, COUNT(*) n
                           FROM tender_analysis GROUP BY category_code, category_label
                           ORDER BY category_label"""),
        "buyers": q("""SELECT DISTINCT buyer_id, buyer_name, department, government_name, district
                       FROM tender_analysis ORDER BY buyer_name"""),
        "tiers": ["Tier 1", "Tier 2", "Tier 3", "Tier 4", "Data gap"],
        "methods": [r["procurement_method"] for r in
                    q("SELECT DISTINCT procurement_method FROM tender_analysis ORDER BY 1")],
    }


@app.get("/api/overview")
def overview():
    tiers = q("""SELECT tier, COUNT(*) n, SUM(value_at_risk) value FROM tender_analysis
                 GROUP BY tier ORDER BY tier""")
    totals = one("""SELECT COUNT(*) tenders, SUM(awarded_value) awarded,
                           COUNT(DISTINCT buyer_id) buyers, COUNT(DISTINCT case_id) cases
                    FROM tender_analysis""")
    coverage = q("""SELECT detector, detector_name,
                           SUM(status='scored') scored,
                           SUM(status='not_applicable') not_applicable,
                           SUM(status='insufficient_data') insufficient,
                           ROUND(AVG(confidence), 3) mean_confidence
                    FROM detector_results GROUP BY detector ORDER BY detector""")
    top = q("""SELECT tender_id, buyer_name, department, government_name, category_label,
                      publish_date, awarded_value, awarded_vendor_name, n_bids,
                      tier, risk_r, priority_p, case_confidence, value_at_risk, headline, case_id
               FROM tender_analysis ORDER BY priority_p DESC LIMIT 20""")
    by_dept = q("""SELECT department, COUNT(*) n,
                          SUM(tier IN ('Tier 1','Tier 2')) flagged,
                          ROUND(AVG(priority_p), 3) mean_p
                   FROM tender_analysis GROUP BY department ORDER BY flagged DESC""")
    return {"tiers": tiers, "totals": totals, "coverage": coverage,
            "top": top, "by_department": by_dept}


@app.get("/api/search")
def search(tender_id: str = "", government: str = "", department: str = "", buyer_id: str = "",
           category: str = "", tier: str = "", method: str = "", text: str = "",
           min_priority: float = 0.0, sort: str = "priority", limit: int = 100):
    where, params = ["1=1"], []
    if tender_id:
        where.append("UPPER(tender_id) LIKE ?")
        params.append(f"%{tender_id.strip().upper()}%")
    for col, val in (("government_name", government), ("department", department),
                     ("buyer_id", buyer_id), ("category_code", category),
                     ("tier", tier), ("procurement_method", method)):
        if val:
            where.append(f"{col} = ?")
            params.append(val)
    if text:
        where.append("(title LIKE ? OR awarded_vendor_name LIKE ? OR buyer_name LIKE ?)")
        params += [f"%{text}%"] * 3
    if min_priority:
        where.append("priority_p >= ?")
        params.append(min_priority)
    order = {"priority": "priority_p DESC", "risk": "risk_r DESC", "value": "awarded_value DESC",
             "date": "publish_date DESC", "tender": "tender_id ASC"}.get(sort, "priority_p DESC")
    sql = f"""SELECT tender_id, title, buyer_name, department, government_name, government_level,
                     district, category_label, category_code, procurement_method, publish_date,
                     awarded_value, awarded_vendor_name, n_bids, tier, action, risk_r, priority_p,
                     case_confidence, n_detectors, value_at_risk, case_id, headline
              FROM tender_analysis WHERE {' AND '.join(where)}
              ORDER BY {order} LIMIT ?"""
    params.append(min(limit, 500))
    rows = q(sql, tuple(params))
    total = one(f"SELECT COUNT(*) n FROM tender_analysis WHERE {' AND '.join(where[:len(where)])}",
                tuple(params[:-1]))
    return {"results": rows, "count": len(rows), "total_matching": total["n"] if total else 0}


@app.get("/api/tender/{tender_id}")
def tender(tender_id: str):
    a = one("SELECT * FROM tender_analysis WHERE tender_id = ?", (tender_id.strip().upper(),))
    if not a:
        raise HTTPException(404, f"No tender {tender_id}")
    tid = a["tender_id"]

    detectors = q("SELECT * FROM detector_results WHERE tender_id = ? ORDER BY detector", (tid,))
    signals = q("SELECT * FROM signals WHERE tender_id = ? ORDER BY strength DESC", (tid,))
    for d in detectors:
        d["signals"] = [s for s in signals if s["detector"] == d["detector"]]

    bids = q("""SELECT b.vendor_id, b.bid_value, b.submitted_at, b.status,
                       b.disqualification_reason, b.is_winner, v.legal_name,
                       v.incorporation_date, v.paid_up_capital, v.size_tier,
                       v.msme_registered, v.startup_registered, v.registry_status
                FROM bids b JOIN vendors v ON v.vendor_id = b.vendor_id
                WHERE b.tender_id = ? ORDER BY b.bid_value""", (tid,))
    links = q("SELECT * FROM vendor_links WHERE tender_id = ? ORDER BY strength DESC", (tid,))
    innocent = q("SELECT detector, text FROM innocent_explanations WHERE tender_id = ?", (tid,))
    dq = [r["note"] for r in q("SELECT note FROM data_quality WHERE tender_id = ?", (tid,))]

    raw = one("SELECT * FROM tenders WHERE tender_id = ?", (tid,))
    contract = one("SELECT * FROM contracts WHERE tender_id = ?", (tid,))
    amendments = q("SELECT * FROM amendments WHERE tender_id = ? ORDER BY date", (tid,))
    items = q("SELECT * FROM tender_items WHERE tender_id = ?", (tid,))

    case = one("SELECT * FROM cases WHERE case_id = ?", (a["case_id"],)) if a["case_id"] else None
    case_tenders = q("""SELECT ta.tender_id, ta.title, ta.publish_date, ta.awarded_value,
                               ta.awarded_vendor_name, ta.tier, ta.priority_p
                        FROM case_tenders ct JOIN tender_analysis ta USING (tender_id)
                        WHERE ct.case_id = ? ORDER BY ta.priority_p DESC""",
                     (a["case_id"],)) if a["case_id"] else []

    timeline = []
    timeline.append({"date": a["publish_date"], "kind": "tender",
                     "text": f"Tender published, estimate {a['estimate_value'] or 'not published'}"})
    for b in bids:
        timeline.append({"date": (b["submitted_at"] or "")[:10], "kind": "bid",
                         "text": f"{b['legal_name']} bid {b['bid_value']:,.0f}"
                                 + (" (winner)" if b["is_winner"] else "")
                                 + (f" - disqualified: {b['disqualification_reason']}"
                                    if b["status"] == "disqualified" else "")})
    timeline.append({"date": a["award_date"], "kind": "award",
                     "text": f"Awarded to {a['awarded_vendor_name']} at {a['awarded_value']:,.0f}"})
    if contract:
        timeline.append({"date": contract["sign_date"], "kind": "contract",
                         "text": f"Contract signed, {contract['duration_days']} days"})
    for am in amendments:
        timeline.append({"date": am["date"], "kind": "amendment",
                         "text": f"Amendment: {am['description']} "
                                 f"({am['previous_value']:,.0f} to {am['new_value']:,.0f})"})
    for b in bids:
        if b["incorporation_date"]:
            timeline.append({"date": b["incorporation_date"], "kind": "registration",
                             "text": f"{b['legal_name']} incorporated"})
    timeline.sort(key=lambda x: x["date"] or "")

    return {"analysis": a, "detectors": detectors, "bids": bids, "links": links,
            "innocent": innocent, "data_quality": dq, "raw": raw, "contract": contract,
            "amendments": amendments, "items": items, "case": case,
            "case_tenders": case_tenders, "timeline": timeline}


@app.get("/api/vendor/{vendor_id}")
def vendor(vendor_id: str):
    v = one("SELECT * FROM vendors WHERE vendor_id = ?", (vendor_id,))
    if not v:
        raise HTTPException(404, "No such vendor")
    v["directors"] = q("SELECT * FROM vendor_persons WHERE vendor_id = ?", (vendor_id,))
    v["awards"] = q("""SELECT tender_id, title, buyer_name, publish_date, awarded_value, tier
                       FROM tender_analysis WHERE awarded_vendor_id = ?
                       ORDER BY publish_date DESC LIMIT 25""", (vendor_id,))
    v["bids"] = q("""SELECT b.tender_id, b.bid_value, b.is_winner, ta.buyer_name, ta.publish_date
                     FROM bids b JOIN tender_analysis ta USING (tender_id)
                     WHERE b.vendor_id = ? ORDER BY ta.publish_date DESC LIMIT 25""", (vendor_id,))
    return v


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


app.mount("/", StaticFiles(directory=STATIC), name="static")

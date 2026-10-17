"""Daily refresh of ktr-cannon-beach/data.json from Windsor.ai.

Pulls the last 7 days (re-pulled every run so late-posting purchases land),
validates against Windsor's own daily totals, merges into the existing file,
and only then writes. Any check failing exits non-zero without writing, so
the dashboard keeps its last good data and GitHub flags the failed run.

Cannon Beach rules:
- Revenue/items: GA4 property 468721899 ("KTR"), Google Ads campaign
  containing "KTR CB", transactions NOT starting "001-" (CenterEdge = other
  KTR locations; Cannon Beach sells only through Roller).
- Spend/clicks: Google Ads account 754-213-9286.
"""
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from zoneinfo import ZoneInfo

API = "https://connectors.windsor.ai"
KEY = os.environ.get("WINDSOR_API_KEY", "").strip()
DATA = os.path.join(os.path.dirname(__file__), "..", "ktr-cannon-beach", "data.json")
GA4_ACCOUNT = "468721899"
ADS_ACCOUNT = "754-213-9286"
CAMPAIGN_MARK = "KTR CB"
WINDOW_DAYS = 7


def fail(msg):
    print(f"::error::{msg}")
    sys.exit(1)


def pull(connector, account, fields, date_from, date_to):
    params = {
        "api_key": KEY,
        "date_from": date_from,
        "date_to": date_to,
        "fields": ",".join(fields),
        "select_accounts": f"{connector}__{account}",
    }
    url = f"{API}/{connector}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "ktr-reports-refresh"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            body = json.loads(r.read().decode())
    except Exception as e:  # never print the URL: it carries the key
        fail(f"Windsor request failed for {connector}: {type(e).__name__}: {e}".replace(KEY, "***"))
    rows = body.get("data") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        fail(f"Unexpected Windsor response for {connector}: keys={list(body)[:5] if isinstance(body, dict) else type(body).__name__}")
    print(f"{connector}: {len(rows)} rows, fields seen: {sorted(rows[0].keys()) if rows else '-'}")
    return rows


def ymd(v):
    s = str(v).replace("-", "")[:8]
    if len(s) != 8 or not s.isdigit():
        fail(f"Unexpected date value: {v!r}")
    return s


def num(v):
    return float(v or 0)


def fmt(v):
    v = round(float(v), 2)
    return str(int(v)) if v == int(v) else f"{v:g}"


def main():
    if not KEY:
        fail("WINDSOR_API_KEY secret is missing")
    today = dt.datetime.now(ZoneInfo("America/Phoenix")).date()
    yesterday = today - dt.timedelta(days=1)
    start = yesterday - dt.timedelta(days=WINDOW_DAYS - 1)
    d_from, d_to = start.isoformat(), yesterday.isoformat()
    s_from, s_to = ymd(d_from), ymd(d_to)
    print(f"Window {d_from} .. {d_to}")

    # 1) Line items (filtered here, not by the API, so the rule is visible and testable)
    raw = pull("googleanalytics4", GA4_ACCOUNT,
               ["date", "transactionid", "item_name", "item_revenue", "items_purchased", "session_google_ads_campaign_name"],
               d_from, d_to)
    lines = []
    for r in raw:
        camp = r.get("session_google_ads_campaign_name") or ""
        tx = str(r.get("transactionid") or "")
        if CAMPAIGN_MARK not in camp or tx.startswith("001-"):
            continue
        lines.append((ymd(r["date"]), str(r.get("item_name") or ""), tx, num(r.get("item_revenue")), int(num(r.get("items_purchased")))))

    # 2) Spend
    spend_rows = pull("google_ads", ADS_ACCOUNT, ["date", "spend", "clicks"], d_from, d_to)
    spend = {}
    for r in spend_rows:
        d = ymd(r["date"])
        s, c = spend.get(d, (0.0, 0))
        spend[d] = (s + num(r.get("spend")), c + int(num(r.get("clicks"))))

    # ---- checks ----
    if any(l[0] > s_to or l[0] < s_from for l in lines):
        fail("Line item outside the requested window")
    if s_to not in spend:
        fail(f"No Google Ads spend row for {d_to} yet; leaving dashboard unchanged")
    # Cross-check: per-day sums from the line items vs Windsor's own day totals
    totals = pull("googleanalytics4", GA4_ACCOUNT,
                  ["date", "transactionid", "item_revenue", "items_purchased", "session_google_ads_campaign_name"],
                  d_from, d_to)
    want = defaultdict(lambda: [0.0, 0])
    for r in totals:
        if CAMPAIGN_MARK in (r.get("session_google_ads_campaign_name") or "") and not str(r.get("transactionid") or "").startswith("001-"):
            want[ymd(r["date"])][0] += num(r.get("item_revenue"))
            want[ymd(r["date"])][1] += int(num(r.get("items_purchased")))
    got = defaultdict(lambda: [0.0, 0])
    for d, _, _, rev, q in lines:
        got[d][0] += rev
        got[d][1] += q
    for d in set(want) | set(got):
        if abs(want[d][0] - got[d][0]) > 0.01 or want[d][1] != got[d][1]:
            fail(f"Day {d} does not reconcile: items {got[d]} vs totals {want[d]}")

    # ---- merge ----
    with open(DATA) as f:
        cur = json.load(f)
    old_items = cur["items"]
    kept, removed = [], 0
    for x in filter(None, cur.get("rows", "").split(";")):
        d, i, t, rev, q = x.split(",")
        if d >= s_from:
            removed += 1
            continue
        kept.append((d, old_items[int(i)], f"old-{t}", float(rev), int(q)))
    merged = sorted(lines, key=lambda l: l[0], reverse=True) + sorted(kept, key=lambda l: l[0], reverse=True)
    merged.sort(key=lambda l: l[0], reverse=True)  # stable: keeps order within a day

    items, txs, out = [], {}, []
    for d, name, tx, rev, q in merged:
        if name not in items:
            items.append(name)
        if tx not in txs:
            txs[tx] = len(txs)
        out.append(f"{d},{items.index(name)},{txs[tx]},{fmt(rev)},{q}")

    old_spend = {x.split(",")[0]: x for x in filter(None, cur.get("spend", "").split(";"))}
    for d in list(old_spend):
        if d >= s_from:
            del old_spend[d]
    for d, (s, c) in spend.items():
        old_spend[d] = f"{d},{fmt(s)},{c}"

    new = {
        "updated": dt.datetime.now(ZoneInfo("America/Phoenix")).replace(microsecond=0).isoformat(),
        "items": items,
        "rows": ";".join(out),
        "spend": ";".join(old_spend[d] for d in sorted(old_spend)),
    }
    if len(out) < len(kept):
        fail("Row count shrank unexpectedly")
    with open(DATA, "w") as f:
        json.dump(new, f, separators=(",", ":"))

    yd = [l for l in lines if l[0] == s_to]
    print(f"OK: replaced {removed} rows with {len(lines)} for {d_from}..{d_to}. "
          f"Yesterday: ${sum(l[3] for l in yd):,.2f} on {len({l[2] for l in yd})} bookings; "
          f"spend ${spend[s_to][0]:,.2f}.")


if __name__ == "__main__":
    main()

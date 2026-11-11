"""Daily refresh of wrights/data.json (Wright's Gymnastics Google Ads dashboard).

Rebuilds the whole file from Sep 1, 2026 through yesterday (Arizona time) on
every run, so late-posting conversions and Google Ads restatements always land.
Any check failing exits non-zero without writing: the dashboard keeps its last
good data and GitHub flags the failed run.

Wright's rules:
- Google Ads account 440-731-8484.
- "Sent to iClassPro sign-up" = "Wright's Gymnastics (web) GMB_Clicked_iClassPro".
- "Tracked purchases" = "Wright's Gymnastics (web) purchase" (count + value).
- "Party clicks" = "Book Party Click" (the GA4-imported "(web) book_party_click"
  duplicate and its fake $150 value are ignored).
- All conversion counts use all_conversions, so switching a goal on/off in
  Google Ads does not change the dashboard. "gconv" is Google's own Conversions
  column, which does change with goal settings (shown for reference).
- "By area" uses Google's geographic report (town the searcher was in). It is
  NOT the Wright's location the family enrolled at; iClassPro doesn't send that.
- The page is client-facing: nothing written here may name the data tool.
"""
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from zoneinfo import ZoneInfo

API = "https://connectors.windsor.ai"
KEY = os.environ.get("WINDSOR_API_KEY", "").strip()
DATA = os.path.join(os.path.dirname(__file__), "..", "wrights", "data.json")
ADS_ACCOUNT = "440-731-8484"
START = dt.date(2026, 9, 1)

ACTIONS = {
    "Wright's Gymnastics (web) GMB_Clicked_iClassPro": ("iclass", False),
    "Wright's Gymnastics (web) purchase": ("purch", True),
    "Book Party Click": ("party", False),
}
# Known campaigns -> dashboard rows (order matters: the chart uses gym + ninja).
KNOWN = [
    ("gym", "Gymnastics Classes", "Search", ["Gymnastics Classes"]),
    ("ninja", "NinjaZone", "Search", ["NinjaZone"]),
    ("party", "Birthday Party", "Search", ["Wright's - Birthday Party - Search"]),
    ("display", "Display (2 campaigns)", "Display", ["[Display] - Birthday Parties", "MB | Display Retargeting"]),
]
METRICS = ["spend", "clicks", "impr", "gconv", "iclass", "purch", "pval", "party"]


def fail(msg):
    print(f"::error::{msg}")
    sys.exit(1)


def pull(fields, date_from, date_to):
    params = {
        "api_key": KEY,
        "date_from": date_from,
        "date_to": date_to,
        "fields": ",".join(fields),
        "select_accounts": ADS_ACCOUNT,
    }
    url = f"{API}/google_ads?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "ktr-reports-refresh"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:  # never print the URL: it carries the key
        detail = e.read().decode(errors="replace")[:800].replace(KEY, "***")
        fail(f"Data request failed ({', '.join(fields)}): HTTP {e.code}: {detail}")
    except Exception as e:
        fail(f"Data request failed: {type(e).__name__}: {e}".replace(KEY, "***"))
    rows = body.get("data") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        fail(f"Unexpected response: {list(body)[:5] if isinstance(body, dict) else type(body).__name__}")
    print(f"google_ads {fields}: {len(rows)} rows")
    return rows


def iso(v):
    s = str(v).replace("-", "")[:8]
    if len(s) != 8 or not s.isdigit():
        fail(f"Unexpected date value: {v!r}")
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def num(v):
    return float(v or 0)


def main():
    if not KEY:
        fail("WINDSOR_API_KEY secret is missing")
    now = dt.datetime.now(ZoneInfo("America/Phoenix"))
    yesterday = now.date() - dt.timedelta(days=1)
    d_from, d_to = START.isoformat(), yesterday.isoformat()
    print(f"Window {d_from} .. {d_to}")
    days = [(START + dt.timedelta(i)).isoformat() for i in range((yesterday - START).days + 1)]
    idx = {d: i for i, d in enumerate(days)}

    key_of, rows = {}, {}
    for key, label, channel, names in KNOWN:
        rows[key] = {"key": key, "name": label, "channel": channel, "names": names}
        for n in names:
            key_of[n] = key

    def row_for(name):
        if name not in key_of:  # a new campaign: give it its own row instead of failing
            key = f"c{len(rows)}"
            key_of[name] = key
            rows[key] = {"key": key, "name": name, "channel": "Search", "names": [name]}
        return rows[key_of[name]]

    series = defaultdict(lambda: {m: [0.0] * len(days) for m in METRICS})
    status = defaultdict(set)
    last_spend = {}

    for r in pull(["date", "campaign", "campaign_status", "spend", "clicks", "impressions", "conversions"], d_from, d_to):
        d = iso(r["date"])
        if d not in idx:
            fail(f"Row outside the requested window: {d}")
        row = row_for((r.get("campaign") or "").strip() or "(unnamed campaign)")
        s, i = series[row["key"]], idx[d]
        s["spend"][i] += num(r.get("spend"))
        s["clicks"][i] += num(r.get("clicks"))
        s["impr"][i] += num(r.get("impressions"))
        s["gconv"][i] += num(r.get("conversions"))
        status[row["key"]].add((r.get("campaign_status") or "").upper())
        if num(r.get("spend")) > 0:
            last_spend[row["key"]] = max(last_spend.get(row["key"], d), d)

    for r in pull(["date", "campaign", "conversion_action_name", "all_conversions", "all_conversions_value"], d_from, d_to):
        a = ACTIONS.get((r.get("conversion_action_name") or "").strip())
        if not a:
            continue
        d = iso(r["date"])
        if d not in idx:
            fail(f"Row outside the requested window: {d}")
        row = row_for((r.get("campaign") or "").strip() or "(unnamed campaign)")
        s, i = series[row["key"]], idx[d]
        s[a[0]][i] += num(r.get("all_conversions"))
        if a[1]:
            s["pval"][i] += num(r.get("all_conversions_value"))

    # By area: the town the parent was searching from (not the gym they enroll at).
    area_metrics = ["spend", "clicks", "iclass", "party", "purch", "pval"]
    areas = defaultdict(lambda: {m: [0.0] * len(days) for m in area_metrics})
    for r in pull(["date", "city", "spend", "clicks"], d_from, d_to):
        d = iso(r["date"])
        if d not in idx:
            fail(f"Row outside the requested window: {d}")
        a_ = areas[(r.get("city") or "").strip() or "Unknown"]
        a_["spend"][idx[d]] += num(r.get("spend"))
        a_["clicks"][idx[d]] += num(r.get("clicks"))
    for r in pull(["date", "city", "conversion_action_name", "all_conversions", "all_conversions_value"], d_from, d_to):
        act = ACTIONS.get((r.get("conversion_action_name") or "").strip())
        if not act:
            continue
        d = iso(r["date"])
        if d not in idx:
            fail(f"Row outside the requested window: {d}")
        a_ = areas[(r.get("city") or "").strip() or "Unknown"]
        a_[act[0]][idx[d]] += num(r.get("all_conversions"))
        if act[1]:
            a_["pval"][idx[d]] += num(r.get("all_conversions_value"))

    is_from = (yesterday - dt.timedelta(days=13)).isoformat()
    share = {}
    for r in pull(["campaign", "search_impression_share", "search_budget_lost_impression_share",
                   "search_rank_lost_impression_share"], is_from, d_to):
        k = key_of.get((r.get("campaign") or "").strip())
        if k:
            share[k] = {"shown": num(r.get("search_impression_share")),
                        "rank": num(r.get("search_rank_lost_impression_share")),
                        "budget": num(r.get("search_budget_lost_impression_share"))}

    # ---- checks ----
    total_spend = sum(sum(s["spend"]) for s in series.values())
    if sum(s["spend"][idx[d_to]] + s["clicks"][idx[d_to]] for s in series.values()) == 0:
        fail(f"No Google Ads activity for {d_to} yet; leaving dashboard unchanged")
    if os.path.exists(DATA):
        with open(DATA) as f:
            cur = json.load(f)
        old = sum(sum(c["spend"]) for c in cur.get("campaigns", []))
        if total_spend + 1 < old * 0.9:  # history only grows; a big drop means a bad pull
            fail(f"Total spend fell from ${old:,.2f} to ${total_spend:,.2f}; leaving dashboard unchanged")

    campaigns = []
    for key, row in rows.items():
        s = series.get(key)
        if not s or not any(sum(s[m]) for m in METRICS):
            continue
        st = status[key]
        if "ENABLED" in st:
            label = "Active"
        elif key in last_spend:
            p = dt.date.fromisoformat(last_spend[key])
            label = f"Paused {p.strftime('%b')} {p.day}"
        else:
            label = "Paused"
        campaigns.append({"key": key, "name": row["name"], "channel": row["channel"], "status": label,
                          **{m: [round(v, 2) for v in s[m]] for m in METRICS}})
    if [c["key"] for c in campaigns[:2]] != ["gym", "ninja"]:
        fail("Gymnastics Classes / NinjaZone rows missing; the class chart needs both")

    area_spend = sum(sum(v["spend"]) for v in areas.values())
    if total_spend and area_spend < total_spend * 0.5:  # town report normally covers nearly all spend
        fail(f"Town data covers only ${area_spend:,.2f} of ${total_spend:,.2f} spend; leaving dashboard unchanged")

    out = {
        "updated": now.replace(microsecond=0).isoformat(),
        "through": d_to,
        "share": share,
        "days": days,
        "campaigns": campaigns,
        "areas": [{"name": n, **{m: [round(x, 2) for x in v[m]] for m in area_metrics}}
                  for n, v in sorted(areas.items(), key=lambda kv: -sum(kv[1]["spend"]))
                  if any(sum(v[m]) for m in area_metrics)],
    }
    with open(DATA, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    y = idx[d_to]
    print(f"OK through {d_to}: {len(campaigns)} rows, ${total_spend:,.2f} total spend; yesterday "
          f"${sum(s['spend'][y] for s in series.values()):,.2f} spend, "
          f"{sum(s['purch'][y] for s in series.values()):.1f} purchases.")


if __name__ == "__main__":
    main()

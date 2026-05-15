import json
import os
import re
import requests
import anthropic
from datetime import datetime, timedelta

# ─── CONFIG ───────────────────────────────────────────────────────────────────

KEYWORDS = [
    "maintenance", "refurbishment", "kitchen", "bathroom", "playground",
    "void", "decoration", "painting", "flooring", "housing", "property",
    "open space", "estate", "building works", "alterations", "repair",
    "retrofit", "renovation", "dwelling", "residential", "social housing",
    "facilities", "fabric", "roofing", "plumbing", "damp", "insulation",
    "window", "door", "rewire", "boiler", "grounds", "caretaking",
]

COMPANY_PROFILE = """
Simpled Services Ltd — London-based property maintenance and refurbishment contractor.

GOOD FIT: kitchen/bathroom refurbishments, property maintenance, playground equipment,
open space works, internal alterations, decoration, flooring, void works, estate
maintenance, building fabric, housing association or council works.

NOT a fit: professional consultancy (architects, engineers, surveyors), large civil
infrastructure, IT/digital, works entirely outside UK.
"""

HISTORY_FILE = "docs/tenders.json"

# ─── PERSISTENCE ─────────────────────────────────────────────────────────────

def load_history():
    """Load saved tenders from previous runs."""
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                print(f"Loaded {len(data)} tenders from history")
                return data
    except Exception as e:
        print(f"Could not load history: {e}")
    return []

def save_history(tenders):
    os.makedirs("docs", exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(tenders, f, indent=2)
    print(f"Saved {len(tenders)} tenders to history")

def is_expired(deadline_str):
    """Return True if the deadline has clearly passed."""
    if not deadline_str:
        return False
    # Try ISO format first (from API)
    for fmt in ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d %B %Y", "%d/%m/%Y", "%d %b %Y"]:
        try:
            dt = datetime.strptime(deadline_str.strip()[:19], fmt[:len(deadline_str.strip()[:19])])
            return dt.date() < datetime.now().date()
        except Exception:
            pass
    # Fallback: check year
    years = re.findall(r'\b(20\d{2})\b', deadline_str)
    if years and int(years[-1]) < datetime.now().year:
        return True
    return False

def merge_tenders(existing, new_batch):
    """
    Merge new tenders into existing history.
    Use link as unique key. Keep expired ones out.
    """
    existing_by_link = {t.get("notice", t).get("link", t.get("link","")): t for t in existing}

    added = 0
    for t in new_batch:
        link = t.get("notice", {}).get("link", t.get("link", ""))
        if link and link not in existing_by_link:
            existing_by_link[link] = t
            added += 1

    # Remove expired
    before = len(existing_by_link)
    active = {k: v for k, v in existing_by_link.items()
              if not is_expired(v.get("notice", v).get("deadline", v.get("deadline", "")))}
    expired = before - len(active)

    print(f"Added {added} new, removed {expired} expired, kept {len(active)} active")
    return list(active.values())

# ─── FETCH FROM CONTRACTS FINDER ─────────────────────────────────────────────

def fetch_notices(days_back=1):
    published_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    published_to = datetime.now().strftime("%Y-%m-%d")
    url = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
    params = {
        "publishedFrom": published_from,
        "publishedTo": published_to,
        "stages": "tender,planning",
        "limit": 100,
    }

    all_releases = []
    seen = set()
    print(f"Fetching Contracts Finder ({published_from} → {published_to})...")

    while True:
        try:
            resp = requests.get(url, params=params, timeout=20,
                                headers={"Accept": "application/json"})
            print(f"  HTTP {resp.status_code}")
            if resp.status_code != 200:
                print(f"  Error: {resp.text[:200]}")
                break
            data = resp.json()
            releases = data.get("releases", [])
            if not releases:
                break
            for r in releases:
                ocid = r.get("ocid", "")
                if ocid and ocid not in seen:
                    seen.add(ocid)
                    all_releases.append(r)
            cursor = data.get("cursor")
            if not cursor or len(releases) < 100:
                break
            params["cursor"] = cursor
        except Exception as e:
            print(f"  Error: {e}")
            break

    print(f"Fetched {len(all_releases)} total releases")

    # Keyword pre-filter
    filtered = []
    for r in all_releases:
        t = r.get("tender", {})
        text = (t.get("title","") + " " + t.get("description","")).lower()
        if any(kw in text for kw in KEYWORDS):
            filtered.append(r)

    print(f"After keyword filter: {len(filtered)} to analyse\n")
    return filtered

def extract(release):
    t = release.get("tender", {})
    parties = release.get("parties", [])
    buyer = next((p for p in parties if "buyer" in p.get("roles", [])), {})
    value = t.get("value", {}).get("amount")
    deadline = t.get("tenderPeriod", {}).get("endDate", "")
    locs = t.get("deliveryLocations", [])
    loc = locs[0].get("description", "") if locs else ""

    # Build the direct link — try documents array first, then construct from ocid
    docs = t.get("documents", [])
    direct_link = next(
        (d.get("url") for d in docs
         if d.get("url") and "contractsfinder" in d.get("url","") and "/Notice/" in d.get("url","")),
        None
    )
    if not direct_link:
        ocid = release.get("ocid", "")
        notice_id = ocid.replace("ocds-b5fd17-", "")
        direct_link = f"https://www.contractsfinder.service.gov.uk/Notice/{notice_id}"

    return {
        "title":       t.get("title", "Untitled"),
        "buyer":       buyer.get("name", "Unknown"),
        "description": (t.get("description") or "")[:400],
        "value":       f"£{value:,.0f}" if value else "Not stated",
        "deadline":    deadline[:10] if deadline else "",
        "location":    loc,
        "link":        direct_link,
        "found_date":  datetime.now().strftime("%d %b %Y"),
    }

# ─── ANALYSE WITH CLAUDE ──────────────────────────────────────────────────────

def analyse_batch(notices, claude_client):
    if not notices:
        return []
    items = []
    for i, n in enumerate(notices):
        items.append(
            f"[{i}] {n['title']} | Buyer: {n['buyer']} | "
            f"Location: {n['location'] or 'unknown'} | Value: {n['value']} | "
            f"Desc: {n['description'][:120]}"
        )
    prompt = f"""Assess each tender for Simpled Services Ltd.
{COMPANY_PROFILE}

Tenders:
{chr(10).join(items)}

Return ONLY a JSON array (same order):
[{{"index":0,"isMatch":true,"priority":"high/medium/low","reason":"max 12 words","tenderType":"live or prior_notice"}}]"""

    try:
        msg = claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text.strip().replace("```json","").replace("```","").strip()
        start, end = text.find("["), text.rfind("]")
        if start != -1 and end != -1:
            return json.loads(text[start:end+1])
    except Exception as e:
        print(f"  Claude error: {e}")
    return []

# ─── BUILD HTML ───────────────────────────────────────────────────────────────

def build_html(matches, total_ever):
    now = datetime.now()
    count = len(matches)
    COLS = {
        "high":   ("#3B6D11","#EAF3DE","#1D9E75","High"),
        "medium": ("#854F0B","#FAEEDA","#EF9F27","Medium"),
        "low":    ("#185FA5","#E6F1FB","#378ADD","Low"),
    }

    def deadline_urgency(dl):
        if not dl:
            return ""
        try:
            dt = datetime.strptime(dl[:10], "%Y-%m-%d")
            days = (dt.date() - datetime.now().date()).days
            if days <= 3:   return f'<span style="background:#FCEBEB;color:#A32D2D;font-size:10px;padding:2px 6px;border-radius:4px;margin-left:6px;">⚠ {days}d left</span>'
            if days <= 7:   return f'<span style="background:#FAEEDA;color:#854F0B;font-size:10px;padding:2px 6px;border-radius:4px;margin-left:6px;">{days}d left</span>'
            if days <= 14:  return f'<span style="background:#E6F1FB;color:#185FA5;font-size:10px;padding:2px 6px;border-radius:4px;margin-left:6px;">{days}d left</span>'
        except Exception:
            pass
        return ""

    def format_deadline(dl):
        if not dl:
            return "Check portal"
        try:
            dt = datetime.strptime(dl[:10], "%Y-%m-%d")
            return dt.strftime("%d %b %Y")
        except Exception:
            return dl

    def card(m):
        p = m.get("priority","medium")
        tc,bg,bc,pl = COLS.get(p, COLS["medium"])
        tl = "Prior Notice" if m.get("tenderType")=="prior_notice" else "Live Tender"
        n = m["notice"]
        dl_fmt = format_deadline(n.get("deadline",""))
        urgency = deadline_urgency(n.get("deadline",""))
        found = n.get("found_date","")
        link = n.get("link","#")
        return f"""<div class="card" style="border-left:4px solid {bc}">
  <div class="meta">
    <span class="badge" style="background:{bg};color:{tc}">{pl}</span>
    <span class="badge b2">{tl}</span>
    {f'<span class="badge b2">Found {found}</span>' if found else ''}
  </div>
  <h3>{n['title']}</h3>
  <table>
    <tr><td>Buyer</td><td><b>{n['buyer']}</b></td></tr>
    <tr><td>Location</td><td>{n['location'] or 'Not specified'}</td></tr>
    <tr><td>Value</td><td>{n['value']}</td></tr>
    <tr><td>Deadline</td><td>{dl_fmt}{urgency}</td></tr>
    <tr><td>Why</td><td style="color:{tc};font-style:italic">{m.get('reason','')}</td></tr>
  </table>
  {f'<p class="desc">{n["description"][:220]}</p>' if n.get('description') else ''}
  <a class="btn" href="{link}" target="_blank" rel="noopener noreferrer">Open on Contracts Finder &rarr;</a>
</div>"""

    sections = ""
    for pk in ["high","medium","low"]:
        group = [m for m in matches if m.get("priority")==pk]
        if group:
            tc = COLS[pk][0]
            sections += f'<h2 class="sh" style="color:{tc}">{COLS[pk][3]} priority ({len(group)})</h2>'
            sections += "".join(card(m) for m in group)

    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Simpled Tender Bot</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;color:#1a1a1a;line-height:1.5}}
.hdr{{background:#fff;border-bottom:3px solid #185FA5;padding:18px 24px;position:sticky;top:0;z-index:9;box-shadow:0 1px 4px rgba(0,0,0,.06)}}
.hdr h1{{font-size:18px;color:#185FA5;font-weight:700}}.hdr p{{font-size:12px;color:#999;margin-top:2px}}
.stats{{display:flex;gap:10px;padding:14px 24px;background:#fff;border-bottom:1px solid #eee;flex-wrap:wrap}}
.stat{{background:#f5f5f5;border-radius:8px;padding:10px 16px;min-width:80px}}
.sl{{font-size:10px;color:#999;text-transform:uppercase;letter-spacing:.04em}}
.sv{{font-size:22px;font-weight:700}}
.content{{max-width:740px;margin:0 auto;padding:20px 16px}}
.sh{{font-size:11px;font-weight:700;margin:24px 0 8px;text-transform:uppercase;letter-spacing:.06em}}
.card{{background:#fff;border:1px solid #e5e5e5;border-radius:10px;padding:16px;margin-bottom:10px;transition:box-shadow .15s}}
.card:hover{{box-shadow:0 2px 12px rgba(0,0,0,.08)}}
.card h3{{font-size:14px;font-weight:600;margin:8px 0 10px;line-height:1.4}}
.meta{{display:flex;gap:5px;flex-wrap:wrap;align-items:center}}
.badge{{font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px;text-transform:uppercase;letter-spacing:.03em;white-space:nowrap}}
.b2{{background:#f0f0f0;color:#666}}
table{{width:100%;border-collapse:collapse;font-size:12px;margin:10px 0}}
td{{padding:3px 0;vertical-align:top}}
td:first-child{{color:#999;width:70px;padding-right:8px;white-space:nowrap}}
.desc{{font-size:12px;color:#666;margin:8px 0 12px;line-height:1.5}}
.btn{{display:inline-block;background:#185FA5;color:#fff !important;padding:7px 16px;border-radius:6px;font-size:12px;font-weight:600;text-decoration:none;cursor:pointer}}
.btn:hover{{background:#0c447c}}
.none{{color:#999;font-size:13px;text-align:center;margin:40px 0;padding:40px}}
.ftr{{text-align:center;font-size:11px;color:#ccc;padding:24px}}
@media(max-width:560px){{.stats{{gap:8px}}.content{{padding:12px 10px}}}}
</style></head><body>
<div class="hdr">
  <h1>&#128270; Simpled Tender Bot</h1>
  <p>{count} active match{'es' if count!=1 else ''} &middot; Updated {now.strftime("%d %b %Y at %H:%M UTC")}</p>
</div>
<div class="stats">
  <div class="stat"><div class="sl">Active</div><div class="sv">{count}</div></div>
  <div class="stat"><div class="sl">High</div><div class="sv" style="color:#3B6D11">{len([m for m in matches if m.get('priority')=='high'])}</div></div>
  <div class="stat"><div class="sl">Medium</div><div class="sv" style="color:#854F0B">{len([m for m in matches if m.get('priority')=='medium'])}</div></div>
  <div class="stat"><div class="sl">Low</div><div class="sv" style="color:#185FA5">{len([m for m in matches if m.get('priority')=='low'])}</div></div>
</div>
<div class="content">
{'<div class="none">No active matches right now. Check back tomorrow.</div>' if not matches else sections}
</div>
<div class="ftr">Runs daily &middot; Simpled Services Ltd &middot; contractsfinder.service.gov.uk</div>
</body></html>"""

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    days_back = int(os.environ.get("DAYS_BACK", "1"))
    if not anthropic_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    claude_client = anthropic.Anthropic(api_key=anthropic_key)
    print(f"\n{'='*50}")
    print(f"Simpled Tender Bot — {datetime.now().strftime('%d %b %Y %H:%M')}")
    print(f"{'='*50}\n")

    # Load history
    history = load_history()

    # Fetch and analyse today's tenders
    releases = fetch_notices(days_back)
    notices = [extract(r) for r in releases]

    BATCH = 20
    new_matches = []
    for i in range(0, len(notices), BATCH):
        batch = notices[i:i+BATCH]
        print(f"Analysing batch {i//BATCH+1} ({len(batch)} tenders)...")
        results = analyse_batch(batch, claude_client)
        for r in results:
            idx = r.get("index", 0)
            if 0 <= idx < len(batch) and r.get("isMatch"):
                new_matches.append({**r, "notice": batch[idx]})

    # Merge into history and remove expired
    updated_history = merge_tenders(history, new_matches)
    save_history(updated_history)

    # Sort for display
    updated_history.sort(key=lambda m: {"high":0,"medium":1,"low":2}.get(m.get("priority","low"),1))

    print(f"\n{'─'*50}")
    print(f"{len(updated_history)} total active matches on dashboard")
    for m in updated_history:
        print(f"  [{m.get('priority','?').upper()}] {m['notice']['title'][:55]}")
    print(f"{'─'*50}\n")

    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(build_html(updated_history, len(updated_history)))
    print("Dashboard written to docs/index.html")

if __name__ == "__main__":
    main()

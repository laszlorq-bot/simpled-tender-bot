import json
import os
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
    "window", "door", "rewire", "boiler", "grounds", "caretaking", "cleaning",
]

COMPANY_PROFILE = """
Simpled Services Ltd — London-based property maintenance and refurbishment contractor.

GOOD FIT: kitchen/bathroom refurbishments, property maintenance, playground equipment,
open space works, internal alterations, decoration, flooring, void works, estate
maintenance, building fabric, housing association or council works.

NOT a fit: professional consultancy (architects, engineers, surveyors), large civil
infrastructure, IT/digital, works entirely outside UK.
"""

# ─── FETCH FROM CONTRACTS FINDER ─────────────────────────────────────────────

def fetch_notices(days_back=1):
    base = "https://www.contractsfinder.service.gov.uk"
    published_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    published_to = datetime.now().strftime("%Y-%m-%d")

    url = f"{base}/Published/Notices/OCDS/Search"
    params = {
        "publishedFrom": published_from,
        "publishedTo": published_to,
        "stages": "tender,planning",
        "limit": 100,
    }

    all_releases = []
    seen = set()

    print(f"Fetching from Contracts Finder ({published_from} to {published_to})...")

    while True:
        try:
            resp = requests.get(url, params=params, timeout=20,
                                headers={"Accept": "application/json"})
            print(f"  HTTP {resp.status_code}")

            if resp.status_code != 200:
                print(f"  Error: {resp.text[:300]}")
                break

            data = resp.json()
            releases = data.get("releases", [])
            print(f"  Got {len(releases)} releases")

            if not releases:
                break

            for r in releases:
                ocid = r.get("ocid", "")
                if ocid and ocid not in seen:
                    seen.add(ocid)
                    all_releases.append(r)

            # Pagination via cursor
            cursor = data.get("cursor")
            if not cursor or len(releases) < 100:
                break
            params["cursor"] = cursor

        except Exception as e:
            print(f"  Fetch error: {e}")
            break

    print(f"Total fetched: {len(all_releases)}")

    # Keyword pre-filter
    filtered = []
    for r in all_releases:
        t = r.get("tender", {})
        text = (t.get("title", "") + " " + t.get("description", "")).lower()
        if any(kw in text for kw in KEYWORDS):
            filtered.append(r)

    print(f"After keyword filter: {len(filtered)} to analyse\n")
    return filtered

# ─── EXTRACT FIELDS ───────────────────────────────────────────────────────────

def extract(release):
    t = release.get("tender", {})
    parties = release.get("parties", [])
    buyer = next((p for p in parties if "buyer" in p.get("roles", [])), {})
    value = t.get("value", {}).get("amount")
    deadline = t.get("tenderPeriod", {}).get("endDate", "")
    locs = t.get("deliveryLocations", [])
    loc = locs[0].get("description", "") if locs else ""
    ocid = release.get("ocid", "")
    notice_id = ocid.replace("ocds-b5fd17-", "")
    return {
        "title":  t.get("title", "Untitled"),
        "buyer":  buyer.get("name", "Unknown"),
        "description": (t.get("description") or "")[:400],
        "value":  f"£{value:,.0f}" if value else "Not stated",
        "deadline": deadline[:10] if deadline else "",
        "location": loc,
        "link": f"https://www.contractsfinder.service.gov.uk/Notice/{notice_id}",
    }

# ─── ANALYSE WITH CLAUDE ──────────────────────────────────────────────────────

def analyse_batch(notices, claude_client):
    """Send up to 20 notices to Claude in one call to save tokens."""
    if not notices:
        return []

    items = []
    for i, n in enumerate(notices):
        items.append(
            f"[{i}] Title: {n['title']}\n"
            f"    Buyer: {n['buyer']}\n"
            f"    Location: {n['location'] or 'unknown'}\n"
            f"    Value: {n['value']}\n"
            f"    Deadline: {n['deadline'] or 'unknown'}\n"
            f"    Description: {n['description'][:150]}"
        )

    prompt = f"""Assess each tender below for Simpled Services Ltd.
{COMPANY_PROFILE}

Tenders:
{chr(10).join(items)}

Return ONLY a JSON array with one object per tender (same order, same indexes):
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
        print(f"  Claude batch error: {e}")
    return []

# ─── BUILD HTML DASHBOARD ────────────────────────────────────────────────────

def build_html(matches, total_scanned):
    now = datetime.now()
    date_str = now.strftime("%d %B %Y")
    time_str = now.strftime("%H:%M UTC")
    count = len(matches)

    COLS = {
        "high":   ("#3B6D11","#EAF3DE","#1D9E75"),
        "medium": ("#854F0B","#FAEEDA","#EF9F27"),
        "low":    ("#185FA5","#E6F1FB","#378ADD"),
    }

    def card(m):
        p = m.get("priority","medium")
        tc,bg,bc = COLS.get(p, COLS["medium"])
        tl = "Prior Notice" if m.get("tenderType")=="prior_notice" else "Live Tender"
        n = m["notice"]
        return f"""<div class="card" style="border-left:4px solid {bc}">
  <div class="meta"><span class="badge" style="background:{bg};color:{tc}">{p.upper()}</span><span class="badge b2">{tl}</span></div>
  <h3>{n['title']}</h3>
  <table><tr><td>Buyer</td><td><b>{n['buyer']}</b></td></tr>
  <tr><td>Location</td><td>{n['location'] or 'Not specified'}</td></tr>
  <tr><td>Value</td><td>{n['value']}</td></tr>
  <tr><td>Deadline</td><td>{n['deadline'] or 'Check portal'}</td></tr>
  <tr><td>Why</td><td style="color:{tc};font-style:italic">{m.get('reason','')}</td></tr></table>
  {f'<p class="desc">{n["description"][:200]}</p>' if n.get('description') else ''}
  <a class="btn" href="{n['link']}" target="_blank">View tender &rarr;</a>
</div>"""

    sections = ""
    for pk in ["high","medium","low"]:
        group = [m for m in matches if m.get("priority")==pk]
        if group:
            tc = COLS[pk][0]
            sections += f'<h2 class="sh" style="color:{tc}">{pk.title()} priority ({len(group)})</h2>'
            sections += "".join(card(m) for m in group)

    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Simpled Tender Bot</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;color:#1a1a1a;line-height:1.5}}
.hdr{{background:#fff;border-bottom:3px solid #185FA5;padding:18px 24px;position:sticky;top:0;z-index:9}}
.hdr h1{{font-size:19px;color:#185FA5;font-weight:600}}.hdr p{{font-size:12px;color:#999;margin-top:2px}}
.stats{{display:flex;gap:10px;padding:14px 24px;background:#fff;border-bottom:1px solid #eee;flex-wrap:wrap}}
.stat{{background:#f5f5f5;border-radius:8px;padding:10px 16px;min-width:85px}}
.stat-l{{font-size:11px;color:#999;text-transform:uppercase;letter-spacing:.04em}}
.stat-v{{font-size:22px;font-weight:600}}
.content{{max-width:740px;margin:0 auto;padding:20px 16px}}
.sh{{font-size:12px;font-weight:700;margin:22px 0 8px;text-transform:uppercase;letter-spacing:.05em}}
.card{{background:#fff;border:1px solid #e5e5e5;border-radius:10px;padding:16px;margin-bottom:10px}}
.card h3{{font-size:14px;font-weight:600;margin:8px 0 10px;line-height:1.4}}
.meta{{display:flex;gap:6px;flex-wrap:wrap}}
.badge{{font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px;text-transform:uppercase;letter-spacing:.03em}}
.b2{{background:#f0f0f0;color:#555}}
table{{width:100%;border-collapse:collapse;font-size:12px;margin:10px 0}}
td{{padding:3px 0;vertical-align:top}}td:first-child{{color:#999;width:75px;padding-right:8px}}
.desc{{font-size:12px;color:#666;margin:8px 0 12px;line-height:1.5}}
.btn{{display:inline-block;background:#185FA5;color:#fff;padding:7px 16px;border-radius:5px;font-size:12px;font-weight:600;text-decoration:none}}
.btn:hover{{background:#0c447c}}
.none{{color:#999;font-size:13px;text-align:center;margin:40px 0}}
.ftr{{text-align:center;font-size:11px;color:#ccc;padding:20px}}
@media(max-width:560px){{.stats{{gap:8px}}.content{{padding:14px 10px}}}}
</style></head><body>
<div class="hdr"><h1>Simpled Tender Bot</h1>
<p>{count} match{'es' if count!=1 else ''} from {total_scanned} scanned &middot; {date_str} at {time_str}</p></div>
<div class="stats">
<div class="stat"><div class="stat-l">Scanned</div><div class="stat-v">{total_scanned}</div></div>
<div class="stat"><div class="stat-l">Matches</div><div class="stat-v">{count}</div></div>
<div class="stat"><div class="stat-l">High</div><div class="stat-v" style="color:#3B6D11">{len([m for m in matches if m.get('priority')=='high'])}</div></div>
<div class="stat"><div class="stat-l">Medium</div><div class="stat-v" style="color:#854F0B">{len([m for m in matches if m.get('priority')=='medium'])}</div></div>
</div>
<div class="content">
{'<p class="none">No matches today. Check back tomorrow.</p>' if not matches else sections}
</div>
<div class="ftr">Daily scan &middot; Simpled Services Ltd &middot; contractsfinder.service.gov.uk</div>
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

    releases = fetch_notices(days_back)
    notices = [extract(r) for r in releases]

    # Analyse in batches of 20
    BATCH = 20
    all_results = []
    for i in range(0, len(notices), BATCH):
        batch = notices[i:i+BATCH]
        print(f"Analysing batch {i//BATCH+1} ({len(batch)} tenders)...")
        results = analyse_batch(batch, claude_client)
        for r in results:
            idx = r.get("index", 0)
            if 0 <= idx < len(batch) and r.get("isMatch"):
                all_results.append({**r, "notice": batch[idx]})

    all_results.sort(key=lambda m: {"high":0,"medium":1,"low":2}.get(m.get("priority","low"),1))

    print(f"\n{'─'*50}")
    print(f"{len(all_results)} matches from {len(notices)} tenders")
    for m in all_results:
        print(f"  [{m.get('priority','?').upper()}] {m['notice']['title'][:55]}")
    print(f"{'─'*50}\n")

    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(build_html(all_results, len(notices)))
    print("Dashboard written to docs/index.html")

if __name__ == "__main__":
    main()

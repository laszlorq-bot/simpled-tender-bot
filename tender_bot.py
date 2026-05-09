import requests
import json
import os
import anthropic
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta

COMPANY_PROFILE = """
Simpled Services Ltd is a London-based property maintenance and refurbishment contractor.

GOOD FIT: kitchen refurbishments, bathroom refurbishments, general property maintenance,
playground equipment supply/installation, open space refurbishment, internal alterations,
decoration and painting, flooring, void property works, estate maintenance, building fabric
works, housing association or council property works.

NOT a fit: professional consultancy (architects, engineers, surveyors), large civil
infrastructure, IT/digital services, works entirely outside UK.
"""

KEYWORDS = [
    "maintenance", "refurbishment", "kitchen", "bathroom", "playground",
    "void", "decoration", "painting", "flooring", "housing", "property",
    "open space", "estate", "building works", "alterations", "repair",
    "retrofit", "renovation", "dwelling", "residential", "social housing",
    "facilities", "fabric", "roofing", "plumbing", "damp", "insulation",
    "window", "door", "rewire", "boiler", "grounds",
]

def fetch_contracts_finder(days_back=1):
    """Fetch from Contracts Finder API."""
    from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%dT00:00:00")
    url = "https://www.contractsfinder.service.gov.uk/api/rest/2/search_summaries"

    payload = {
        "searchCriteria": {
            "publishedFrom": from_date,
            "types": ["Contract Notice", "Prior Information Notice"],
            "size": 100,
            "page": 1,
        }
    }

    all_notices = []
    page = 1

    print(f"Fetching from Contracts Finder (from {from_date[:10]})...")

    while True:
        payload["searchCriteria"]["page"] = page
        try:
            resp = requests.post(url, json=payload, timeout=20,
                                 headers={"Content-Type": "application/json"})
            print(f"  Page {page}: HTTP {resp.status_code}")

            if resp.status_code != 200:
                print(f"  Error: {resp.text[:200]}")
                break

            data = resp.json()
            notices = data.get("results", [])
            print(f"  Got {len(notices)} notices")

            if not notices:
                break

            all_notices.extend(notices)

            total = data.get("totalFound", 0)
            if len(all_notices) >= total or len(notices) < 100:
                break

            page += 1

        except Exception as e:
            print(f"  Error: {e}")
            break

    print(f"\nTotal fetched: {len(all_notices)}")

    if not all_notices:
        return []

    # Pre-filter by keyword
    filtered = []
    for n in all_notices:
        title = (n.get("title") or "").lower()
        desc = (n.get("description") or "").lower()
        text = title + " " + desc
        if any(kw in text for kw in KEYWORDS):
            filtered.append(n)

    print(f"After keyword filter: {len(filtered)} to analyse\n")
    return filtered

def fetch_find_a_tender(days_back=1):
    """Fetch from Find a Tender API as secondary source."""
    from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    url = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
    all_releases = []
    seen_ids = set()

    print(f"Fetching from Find a Tender (from {from_date})...")

    try:
        resp = requests.get(url, params={"publishedFrom": from_date, "limit": 100}, timeout=20)
        print(f"  HTTP {resp.status_code}")

        if resp.status_code == 200:
            data = resp.json()
            releases = data.get("releases", [])
            print(f"  Got {len(releases)} releases")
            for r in releases:
                ocid = r.get("ocid", "")
                if ocid and ocid not in seen_ids:
                    seen_ids.add(ocid)
                    all_releases.append(r)
    except Exception as e:
        print(f"  Error: {e}")

    print(f"Find a Tender total: {len(all_releases)}")

    # Convert to same format as Contracts Finder
    converted = []
    for r in all_releases:
        t = r.get("tender", {})
        parties = r.get("parties", [])
        buyer = next((p for p in parties if "buyer" in p.get("roles", [])), {})
        title = t.get("title", "")
        desc = t.get("description", "")
        text = (title + " " + desc).lower()
        if any(kw in text for kw in KEYWORDS):
            value = t.get("value", {}).get("amount")
            deadline = t.get("tenderPeriod", {}).get("endDate", "")
            locs = t.get("deliveryLocations", [])
            ocid = r.get("ocid", "")
            notice_id = ocid.replace("ocds-b5fd17-", "")
            converted.append({
                "_source": "fat",
                "title": title,
                "description": desc[:400],
                "organisationName": buyer.get("name", "Unknown"),
                "value": f"£{value:,.0f}" if value else "Not specified",
                "closingDate": deadline[:10] if deadline else "",
                "location": locs[0].get("description", "") if locs else "",
                "link": f"https://www.find-tender.service.gov.uk/Notice/{notice_id}",
                "noticeType": "Prior Information Notice" if t.get("status") == "planned" else "Contract Notice",
            })

    return converted

def normalise(notice):
    """Normalise a Contracts Finder notice into a standard dict."""
    if notice.get("_source") == "fat":
        return notice

    org = notice.get("organisationName") or notice.get("organisation", {}).get("name", "Unknown")
    value_obj = notice.get("value") or {}
    value = value_obj.get("amount") if isinstance(value_obj, dict) else None
    deadline = notice.get("closeDate") or notice.get("closingDate") or ""
    if deadline and len(deadline) > 10:
        deadline = deadline[:10]

    # Build link
    notice_id = notice.get("id") or notice.get("noticeIdentifier") or ""
    link = f"https://www.contractsfinder.service.gov.uk/Notice/{notice_id}" if notice_id else "https://www.contractsfinder.service.gov.uk"

    return {
        "title": notice.get("title", "Untitled"),
        "description": (notice.get("description") or "")[:400],
        "organisationName": org,
        "value": f"£{value:,.0f}" if value else "Not specified",
        "closingDate": deadline,
        "location": notice.get("location") or notice.get("locationDescription") or "",
        "link": link,
        "noticeType": notice.get("noticeType") or notice.get("type") or "Contract Notice",
    }

def analyse(details, claude_client):
    prompt = f"""Tender:
Title: {details['title']}
Buyer: {details['organisationName']}
Location: {details['location'] or 'Not specified'}
Value: {details['value']}
Deadline: {details['closingDate'] or 'Check portal'}
Type: {details['noticeType']}
Description: {details['description']}

Good fit for Simpled Services Ltd? Return ONLY valid JSON:
{{"isMatch": true or false, "reason": "max 15 words", "priority": "high or medium or low", "tenderType": "live or prior_notice"}}"""

    try:
        msg = claude_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=200,
            system=f"Assess tenders for a property maintenance contractor. Return only valid JSON.\n{COMPANY_PROFILE}",
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text.strip().replace("```json","").replace("```","").strip()
        return json.loads(text)
    except Exception as e:
        print(f"    Claude error: {e}")
        return {"isMatch": False, "reason": "error", "priority": "low", "tenderType": "live"}

def send_email(matches, sender_email, sender_password, recipient_email):
    date_str = datetime.now().strftime("%d %B %Y")
    count = len(matches)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Simpled Tender Bot — {count} match{'es' if count!=1 else ''} · {date_str}"
    msg["From"] = f"Simpled Tender Bot <{sender_email}>"
    msg["To"] = recipient_email

    def card(m):
        p = m.get("priority","medium")
        cols = {"high":("#3B6D11","#EAF3DE","#1D9E75"),"medium":("#854F0B","#FAEEDA","#EF9F27"),"low":("#185FA5","#E6F1FB","#378ADD")}
        tc,bg,bc = cols.get(p,cols["medium"])
        tl = "Prior Notice" if m.get("tenderType")=="prior_notice" else "Live Tender"
        d = m.get("details",{})
        desc = d.get("description","")
        return f"""<div style="border:1px solid #ddd;border-left:4px solid {bc};border-radius:6px;padding:16px;margin:12px 0;background:#fff;">
<div style="margin-bottom:8px;"><span style="background:{bg};color:{tc};font-size:11px;font-weight:700;padding:3px 8px;border-radius:4px;text-transform:uppercase;margin-right:6px;">{p}</span><span style="background:#f0f0f0;color:#555;font-size:11px;padding:3px 8px;border-radius:4px;">{tl}</span></div>
<h3 style="margin:0 0 10px;font-size:15px;color:#1a1a1a;">{d.get('title','')}</h3>
<table style="font-size:13px;color:#555;border-collapse:collapse;margin-bottom:10px;width:100%;">
<tr><td style="padding:3px 0;width:75px;color:#999;font-size:12px;">Buyer</td><td style="font-weight:600;color:#333;">{d.get('organisationName','')}</td></tr>
<tr><td style="padding:3px 0;color:#999;font-size:12px;">Location</td><td>{d.get('location','') or 'Not specified'}</td></tr>
<tr><td style="padding:3px 0;color:#999;font-size:12px;">Value</td><td>{d.get('value','')}</td></tr>
<tr><td style="padding:3px 0;color:#999;font-size:12px;">Deadline</td><td>{d.get('closingDate','') or 'Check portal'}</td></tr>
<tr><td style="padding:3px 0;color:#999;font-size:12px;">Why</td><td style="color:{tc};font-style:italic;">{m.get('reason','')}</td></tr>
</table>
{f'<p style="font-size:12px;color:#666;margin:0 0 12px;line-height:1.5;">{desc[:250]}{"..." if len(desc)>250 else ""}</p>' if desc else ''}
<a href="{d.get('link','#')}" style="background:#185FA5;color:#fff;padding:8px 16px;text-decoration:none;border-radius:5px;font-size:12px;font-weight:600;display:inline-block;">View tender →</a>
</div>"""

    high = [m for m in matches if m.get("priority")=="high"]
    medium = [m for m in matches if m.get("priority")=="medium"]
    low = [m for m in matches if m.get("priority")=="low"]

    sections = ""
    if high: sections += f'<h3 style="color:#3B6D11;margin:20px 0 4px;font-size:14px;">High priority ({len(high)})</h3>'+"".join(card(m) for m in high)
    if medium: sections += f'<h3 style="color:#854F0B;margin:20px 0 4px;font-size:14px;">Medium priority ({len(medium)})</h3>'+"".join(card(m) for m in medium)
    if low: sections += f'<h3 style="color:#185FA5;margin:20px 0 4px;font-size:14px;">Low priority ({len(low)})</h3>'+"".join(card(m) for m in low)

    html = f"""<html><body style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;padding:20px;background:#f5f5f5;">
<div style="background:#fff;border-radius:8px;padding:24px;border:1px solid #e0e0e0;">
<div style="border-bottom:3px solid #185FA5;padding-bottom:12px;margin-bottom:8px;">
<h2 style="margin:0 0 4px;color:#185FA5;font-size:20px;">Simpled Tender Bot</h2>
<p style="margin:0;color:#999;font-size:12px;">{count} match{'es' if count!=1 else ''} · {date_str} · contractsfinder.service.gov.uk</p>
</div>
{sections}
<p style="color:#ccc;font-size:11px;margin-top:20px;">Automated daily scan for Simpled Services Ltd</p>
</div></body></html>"""

    msg.attach(MIMEText(html,"html"))
    with smtplib.SMTP_SSL("smtp.gmail.com",465) as s:
        s.login(sender_email,sender_password)
        s.sendmail(sender_email,recipient_email,msg.as_string())
    print(f"Email sent to {recipient_email}")

def main():
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    sender_email = os.environ.get("SENDER_EMAIL")
    sender_password = os.environ.get("SENDER_PASSWORD")
    recipient_email = os.environ.get("RECIPIENT_EMAIL", sender_email)
    days_back = int(os.environ.get("DAYS_BACK","1"))

    if not anthropic_key: raise ValueError("ANTHROPIC_API_KEY not set")
    if not sender_email or not sender_password: raise ValueError("Email secrets not set")

    claude_client = anthropic.Anthropic(api_key=anthropic_key)

    print(f"\n{'='*50}")
    print(f"Simpled Tender Bot — {datetime.now().strftime('%d %b %Y %H:%M')}")
    print(f"{'='*50}\n")

    # Fetch from both sources
    cf_notices = fetch_contracts_finder(days_back)
    fat_notices = fetch_find_a_tender(days_back)
    all_notices = cf_notices + fat_notices

    if not all_notices:
        print("No relevant tenders found today. Exiting.")
        return

    print(f"Analysing {len(all_notices)} tenders with Claude...\n")
    matches = []

    for i, raw in enumerate(all_notices, 1):
        details = normalise(raw)
        print(f"[{i}/{len(all_notices)}] {details['title'][:50]}...", end=" ", flush=True)
        result = analyse(details, claude_client)
        if result.get("isMatch"):
            matches.append({**result, "details": details})
            print(f"MATCH ({result.get('priority','?')})")
        else:
            print("skip")

    print(f"\n{'─'*50}")
    print(f"{len(matches)} matches from {len(all_notices)} tenders")
    print(f"{'─'*50}\n")

    if matches:
        matches.sort(key=lambda m: {"high":0,"medium":1,"low":2}.get(m.get("priority","low"),1))
        send_email(matches, sender_email, sender_password, recipient_email)
    else:
        print("No matches today — no email sent.")

if __name__ == "__main__":
    main()

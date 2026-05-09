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

GOOD FIT tenders:
- Kitchen refurbishments or replacements
- Bathroom refurbishments or replacements
- General property maintenance and repairs
- Playground equipment supply and installation
- Open space refurbishment
- Internal alterations
- Decoration and painting
- Flooring
- Void property works
- Estate maintenance
- Building fabric works
- Housing association or council property works

NOT a good fit:
- Professional/consultancy services (architects, engineers, quantity surveyors, project managers)
- Large civil infrastructure (roads, bridges)
- IT or digital services
- Works entirely outside the UK
- Specialist mechanical or electrical engineering only
"""

KEYWORDS = [
    "maintenance", "refurbishment", "kitchen", "bathroom", "playground",
    "void", "decoration", "painting", "flooring", "housing", "property",
    "open space", "estate", "building works", "alterations", "repair",
    "retrofit", "renovation", "dwelling", "residential", "social housing",
    "grounds", "facilities", "fabric", "window", "door", "roofing",
    "plumbing", "electrical", "rewire", "boiler", "damp", "insulation",
]

def fetch_tenders(days_back=1):
    from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    base_url = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
    all_releases = []
    seen_ids = set()
    skip = 0
    limit = 100

    print(f"Scanning Find a Tender from {from_date}...")

    while True:
        params = {"publishedFrom": from_date, "limit": limit, "skip": skip}
        try:
            resp = requests.get(base_url, params=params, timeout=20)
            print(f"  Page {skip // limit + 1}: HTTP {resp.status_code}")
            if resp.status_code != 200:
                print(f"  API error: {resp.text[:300]}")
                break
            data = resp.json()
            releases = data.get("releases", [])
            print(f"  Got {len(releases)} releases")
            if not releases:
                break
            for r in releases:
                ocid = r.get("ocid", "")
                if ocid and ocid not in seen_ids:
                    seen_ids.add(ocid)
                    all_releases.append(r)
            if len(releases) < limit:
                break
            skip += limit
        except Exception as e:
            print(f"  Fetch error: {e}")
            break

    print(f"\nTotal unique tenders fetched: {len(all_releases)}")
    if not all_releases:
        return []

    filtered = []
    for t in all_releases:
        tender = t.get("tender", {})
        title = tender.get("title", "").lower()
        desc = tender.get("description", "").lower()
        text = title + " " + desc
        if any(kw in text for kw in KEYWORDS):
            filtered.append(t)

    print(f"After keyword filter: {len(filtered)} tenders to analyse\n")
    return filtered

def extract_details(release):
    tender = release.get("tender", {})
    parties = release.get("parties", [])
    buyer = next((p for p in parties if "buyer" in p.get("roles", [])), {})
    buyer_name = buyer.get("name", "Unknown buyer")
    title = tender.get("title", "Untitled")
    description = tender.get("description", "")
    value = tender.get("value", {}).get("amount")
    currency = tender.get("value", {}).get("currency", "GBP")
    deadline = tender.get("tenderPeriod", {}).get("endDate", "")
    locations = tender.get("deliveryLocations", [])
    location = locations[0].get("description", "") if locations else ""
    ocid = release.get("ocid", "")
    notice_id = ocid.replace("ocds-b5fd17-", "") if ocid else ""
    documents = tender.get("documents", [])
    portal_link = next(
        (d.get("url") for d in documents if d.get("url") and "find-tender" in d.get("url", "")),
        f"https://www.find-tender.service.gov.uk/Notice/{notice_id}" if notice_id else "https://www.find-tender.service.gov.uk"
    )
    return {
        "title": title, "buyer": buyer_name, "description": description[:400],
        "value": f"£{value:,.0f} {currency}" if value else "Not specified",
        "deadline": deadline[:10] if deadline else "Check portal",
        "location": location, "link": portal_link, "ocid": ocid,
    }

def analyse_tender(details, claude_client):
    prompt = f"""Tender details:
Title: {details['title']}
Buyer: {details['buyer']}
Location: {details['location'] or 'Not specified'}
Value: {details['value']}
Deadline: {details['deadline']}
Description: {details['description']}

Is this a good fit for Simpled Services Ltd? Return ONLY valid JSON:
{{"isMatch": true or false, "reason": "max 15 words", "priority": "high, medium, or low", "tenderType": "live or prior_notice"}}"""
    try:
        message = claude_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=200,
            system=f"You assess tender opportunities for a property maintenance contractor. Return only valid JSON.\n\n{COMPANY_PROFILE}",
            messages=[{"role": "user", "content": prompt}]
        )
        text = message.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        print(f"    Claude error: {e}")
        return {"isMatch": False, "reason": "Analysis failed", "priority": "low", "tenderType": "live"}

def send_email(matches, sender_email, sender_password, recipient_email):
    date_str = datetime.now().strftime("%d %B %Y")
    count = len(matches)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Simpled Tender Bot — {count} new match{'es' if count != 1 else ''} · {date_str}"
    msg["From"] = f"Simpled Tender Bot <{sender_email}>"
    msg["To"] = recipient_email

    high = [m for m in matches if m.get("priority") == "high"]
    medium = [m for m in matches if m.get("priority") == "medium"]
    low = [m for m in matches if m.get("priority") == "low"]

    def render_match(m):
        colors = {"high": ("#3B6D11","#EAF3DE","#1D9E75"), "medium": ("#854F0B","#FAEEDA","#EF9F27"), "low": ("#185FA5","#E6F1FB","#378ADD")}
        p = m.get("priority", "medium")
        tc, bg, bc = colors.get(p, colors["medium"])
        tl = "Prior Notice" if m.get("tenderType") == "prior_notice" else "Live Tender"
        return f"""<div style="border:1px solid #e0e0e0;border-left:4px solid {bc};border-radius:6px;padding:16px;margin:12px 0;background:#fff;">
          <div style="margin-bottom:8px;"><span style="background:{bg};color:{tc};font-size:11px;font-weight:700;padding:3px 8px;border-radius:4px;text-transform:uppercase;margin-right:6px;">{p} priority</span><span style="background:#f0f0f0;color:#555;font-size:11px;padding:3px 8px;border-radius:4px;">{tl}</span></div>
          <h3 style="margin:0 0 10px;font-size:15px;color:#1a1a1a;">{m['title']}</h3>
          <table style="width:100%;font-size:13px;color:#555;border-collapse:collapse;margin-bottom:12px;">
            <tr><td style="padding:3px 0;width:80px;color:#888;font-size:12px;">Buyer</td><td style="font-weight:600;color:#333;">{m['buyer']}</td></tr>
            <tr><td style="padding:3px 0;color:#888;font-size:12px;">Location</td><td>{m['location'] or 'Not specified'}</td></tr>
            <tr><td style="padding:3px 0;color:#888;font-size:12px;">Value</td><td>{m['value']}</td></tr>
            <tr><td style="padding:3px 0;color:#888;font-size:12px;">Deadline</td><td>{m['deadline']}</td></tr>
            <tr><td style="padding:3px 0;color:#888;font-size:12px;">Why</td><td style="color:{tc};font-style:italic;">{m.get('reason','')}</td></tr>
          </table>
          {f'<p style="font-size:12px;color:#666;margin:0 0 12px;line-height:1.5;">{m["description"][:250]}{"..." if len(m["description"])>250 else ""}</p>' if m.get('description') else ''}
          <a href="{m['link']}" style="display:inline-block;background:#185FA5;color:#fff;padding:8px 18px;text-decoration:none;border-radius:5px;font-size:12px;font-weight:600;">View tender →</a>
        </div>"""

    sections = ""
    if high: sections += f'<h3 style="color:#3B6D11;margin:24px 0 4px;font-size:14px;">High priority ({len(high)})</h3>' + "".join(render_match(m) for m in high)
    if medium: sections += f'<h3 style="color:#854F0B;margin:24px 0 4px;font-size:14px;">Medium priority ({len(medium)})</h3>' + "".join(render_match(m) for m in medium)
    if low: sections += f'<h3 style="color:#185FA5;margin:24px 0 4px;font-size:14px;">Low priority ({len(low)})</h3>' + "".join(render_match(m) for m in low)

    html = f"""<html><body style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;padding:20px;background:#f5f5f5;">
      <div style="background:#fff;border-radius:8px;padding:28px;border:1px solid #e0e0e0;">
        <div style="border-bottom:3px solid #185FA5;padding-bottom:14px;margin-bottom:4px;">
          <h2 style="margin:0 0 4px;color:#185FA5;font-size:22px;">Simpled Tender Bot</h2>
          <p style="margin:0;color:#888;font-size:13px;">{count} new match{'es' if count!=1 else ''} · {date_str}</p>
        </div>
        {sections}
        <hr style="border:none;border-top:1px solid #eee;margin:28px 0 16px;">
        <p style="color:#bbb;font-size:11px;margin:0;">Automated daily scan for Simpled Services Ltd</p>
      </div>
    </body></html>"""

    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, recipient_email, msg.as_string())
    print(f"Email sent to {recipient_email}")

def main():
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    sender_email = os.environ.get("SENDER_EMAIL")
    sender_password = os.environ.get("SENDER_PASSWORD")
    recipient_email = os.environ.get("RECIPIENT_EMAIL", sender_email)
    days_back = int(os.environ.get("DAYS_BACK", "1"))

    if not anthropic_key: raise ValueError("ANTHROPIC_API_KEY not set")
    if not sender_email or not sender_password: raise ValueError("SENDER_EMAIL and SENDER_PASSWORD must be set")

    claude_client = anthropic.Anthropic(api_key=anthropic_key)
    print(f"\n{'='*50}\nSimpled Tender Bot — {datetime.now().strftime('%d %b %Y %H:%M')}\n{'='*50}\n")

    tenders = fetch_tenders(days_back=days_back)
    if not tenders:
        print("No relevant tenders found. Exiting.")
        return

    print(f"Analysing {len(tenders)} tenders with Claude...\n")
    matches = []

    for i, release in enumerate(tenders, 1):
        details = extract_details(release)
        print(f"[{i}/{len(tenders)}] {details['title'][:55]}...", end=" ", flush=True)
        result = analyse_tender(details, claude_client)
        if result.get("isMatch"):
            details["priority"] = result.get("priority", "medium")
            details["reason"] = result.get("reason", "")
            details["tenderType"] = result.get("tenderType", "live")
            matches.append(details)
            print(f"MATCH ({details['priority']})")
        else:
            print("skip")

    print(f"\n{'─'*50}\n{len(matches)} matches from {len(tenders)} relevant tenders\n{'─'*50}\n")

    if matches:
        matches.sort(key=lambda m: {"high": 0, "medium": 1, "low": 2}.get(m.get("priority", "low"), 1))
        send_email(matches, sender_email, sender_password, recipient_email)
    else:
        print("No matches today — no email sent.")

if __name__ == "__main__":
    main()

import requests
import json
import os
import anthropic
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta

# ─── CONFIG ───────────────────────────────────────────────────────────────────

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

# CPV codes relevant to Simpled's work
CPV_CODES = [
    "45453100",  # Refurbishment work
    "45000000",  # Construction work (general)
    "45211000",  # Construction work for multi-dwelling buildings
    "45211310",  # Bathrooms construction work
    "50000000",  # Repair and maintenance services
    "50700000",  # Repair and maintenance of building installations
    "45442100",  # Painting work
    "45431000",  # Tiling work
    "45432100",  # Floor laying work
    "37535200",  # Playground equipment
    "45112723",  # Landscaping work for play areas
    "45233200",  # Road surface work (estates)
    "45262700",  # Alterations to buildings
    "45400000",  # Building completion work
    "45300000",  # Building installation work
]

# ─── FETCH FROM FIND A TENDER ─────────────────────────────────────────────────

def fetch_tenders(days_back=1):
    """Fetch tenders published in the last N days from Find a Tender."""
    from_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    to_date = datetime.now().strftime("%Y-%m-%d")

    base_url = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
    all_tenders = []
    seen_ids = set()

    for cpv in CPV_CODES:
        try:
            params = {
                "publishedFrom": from_date,
                "publishedTo": to_date,
                "stages": "tender,planning",
                "CPVs": cpv,
                "limit": 100,
            }
            resp = requests.get(base_url, params=params, timeout=15)
            if resp.status_code != 200:
                continue

            data = resp.json()
            releases = data.get("releases", [])

            for release in releases:
                ocid = release.get("ocid", "")
                if ocid and ocid not in seen_ids:
                    seen_ids.add(ocid)
                    all_tenders.append(release)

        except Exception as e:
            print(f"  Error fetching CPV {cpv}: {e}")
            continue

    print(f"Fetched {len(all_tenders)} unique tenders (last {days_back} day(s))")
    return all_tenders


# ─── EXTRACT TENDER DETAILS ───────────────────────────────────────────────────

def extract_details(release):
    """Pull key fields from a raw OCDS release."""
    tender = release.get("tender", {})
    parties = release.get("parties", [])

    buyer = next((p for p in parties if "buyer" in p.get("roles", [])), {})
    buyer_name = buyer.get("name", "Unknown buyer")

    title = tender.get("title", "Untitled")
    description = tender.get("description", "")
    value = tender.get("value", {}).get("amount")
    currency = tender.get("value", {}).get("currency", "GBP")
    deadline = tender.get("tenderPeriod", {}).get("endDate", "")
    published = release.get("date", "")[:10] if release.get("date") else ""

    locations = tender.get("deliveryLocations", [])
    location = locations[0].get("description", "") if locations else ""

    # Try to get a direct link
    documents = tender.get("documents", [])
    portal_link = next(
        (d.get("url") for d in documents if d.get("url") and "find-tender" in d.get("url", "")),
        f"https://www.find-tender.service.gov.uk/Notice/{release.get('ocid','').replace('ocds-b5fd17-','')}"
    )

    return {
        "title": title,
        "buyer": buyer_name,
        "description": description[:400],
        "value": f"£{value:,.0f} {currency}" if value else "Not specified",
        "deadline": deadline[:10] if deadline else "Check portal",
        "location": location,
        "published": published,
        "link": portal_link,
        "ocid": release.get("ocid", ""),
    }


# ─── ANALYSE WITH CLAUDE ──────────────────────────────────────────────────────

def analyse_tender(details, claude_client):
    """Use Claude to assess if a tender is a good fit for Simpled."""
    prompt = f"""Tender opportunity details:

Title: {details['title']}
Buyer: {details['buyer']}
Location: {details['location'] or 'Not specified'}
Value: {details['value']}
Deadline: {details['deadline']}
Description: {details['description']}

Assess this for Simpled Services Ltd and return ONLY valid JSON, no other text:
{{
  "isMatch": true or false,
  "reason": "max 15 words explaining why",
  "priority": "high, medium, or low",
  "tenderType": "live or prior_notice"
}}"""

    try:
        message = claude_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=f"You assess tender opportunities for a property maintenance contractor. Return only JSON.\n\n{COMPANY_PROFILE}",
            messages=[{"role": "user", "content": prompt}]
        )
        text = message.content[0].text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        print(f"  Claude error for '{details['title'][:40]}': {e}")
        return {"isMatch": False, "reason": "Analysis failed", "priority": "low", "tenderType": "live"}


# ─── BUILD AND SEND EMAIL ─────────────────────────────────────────────────────

def send_email(matches, sender_email, sender_password, recipient_email):
    """Send a nicely formatted HTML email digest."""
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
        priority_colors = {
            "high": ("#3B6D11", "#EAF3DE"),
            "medium": ("#854F0B", "#FAEEDA"),
            "low": ("#185FA5", "#E6F1FB"),
        }
        p = m.get("priority", "medium")
        text_color, bg_color = priority_colors.get(p, priority_colors["medium"])
        tender_type_label = "Prior Notice" if m.get("tenderType") == "prior_notice" else "Live Tender"

        return f"""
        <div style="border:1px solid #e0e0e0;border-left:4px solid {text_color};border-radius:6px;padding:16px;margin:12px 0;background:#fff;">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
            <span style="background:{bg_color};color:{text_color};font-size:11px;font-weight:700;padding:3px 8px;border-radius:4px;text-transform:uppercase;">{p}</span>
            <span style="background:#f0f0f0;color:#666;font-size:11px;padding:3px 8px;border-radius:4px;">{tender_type_label}</span>
          </div>
          <h3 style="margin:0 0 8px;font-size:15px;color:#1a1a1a;line-height:1.3;">{m['title']}</h3>
          <table style="width:100%;font-size:13px;color:#555;border-collapse:collapse;margin-bottom:10px;">
            <tr><td style="padding:2px 0;width:80px;"><strong>Buyer</strong></td><td>{m['buyer']}</td></tr>
            <tr><td style="padding:2px 0;"><strong>Location</strong></td><td>{m['location'] or 'Not specified'}</td></tr>
            <tr><td style="padding:2px 0;"><strong>Value</strong></td><td>{m['value']}</td></tr>
            <tr><td style="padding:2px 0;"><strong>Deadline</strong></td><td>{m['deadline']}</td></tr>
            <tr><td style="padding:2px 0;"><strong>Why</strong></td><td style="color:{text_color};">{m.get('reason','')}</td></tr>
          </table>
          {f'<p style="font-size:12px;color:#666;margin:0 0 10px;">{m["description"][:200]}{"..." if len(m["description"])>200 else ""}</p>' if m.get('description') else ''}
          <a href="{m['link']}" style="display:inline-block;background:#185FA5;color:#fff;padding:7px 16px;text-decoration:none;border-radius:5px;font-size:12px;font-weight:600;">View tender →</a>
        </div>"""

    sections = ""
    if high:
        sections += f'<h3 style="color:#3B6D11;margin:24px 0 8px;">High priority ({len(high)})</h3>' + "".join(render_match(m) for m in high)
    if medium:
        sections += f'<h3 style="color:#854F0B;margin:24px 0 8px;">Medium priority ({len(medium)})</h3>' + "".join(render_match(m) for m in medium)
    if low:
        sections += f'<h3 style="color:#185FA5;margin:24px 0 8px;">Low priority ({len(low)})</h3>' + "".join(render_match(m) for m in low)

    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:620px;margin:0 auto;padding:20px;background:#f9f9f9;">
      <div style="background:#fff;border-radius:8px;padding:24px;border:1px solid #e0e0e0;">
        <div style="border-bottom:2px solid #185FA5;padding-bottom:12px;margin-bottom:16px;">
          <h2 style="margin:0;color:#185FA5;font-size:20px;">Simpled Tender Bot</h2>
          <p style="margin:4px 0 0;color:#666;font-size:13px;">{count} new match{'es' if count!=1 else ''} found · {date_str}</p>
        </div>
        {sections}
        <hr style="border:none;border-top:1px solid #eee;margin:24px 0 16px;">
        <p style="color:#aaa;font-size:11px;margin:0;">Daily scan of find-a-tender.service.gov.uk · Simpled Services Ltd</p>
      </div>
    </body></html>"""

    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, recipient_email, msg.as_string())
        print(f"Email sent to {recipient_email}")
    except Exception as e:
        print(f"Email error: {e}")
        raise


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    sender_email = os.environ.get("SENDER_EMAIL")
    sender_password = os.environ.get("SENDER_PASSWORD")
    recipient_email = os.environ.get("RECIPIENT_EMAIL", sender_email)
    days_back = int(os.environ.get("DAYS_BACK", "1"))

    if not anthropic_key:
        raise ValueError("ANTHROPIC_API_KEY not set")
    if not sender_email or not sender_password:
        raise ValueError("SENDER_EMAIL and SENDER_PASSWORD must be set")

    claude_client = anthropic.Anthropic(api_key=anthropic_key)

    print(f"\n{'='*50}")
    print(f"Simpled Tender Bot — {datetime.now().strftime('%d %b %Y %H:%M')}")
    print(f"{'='*50}\n")

    tenders = fetch_tenders(days_back=days_back)

    if not tenders:
        print("No tenders found today. Exiting.")
        return

    print(f"\nAnalysing {len(tenders)} tenders with Claude...\n")
    matches = []

    for i, release in enumerate(tenders, 1):
        details = extract_details(release)
        title_short = details["title"][:55]
        print(f"[{i}/{len(tenders)}] {title_short}...", end=" ", flush=True)

        result = analyse_tender(details, claude_client)

        if result.get("isMatch"):
            details["priority"] = result.get("priority", "medium")
            details["reason"] = result.get("reason", "")
            details["tenderType"] = result.get("tenderType", "live")
            matches.append(details)
            print(f"✓ MATCH ({details['priority']})")
        else:
            print("✗ skip")

    print(f"\n{'─'*50}")
    print(f"Result: {len(matches)} matches from {len(tenders)} tenders scanned")
    print(f"{'─'*50}\n")

    if matches:
        matches.sort(key=lambda m: {"high": 0, "medium": 1, "low": 2}.get(m.get("priority", "low"), 1))
        send_email(matches, sender_email, sender_password, recipient_email)
    else:
        print("No matches today — no email sent.")


if __name__ == "__main__":
    main()

import json
import re
import os
import anthropic
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta

SEARCH_PROMPT = """Search for UK public sector tender opportunities published in the last {days} days.

Search these sites using multiple queries:
- site:contractsfinder.service.gov.uk property maintenance tender
- site:contractsfinder.service.gov.uk refurbishment housing
- site:contractsfinder.service.gov.uk kitchen bathroom council
- site:contractsfinder.service.gov.uk playground equipment
- site:find-tender.service.gov.uk maintenance refurbishment
- site:procontract.due-north.com maintenance housing

You are finding tenders for Simpled Services Ltd, a London-based property maintenance and refurbishment contractor.

GOOD FIT: kitchen/bathroom refurbishments, property maintenance, playground equipment, open space works, internal alterations, decoration, flooring, void works, estate maintenance, housing association or council works.
NOT a fit: professional consultancy (architects, engineers, surveyors), large civil infrastructure, IT/digital.

After completing all searches, return ONLY a valid JSON array with every opportunity found, no markdown, no explanation:
[
  {{
    "title": "tender title",
    "client": "buyer organisation",
    "description": "what the work involves, max 15 words",
    "link": "direct URL to the tender",
    "deadline": "submission deadline as written, or null",
    "estimatedValue": "contract value if stated, or null",
    "location": "city or region",
    "isMatch": true or false,
    "matchReason": "why it matches or not, max 12 words",
    "tenderType": "live or prior_notice"
  }}
]"""

def extract_json(text):
    """Extract JSON array from text even if surrounded by other content."""
    text = text.strip()
    # Try direct parse first
    try:
        return json.loads(text)
    except Exception:
        pass
    # Try stripping markdown fences
    text = re.sub(r"```json|```", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # Try finding the first [ ... ] block
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass
    return []

def search_tenders(claude_client, days_back=1):
    print(f"Searching for tenders (last {days_back} day(s))...")
    tools = [{"type": "web_search_20250305", "name": "web_search"}]
    messages = [{"role": "user", "content": SEARCH_PROMPT.format(days=days_back)}]

    for i in range(10):  # agentic loop — max 10 turns
        response = claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            tools=tools,
            messages=messages,
        )

        block_types = [b.type for b in response.content]
        print(f"  Turn {i+1}: stop_reason={response.stop_reason} blocks={block_types}")

        # Append assistant turn to conversation
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            # Extract final text response
            text = "".join(
                b.text for b in response.content
                if hasattr(b, "text") and b.type == "text"
            )
            print(f"  Final text length: {len(text)} chars")
            results = extract_json(text)
            print(f"  Parsed {len(results)} results")
            return results

        elif response.stop_reason == "tool_use":
            # Build tool_result blocks for each tool_use
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"    Tool call: {block.name} — {getattr(block, 'input', {})}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Search completed.",
                    })
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            else:
                break
        else:
            print(f"  Unexpected stop_reason: {response.stop_reason}")
            break

    print("  Search loop ended without final response.")
    return []

def send_email(matches, all_results, sender_email, sender_password, recipient_email):
    date_str = datetime.now().strftime("%d %B %Y")
    count = len(matches)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Simpled Tender Bot — {count} match{'es' if count!=1 else ''} · {date_str}"
    msg["From"] = f"Simpled Tender Bot <{sender_email}>"
    msg["To"] = recipient_email

    def card(m):
        p = m.get("priority", "medium")
        cols = {
            "high":   ("#3B6D11", "#EAF3DE", "#1D9E75"),
            "medium": ("#854F0B", "#FAEEDA", "#EF9F27"),
            "low":    ("#185FA5", "#E6F1FB", "#378ADD"),
        }
        tc, bg, bc = cols.get(p, cols["medium"])
        tl = "Prior Notice" if m.get("tenderType") == "prior_notice" else "Live Tender"
        desc = m.get("description", "")
        return f"""<div style="border:1px solid #ddd;border-left:4px solid {bc};border-radius:6px;padding:16px;margin:12px 0;background:#fff;">
<div style="margin-bottom:8px;">
  <span style="background:{bg};color:{tc};font-size:11px;font-weight:700;padding:3px 8px;border-radius:4px;text-transform:uppercase;margin-right:6px;">{p}</span>
  <span style="background:#f0f0f0;color:#555;font-size:11px;padding:3px 8px;border-radius:4px;">{tl}</span>
</div>
<h3 style="margin:0 0 10px;font-size:15px;color:#1a1a1a;line-height:1.4;">{m.get('title','')}</h3>
<table style="font-size:13px;color:#555;border-collapse:collapse;margin-bottom:10px;width:100%;">
  <tr><td style="padding:3px 0;width:75px;color:#999;font-size:12px;">Buyer</td><td style="font-weight:600;color:#333;">{m.get('client','')}</td></tr>
  <tr><td style="padding:3px 0;color:#999;font-size:12px;">Location</td><td>{m.get('location','') or 'Not specified'}</td></tr>
  <tr><td style="padding:3px 0;color:#999;font-size:12px;">Value</td><td>{m.get('estimatedValue','') or 'Not specified'}</td></tr>
  <tr><td style="padding:3px 0;color:#999;font-size:12px;">Deadline</td><td>{m.get('deadline','') or 'Check portal'}</td></tr>
  <tr><td style="padding:3px 0;color:#999;font-size:12px;">Why</td><td style="color:{tc};font-style:italic;">{m.get('matchReason','')}</td></tr>
</table>
{f'<p style="font-size:12px;color:#666;margin:0 0 12px;line-height:1.5;">{desc[:250]}{"..." if len(desc)>250 else ""}</p>' if desc else ''}
<a href="{m.get('link','#')}" style="background:#185FA5;color:#fff;padding:8px 16px;text-decoration:none;border-radius:5px;font-size:12px;font-weight:600;display:inline-block;">View tender →</a>
</div>"""

    high   = [m for m in matches if m.get("priority") == "high"]
    medium = [m for m in matches if m.get("priority") == "medium"]
    low    = [m for m in matches if m.get("priority") == "low"]
    non_matches = [r for r in all_results if not r.get("isMatch")]

    sections = ""
    if high:   sections += f'<h3 style="color:#3B6D11;margin:20px 0 4px;font-size:14px;">High priority ({len(high)})</h3>' + "".join(card(m) for m in high)
    if medium: sections += f'<h3 style="color:#854F0B;margin:20px 0 4px;font-size:14px;">Medium priority ({len(medium)})</h3>' + "".join(card(m) for m in medium)
    if low:    sections += f'<h3 style="color:#185FA5;margin:20px 0 4px;font-size:14px;">Low priority ({len(low)})</h3>' + "".join(card(m) for m in low)

    non_rows = "".join(
        f'<tr><td style="padding:4px 8px;font-size:12px;color:#555;border-bottom:1px solid #f0f0f0;">{r.get("title","")[:60]}</td>'
        f'<td style="padding:4px 8px;font-size:11px;color:#aaa;border-bottom:1px solid #f0f0f0;">{r.get("matchReason","")}</td></tr>'
        for r in non_matches[:15]
    )
    also_checked = f"""<details style="margin-top:16px;"><summary style="font-size:12px;color:#aaa;cursor:pointer;">Also checked — {len(non_matches)} not a fit</summary>
<table style="width:100%;border-collapse:collapse;margin-top:6px;">{non_rows}</table></details>""" if non_rows else ""

    html = f"""<html><body style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;padding:20px;background:#f5f5f5;">
<div style="background:#fff;border-radius:8px;padding:24px;border:1px solid #e0e0e0;">
  <div style="border-bottom:3px solid #185FA5;padding-bottom:12px;margin-bottom:8px;">
    <h2 style="margin:0 0 4px;color:#185FA5;font-size:20px;">Simpled Tender Bot</h2>
    <p style="margin:0;color:#999;font-size:12px;">{count} match{'es' if count!=1 else ''} from {len(all_results)} scanned · {date_str}</p>
  </div>
  {sections or '<p style="color:#888;font-size:13px;margin:16px 0;">No matches found today.</p>'}
  {also_checked}
  <p style="color:#ccc;font-size:11px;margin-top:20px;padding-top:12px;border-top:1px solid #eee;">Automated daily scan · Simpled Services Ltd</p>
</div></body></html>"""

    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(sender_email, sender_password)
        s.sendmail(sender_email, recipient_email, msg.as_string())
    print(f"Email sent to {recipient_email}")

def main():
    anthropic_key   = os.environ.get("ANTHROPIC_API_KEY")
    sender_email    = os.environ.get("SENDER_EMAIL")
    sender_password = os.environ.get("SENDER_PASSWORD")
    recipient_email = os.environ.get("RECIPIENT_EMAIL", sender_email)
    days_back = int(os.environ.get("DAYS_BACK", "1"))

    if not anthropic_key:   raise ValueError("ANTHROPIC_API_KEY not set")
    if not sender_email:    raise ValueError("SENDER_EMAIL not set")
    if not sender_password: raise ValueError("SENDER_PASSWORD not set")

    claude_client = anthropic.Anthropic(api_key=anthropic_key)

    print(f"\n{'='*50}")
    print(f"Simpled Tender Bot — {datetime.now().strftime('%d %b %Y %H:%M')}")
    print(f"{'='*50}\n")

    results = search_tenders(claude_client, days_back)

    if not results:
        print("No results returned. Exiting.")
        return

    matches = [r for r in results if r.get("isMatch")]
    for m in matches:
        if "priority" not in m:
            loc = (m.get("location") or "").lower()
            m["priority"] = "high" if any(x in loc for x in [
                "london","south east","essex","kent","surrey","hertfordshire","middlesex","berkshire"
            ]) else "medium"

    matches.sort(key=lambda m: {"high":0,"medium":1,"low":2}.get(m.get("priority","low"),1))

    print(f"\n{'─'*50}")
    print(f"{len(matches)} matches from {len(results)} tenders scanned")
    for m in matches:
        print(f"  [{m.get('priority','?').upper()}] {m.get('title','')[:55]}")
    print(f"{'─'*50}\n")

    send_email(matches, results, sender_email, sender_password, recipient_email)
    print("Done.")

if __name__ == "__main__":
    main()

import json
import re
import os
import time
import anthropic
from datetime import datetime, timedelta

def get_search_prompt(days_back):
    today = datetime.now().strftime("%d %B %Y")
    cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%d %B %Y")
    date_filter = (datetime.now() - timedelta(days=days_back)).strftime('%Y-%m-%d')
    return f"""Today is {today}. Search for UK public sector tender opportunities published between {cutoff} and {today}.

Use multiple searches:
- site:contractsfinder.service.gov.uk property maintenance tender after:{date_filter}
- site:contractsfinder.service.gov.uk refurbishment housing association after:{date_filter}
- site:contractsfinder.service.gov.uk kitchen bathroom council after:{date_filter}
- site:contractsfinder.service.gov.uk playground equipment after:{date_filter}
- site:find-tender.service.gov.uk maintenance refurbishment after:{date_filter}
- site:procontract.due-north.com maintenance housing after:{date_filter}

STRICT RULES:
- Only include tenders where the submission deadline is after {today}
- Only include tenders published in the last {days_back} days
- Do NOT include expired tenders, awarded contracts, or anything from before 2026

Finding tenders for Simpled Services Ltd — London-based property maintenance and refurbishment contractor.
GOOD FIT: kitchen/bathroom refurbishments, property maintenance, playground equipment, open space, internal alterations, decoration, flooring, void works, estate maintenance, housing association or council works.
NOT a fit: consultancy (architects, engineers, surveyors), civil infrastructure, IT/digital.

After all searches, return ONLY a valid JSON array, no markdown, no explanation whatsoever:
[{{"title":"...","client":"...","description":"max 15 words","link":"url","deadline":"as written or null","estimatedValue":"or null","location":"city/region","isMatch":true,"matchReason":"max 12 words","tenderType":"live or prior_notice"}}]"""

def is_future_deadline(deadline_str):
    if not deadline_str:
        return True
    years = re.findall(r'\b(20\d{2})\b', deadline_str)
    if years:
        year = int(years[-1])
        current_year = datetime.now().year
        if year < current_year:
            return False
        if year == current_year:
            for fmt in ["%d %B %Y", "%d/%m/%Y", "%Y-%m-%d", "%d %b %Y"]:
                try:
                    dt = datetime.strptime(deadline_str.strip()[:20], fmt)
                    return dt >= datetime.now()
                except Exception:
                    pass
    return True

def extract_json(text):
    try:
        result = json.loads(text.strip())
        if isinstance(result, list):
            return result
    except Exception:
        pass
    cleaned = re.sub(r"```json|```", "", text).strip()
    try:
        result = json.loads(cleaned)
        if isinstance(result, list):
            return result
    except Exception:
        pass
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            result = json.loads(text[start:end+1])
            if isinstance(result, list):
                return result
        except Exception:
            pass
    return []

def reformat_to_json(claude_client, raw_text):
    """Fresh lightweight call — just ask Claude to extract JSON from the raw text."""
    print("  Reformatting via fresh call...")
    try:
        time.sleep(20)  # wait before retry to avoid rate limit
        response = claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=3000,
            messages=[{
                "role": "user",
                "content": f"Extract all tender opportunities from the text below and return ONLY a valid JSON array. No markdown, no explanation, just the raw JSON array starting with [ and ending with ].\n\nText:\n{raw_text[:6000]}"
            }]
        )
        text = "".join(b.text for b in response.content if hasattr(b, "text") and b.type == "text")
        return extract_json(text)
    except Exception as e:
        print(f"  Reformat error: {e}")
        return []

def search_tenders(claude_client, days_back=1):
    print(f"Searching for tenders (last {days_back} day(s))...")
    tools = [{"type": "web_search_20250305", "name": "web_search"}]
    messages = [{"role": "user", "content": get_search_prompt(days_back)}]
    all_text_so_far = []

    for i in range(8):
        try:
            response = claude_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4000,
                tools=tools,
                messages=messages,
            )
        except anthropic.RateLimitError:
            print(f"  Rate limit hit on turn {i+1} — waiting 30s...")
            time.sleep(30)
            try:
                response = claude_client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=4000,
                    tools=tools,
                    messages=messages,
                )
            except Exception as e:
                print(f"  Retry failed: {e}")
                break

        block_types = [b.type for b in response.content]
        print(f"  Turn {i+1}: stop_reason={response.stop_reason} blocks={block_types}")

        messages.append({"role": "assistant", "content": response.content})

        # Collect text blocks
        text_blocks = [
            b.text for b in response.content
            if hasattr(b, "text") and b.type == "text"
        ]
        all_text_so_far.extend(text_blocks)

        if response.stop_reason == "end_turn":
            print(f"  Text blocks this turn: {len(text_blocks)}, chars: {sum(len(t) for t in text_blocks)}")

            # Try each block individually
            for block in text_blocks:
                result = extract_json(block)
                if result:
                    print(f"  Parsed {len(result)} results from single block")
                    return result

            # Try combined
            combined = "".join(text_blocks)
            result = extract_json(combined)
            if result:
                print(f"  Parsed {len(result)} results from combined")
                return result

            # Fall back to fresh lightweight call with all text collected so far
            all_text = "".join(all_text_so_far)
            return reformat_to_json(claude_client, all_text)

        elif response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"    Search: {getattr(block, 'input', {})}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Search completed.",
                    })
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
                time.sleep(12)
            else:
                break
        else:
            break

    print("  Could not parse results.")
    return []

def priority_for(match):
    if "priority" in match:
        return match["priority"]
    loc = (match.get("location") or "").lower()
    return "high" if any(x in loc for x in [
        "london", "south east", "essex", "kent", "surrey",
        "hertfordshire", "middlesex", "berkshire"
    ]) else "medium"

def build_html(matches, all_results, removed_count=0):
    now = datetime.now()
    date_str = now.strftime("%d %B %Y")
    time_str = now.strftime("%H:%M UTC")
    count = len(matches)
    non_matches = [r for r in all_results if not r.get("isMatch")]

    priority_colors = {
        "high":   {"bg":"#EAF3DE","color":"#3B6D11","border":"#1D9E75","label":"High priority"},
        "medium": {"bg":"#FAEEDA","color":"#854F0B","border":"#EF9F27","label":"Medium priority"},
        "low":    {"bg":"#E6F1FB","color":"#185FA5","border":"#378ADD","label":"Low priority"},
    }

    def card(m):
        p = m.get("priority", "medium")
        c = priority_colors.get(p, priority_colors["medium"])
        tl = "Prior Notice" if m.get("tenderType") == "prior_notice" else "Live Tender"
        desc = m.get("description", "")
        val = m.get("estimatedValue") or "Not stated"
        dl = m.get("deadline") or "Check portal"
        loc = m.get("location") or "Not specified"
        link = m.get("link", "#")
        return f"""
        <div class="card" style="border-left:4px solid {c['border']};">
          <div class="card-meta">
            <span class="badge" style="background:{c['bg']};color:{c['color']};">{c['label']}</span>
            <span class="badge badge-type">{tl}</span>
          </div>
          <h3>{m.get('title','')}</h3>
          <table class="details">
            <tr><td>Buyer</td><td><strong>{m.get('client','')}</strong></td></tr>
            <tr><td>Location</td><td>{loc}</td></tr>
            <tr><td>Value</td><td>{val}</td></tr>
            <tr><td>Deadline</td><td>{dl}</td></tr>
            <tr><td>Why</td><td style="color:{c['color']};font-style:italic;">{m.get('matchReason','')}</td></tr>
          </table>
          {f'<p class="desc">{desc}</p>' if desc else ''}
          <a href="{link}" class="btn" target="_blank" rel="noopener">View tender &rarr;</a>
        </div>"""

    cards_html = ""
    for p_key in ["high", "medium", "low"]:
        group = [m for m in matches if m.get("priority") == p_key]
        if group:
            c = priority_colors[p_key]
            cards_html += f'<h2 class="section-title" style="color:{c["color"]}">{c["label"]} ({len(group)})</h2>'
            cards_html += "".join(card(m) for m in group)

    skip_rows = "".join(
        f'<tr><td>{r.get("title","")[:70]}</td><td>{r.get("client","")}</td><td>{r.get("matchReason","")}</td></tr>'
        for r in non_matches[:20]
    )
    skip_section = f"""
    <details class="skip-section">
      <summary>Also checked &mdash; {len(non_matches)} not a fit{f', {removed_count} expired removed' if removed_count else ''}</summary>
      <table class="skip-table">
        <thead><tr><th>Title</th><th>Buyer</th><th>Reason skipped</th></tr></thead>
        <tbody>{skip_rows}</tbody>
      </table>
    </details>""" if skip_rows else ""

    no_match_msg = '<p class="no-match">No matches found today. Check back tomorrow.</p>' if not matches else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Simpled Tender Bot</title>
<style>
  *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;color:#1a1a1a;line-height:1.5}}
  .header{{background:#fff;border-bottom:3px solid #185FA5;padding:20px 24px;position:sticky;top:0;z-index:10}}
  .header h1{{font-size:20px;color:#185FA5;font-weight:600}}
  .header p{{font-size:13px;color:#999;margin-top:2px}}
  .stats{{display:flex;gap:12px;padding:16px 24px;background:#fff;border-bottom:1px solid #eee;flex-wrap:wrap}}
  .stat{{background:#f5f5f5;border-radius:8px;padding:10px 16px;min-width:90px}}
  .stat-label{{font-size:11px;color:#999;text-transform:uppercase;letter-spacing:.04em}}
  .stat-value{{font-size:22px;font-weight:600;color:#1a1a1a}}
  .content{{max-width:760px;margin:0 auto;padding:24px 16px}}
  .section-title{{font-size:13px;font-weight:700;margin:24px 0 10px;text-transform:uppercase;letter-spacing:.05em}}
  .card{{background:#fff;border:1px solid #e5e5e5;border-radius:10px;padding:18px;margin-bottom:12px}}
  .card h3{{font-size:15px;font-weight:600;margin:10px 0 12px;line-height:1.4}}
  .card-meta{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:4px}}
  .badge{{font-size:11px;font-weight:700;padding:3px 9px;border-radius:5px;text-transform:uppercase;letter-spacing:.03em}}
  .badge-type{{background:#f0f0f0;color:#555}}
  .details{{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:12px}}
  .details td{{padding:4px 0;vertical-align:top}}
  .details td:first-child{{color:#999;font-size:12px;width:80px;padding-right:8px}}
  .desc{{font-size:13px;color:#666;margin-bottom:14px;line-height:1.5}}
  .btn{{display:inline-block;background:#185FA5;color:#fff;padding:8px 18px;border-radius:6px;font-size:13px;font-weight:600;text-decoration:none}}
  .btn:hover{{background:#0C447C}}
  .no-match{{color:#999;font-size:14px;margin:40px 0;text-align:center}}
  .skip-section{{margin-top:24px;border:1px solid #eee;border-radius:8px;overflow:hidden}}
  .skip-section summary{{padding:12px 16px;font-size:13px;color:#aaa;cursor:pointer;background:#fafafa}}
  .skip-table{{width:100%;border-collapse:collapse;font-size:12px}}
  .skip-table th{{padding:8px 12px;text-align:left;background:#f5f5f5;color:#aaa;font-size:11px;text-transform:uppercase}}
  .skip-table td{{padding:7px 12px;border-top:1px solid #f0f0f0;color:#666}}
  .footer{{text-align:center;font-size:11px;color:#ccc;padding:24px}}
  @media(max-width:600px){{.stats{{gap:8px}}.content{{padding:16px 12px}}}}
</style>
</head>
<body>
<div class="header">
  <h1>Simpled Tender Bot</h1>
  <p>{count} match{'es' if count != 1 else ''} from {len(all_results)} scanned &middot; {date_str} at {time_str}</p>
</div>
<div class="stats">
  <div class="stat"><div class="stat-label">Scanned</div><div class="stat-value">{len(all_results)}</div></div>
  <div class="stat"><div class="stat-label">Matches</div><div class="stat-value">{count}</div></div>
  <div class="stat"><div class="stat-label">High</div><div class="stat-value" style="color:#3B6D11">{len([m for m in matches if m.get('priority')=='high'])}</div></div>
  <div class="stat"><div class="stat-label">Medium</div><div class="stat-value" style="color:#854F0B">{len([m for m in matches if m.get('priority')=='medium'])}</div></div>
</div>
<div class="content">
  {no_match_msg}
  {cards_html}
  {skip_section}
</div>
<div class="footer">Automated daily scan &middot; Simpled Services Ltd &middot; contractsfinder.service.gov.uk &middot; find-tender.service.gov.uk</div>
</body>
</html>"""

def main():
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    days_back = int(os.environ.get("DAYS_BACK", "1"))

    if not anthropic_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    claude_client = anthropic.Anthropic(api_key=anthropic_key)

    print(f"\n{'='*50}")
    print(f"Simpled Tender Bot — {datetime.now().strftime('%d %b %Y %H:%M')}")
    print(f"{'='*50}\n")

    results = search_tenders(claude_client, days_back)
    if not results:
        results = []

    before_filter = len(results)
    results = [r for r in results if is_future_deadline(r.get("deadline", ""))]
    removed = before_filter - len(results)
    if removed:
        print(f"Removed {removed} expired tenders")

    matches = [r for r in results if r.get("isMatch")]
    for m in matches:
        m["priority"] = priority_for(m)
    matches.sort(key=lambda m: {"high":0,"medium":1,"low":2}.get(m.get("priority","low"),1))

    print(f"\n{'─'*50}")
    print(f"{len(matches)} matches from {len(results)} tenders scanned")
    for m in matches:
        print(f"  [{m.get('priority','?').upper()}] {m.get('title','')[:55]}")
    print(f"{'─'*50}\n")

    os.makedirs("docs", exist_ok=True)
    html = build_html(matches, results, removed_count=removed)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("Dashboard written to docs/index.html")

if __name__ == "__main__":
    main()

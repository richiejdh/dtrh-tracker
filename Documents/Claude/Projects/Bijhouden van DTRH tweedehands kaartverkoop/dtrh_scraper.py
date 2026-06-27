#!/usr/bin/env python3
"""
DTRH Ticketmaster Resale Tracker
=================================
Bijhoudt tweedehands kaartverkoop voor Down The Rabbit Hole 2026 (3–5 juli).
Slaat elke run op in een CSV-bestand voor latere analyse en visualisatie.

Installatie (eenmalig):
    pip3 install playwright
    /Users/richie/Library/Python/3.9/bin/playwright install chromium

Handmatig draaien:
    python3 dtrh_scraper.py

Via cron (elke ochtend om 08:00):
    crontab -e
    0 8 * * * /usr/bin/python3 "/Users/richie/Documents/Claude/Projects/Bijhouden van DTRH tweedehands kaartverkoop/dtrh_scraper.py"
"""

import asyncio
import csv
import json
import re
import statistics
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ─── Configuratie ─────────────────────────────────────────────────────────────
FESTIVAL_START = date(2026, 7, 3)
EVENT_ID       = "630353030"
EVENT_SLUG     = "down-the-rabbit-hole-2026-festivalticket-tickets"
FACE_VALUE_EUR = 329.00   # originele verkoopprijs incl. servicekosten

PROJECT_DIR = Path(__file__).parent
CSV_FILE        = PROJECT_DIR / "dtrh_resale_data.csv"
LOG_FILE        = PROJECT_DIR / "dtrh_scraper.log"
DEBUG_DUMP_FILE = PROJECT_DIR / "dtrh_last_run_debug.json"

# Directe API voor aantallen (geen Playwright nodig)
RESALE_API_URL = f"https://availability.ticketmaster.nl/api/v2/TM_NL/resale/{EVENT_ID}"

# Pagina voor DOM-prijzen
EVENT_PAGE_URL = f"https://www.ticketmaster.nl/event/{EVENT_SLUG}/{EVENT_ID}"

CSV_HEADERS = [
    "timestamp", "date", "days_until_festival",
    "total_listings",   # unieke aanbiedingen van verkopers
    "total_tickets",    # totaal kaarten (som van placeCount per listing)
    "min_price", "max_price", "avg_price", "median_price",
    "p25_price", "p75_price",
    "count_below_face", "count_at_face", "count_above_face",
    "face_value", "notes",
]

# ─── Logging ──────────────────────────────────────────────────────────────────
def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ─── Prijs-utilities ──────────────────────────────────────────────────────────
def parse_price(text: str) -> Optional[float]:
    """Zet een prijs-string om naar float (handelt EUR-notatie af)."""
    if not text:
        return None
    text = str(text).strip()
    text = re.sub(r"[€$£\s]", "", text)
    if "," in text and "." in text:
        if text.rindex(",") > text.rindex("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        val = float(text)
        return val if 10 < val < 10_000 else None
    except ValueError:
        return None

def calc_price_stats(prices: List[float]) -> Dict:
    """Bereken statistieken over een lijst prijzen per listing."""
    if not prices:
        return {k: None for k in [
            "min_price", "max_price", "avg_price", "median_price",
            "p25_price", "p75_price",
            "count_below_face", "count_at_face", "count_above_face",
        ]}
    s = sorted(prices)
    n = len(s)
    return {
        "min_price":        round(min(s), 2),
        "max_price":        round(max(s), 2),
        "avg_price":        round(statistics.mean(s), 2),
        "median_price":     round(statistics.median(s), 2),
        "p25_price":        round(s[max(0, int(n * 0.25) - 1)], 2),
        "p75_price":        round(s[min(n - 1, int(n * 0.75))], 2),
        "count_below_face": sum(1 for p in s if p < FACE_VALUE_EUR - 1),
        "count_at_face":    sum(1 for p in s if abs(p - FACE_VALUE_EUR) <= 1),
        "count_above_face": sum(1 for p in s if p > FACE_VALUE_EUR + 1),
    }

def parse_resale_api(data: dict) -> Tuple[int, int]:
    """Verwerk de resale API-response naar (total_listings, total_tickets)."""
    groups = data.get("groups", [])
    total_listings = len(groups)
    total_tickets = sum(
        sum(sec.get("placeCount", 0) for sec in g.get("sections", {}).values())
        for g in groups
    )
    return total_listings, total_tickets

# ─── Playwright: prijzen + aantallen in één sessie ───────────────────────────
async def fetch_prices_playwright() -> Tuple[List[float], int, int]:
    """
    Laadt de event-pagina met Playwright.
    Onderschept de resale API voor aantallen, haalt prijzen uit de DOM.
    Geeft (prices, total_listings, total_tickets) terug.
    """
    from playwright.async_api import async_playwright

    # Optioneel: playwright-stealth voorkomt bot-detectie op cloud-omgevingen
    try:
        from playwright_stealth import stealth_async
        has_stealth = True
    except ImportError:
        has_stealth = False
        log("playwright-stealth niet gevonden; draait zonder stealth (werkt mogelijk niet op cloud)", "WARN")

    captured_api_data = []
    resale_counts = (0, 0)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1280,900",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="nl-NL",
            viewport={"width": 1280, "height": 900},
            extra_http_headers={
                "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
                "Referer": "https://www.ticketmaster.nl/",
            },
        )
        page = await context.new_page()

        # Stealth-mode: verwijdert webdriver-tells die Ticketmaster triggeren
        if has_stealth:
            await stealth_async(page)
            log("Stealth mode actief — bot-detectie omzeilen")

        # Onderschep JSON-responses (voor debug-dump)
        async def on_response(response):
            try:
                if "json" not in response.headers.get("content-type", ""):
                    return
                url = response.url.lower()
                if any(kw in url for kw in ["resale", "listing", "offer", "ticket", "inventory"]):
                    body = await response.json()
                    captured_api_data.append({"url": response.url, "data": body})
            except Exception:
                pass

        page.on("response", on_response)

        prices: List[float] = []
        log(f"Playwright: laden van {EVENT_PAGE_URL}")

        try:
            await page.goto(EVENT_PAGE_URL, wait_until="networkidle", timeout=60_000)

            # Cookiebanner accepteren (nodig op cloud-omgevingen waar sessie leeg is)
            cookie_selectors = [
                "button#onetrust-accept-btn-handler",
                "button[id*='accept-all']",
                "button:has-text('Alles accepteren')",
                "button:has-text('Accept all')",
                "button:has-text('Accepteer')",
                "[aria-label*='ccept']",
            ]
            for sel in cookie_selectors:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=2_000):
                        await btn.click()
                        log(f"Cookie-banner geklikt: {sel}")
                        await page.wait_for_timeout(2_000)
                        break
                except Exception:
                    continue

            await page.wait_for_timeout(5_000)
            prices = await _extract_dom_prices(page)
            log(f"Playwright: {len(prices)} prijzen gevonden via DOM")
        except Exception as e:
            log(f"Playwright laad-fout: {e}", "WARN")

        # Haal aantallen uit onderschepte resale API-response
        for item in captured_api_data:
            if "resale" in item["url"].lower():
                resale_counts = parse_resale_api(item["data"])
                log(f"Aantallen via browser-API: {resale_counts[0]} listings, {resale_counts[1]} kaarten")
                break

        # Sla debug-dump op
        if captured_api_data:
            with open(DEBUG_DUMP_FILE, "w", encoding="utf-8") as f:
                json.dump(captured_api_data[:20], f, indent=2, default=str)
            log(f"Debug dump: {DEBUG_DUMP_FILE}")

        await browser.close()

    return prices, resale_counts[0], resale_counts[1]


async def _extract_dom_prices(page) -> List[float]:
    """Haalt unieke prijzen op uit de gerenderde DOM."""
    prices = []
    seen = set()

    # Probeer CSS-selectors voor prijselementen
    selectors = [
        "[data-testid*='price']", "[class*='price']", "[class*='Price']",
        "[class*='listing']", ".price", ".amount",
    ]
    for sel in selectors:
        try:
            els = await page.query_selector_all(sel)
            for el in els:
                txt = await el.inner_text()
                p = parse_price(txt)
                if p and 50 < p < 1500 and p not in seen:
                    seen.add(p)
                    prices.append(p)
        except Exception:
            continue

    # Fallback: regex over paginatekst
    if not prices:
        try:
            txt = await page.inner_text("body")
            for match in re.findall(r"€\s*(\d{1,4}[,.]?\d{0,2})", txt):
                p = parse_price(match)
                if p and 50 < p < 1500 and p not in seen:
                    seen.add(p)
                    prices.append(p)
        except Exception:
            pass

    return prices

# ─── HTML Dashboard genereren ────────────────────────────────────────────────
def generate_dashboard() -> None:
    """Leest de CSV en schrijft een zelfstandig HTML-dashboard."""
    if not CSV_FILE.exists():
        return
    # Lees CSV, sla lege rijen over, dedupleer op datum (laatste meting per dag)
    by_date: Dict[str, dict] = {}
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("min_price"):
                by_date[r["date"]] = r   # overschrijft eerdere meting zelfde dag
    rows = sorted(by_date.values(), key=lambda r: r["date"])
    if not rows:
        return

    last = rows[-1]
    updated = datetime.now().strftime("%-d %b %Y, %H:%M")
    days_left = int(last.get("days_until_festival") or 0)

    # Bouw JavaScript data-arrays
    def val(r, k):
        v = r.get(k, "")
        try: return round(float(v), 2)
        except: return "null"

    # Datumnotatie: "28 mei" ipv "2026-05-28"
    nl_months = ["","jan","feb","mrt","apr","mei","jun","jul","aug","sep","okt","nov","dec"]
    def fmt_date(d):
        parts = d.split("-")
        return f"{int(parts[2])} {nl_months[int(parts[1])]}"

    js_dates    = json.dumps([fmt_date(r["date"]) for r in rows])
    js_min      = json.dumps([val(r, "min_price")      for r in rows])
    js_avg      = json.dumps([val(r, "avg_price")      for r in rows])
    js_median   = json.dumps([val(r, "median_price")   for r in rows])
    js_max      = json.dumps([val(r, "max_price")      for r in rows])
    js_p25      = json.dumps([val(r, "p25_price")      for r in rows])
    js_p75      = json.dumps([val(r, "p75_price")      for r in rows])
    # 0-waarden voor aantallen = ontbrekende data (API-fout), toon als null
    def val_count(r, k):
        v = val(r, k)
        return None if v == 0 else v
    js_listings = json.dumps([val_count(r, "total_listings") for r in rows])
    js_tickets  = json.dumps([val_count(r, "total_tickets")  for r in rows])
    below = val(last, "count_below_face")
    at_   = val(last, "count_at_face")
    above = val(last, "count_above_face")

    html = f"""<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DTRH 2026 — Resale Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #f5f5f7; color: #1d1d1f; padding: 24px 16px; }}
  h1   {{ font-size: 20px; font-weight: 600; margin-bottom: 4px; }}
  .sub {{ font-size: 13px; color: #6e6e73; margin-bottom: 24px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
           gap: 12px; margin-bottom: 24px; }}
  .card {{ background: #fff; border-radius: 12px; padding: 16px;
           box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  .card .label {{ font-size: 12px; color: #6e6e73; margin-bottom: 6px; }}
  .card .value {{ font-size: 26px; font-weight: 600; }}
  .card .sub2  {{ font-size: 11px; color: #6e6e73; margin-top: 3px; }}
  .green {{ color: #1a7f37; }}
  .red   {{ color: #c0392b; }}
  .chart-box {{ background: #fff; border-radius: 12px; padding: 20px;
                box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-bottom: 20px; }}
  .chart-box h2 {{ font-size: 14px; font-weight: 600; margin-bottom: 16px; color: #1d1d1f; }}
  .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }}
  .legend {{ display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 12px; font-size: 12px; color: #6e6e73; }}
  .legend span {{ display: flex; align-items: center; gap: 5px; }}
  .dot {{ width: 10px; height: 10px; border-radius: 2px; display: inline-block; }}
  .footer {{ font-size: 11px; color: #aeaeb2; text-align: center; margin-top: 24px; }}
  @media(max-width:600px){{ .two-col{{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<h1>DTRH 2026 — Tweedehands kaartmarkt</h1>
<p class="sub">Bijgewerkt: {updated} &nbsp;·&nbsp; Nog <strong>{days_left} dagen</strong> tot het festival (3 juli 2026) &nbsp;·&nbsp; {len(rows)} meting{'en' if len(rows)!=1 else ''}</p>

<div class="grid">
  <div class="card">
    <div class="label">Goedkoopste nu</div>
    <div class="value green">€{val(last,'min_price')}</div>
    <div class="sub2">–€{round(329 - float(last.get('min_price') or 329), 0):.0f} onder face</div>
  </div>
  <div class="card">
    <div class="label">Mediaan</div>
    <div class="value">€{val(last,'median_price')}</div>
    <div class="sub2">face value = €329</div>
  </div>
  <div class="card">
    <div class="label">Gemiddelde</div>
    <div class="value">€{val(last,'avg_price')}</div>
    <div class="sub2">+€{round(float(last.get('avg_price') or 329) - 329, 0):.0f} t.o.v. face</div>
  </div>
  <div class="card">
    <div class="label">Duurste</div>
    <div class="value red">€{val(last,'max_price')}</div>
    <div class="sub2">+€{round(float(last.get('max_price') or 329) - 329, 0):.0f} boven face</div>
  </div>
  <div class="card">
    <div class="label">Listings</div>
    <div class="value">{val(last,'total_listings')}</div>
    <div class="sub2">unieke aanbiedingen</div>
  </div>
  <div class="card">
    <div class="label">Kaarten totaal</div>
    <div class="value">{val(last,'total_tickets')}</div>
    <div class="sub2">incl. meerdere per listing</div>
  </div>
</div>

<div class="chart-box" style="margin-bottom:20px;">
  <h2>Prijsspreiding <span style="font-weight:400;color:#6e6e73;font-size:12px;">(laatste meting)</span></h2>
  <div style="font-size:11px;color:#6e6e73;display:flex;justify-content:space-between;margin-bottom:8px;">
    <span>€{val(last,'min_price')}</span>
    <span>face value €329</span>
    <span>€{val(last,'max_price')}</span>
  </div>
  <div style="position:relative;height:24px;margin-bottom:14px;">
    <div style="position:absolute;left:0;right:0;top:50%;height:6px;background:#e8e8ed;border-radius:3px;transform:translateY(-50%);"></div>
    <div id="iqrBar" style="position:absolute;top:50%;height:14px;background:#0969da;border-radius:4px;transform:translateY(-50%);
      left:{round((val(last,'p25_price')-val(last,'min_price'))/(val(last,'max_price')-val(last,'min_price'))*100,1)}%;
      width:{round((val(last,'p75_price')-val(last,'p25_price'))/(val(last,'max_price')-val(last,'min_price'))*100,1)}%;
      opacity:0.75;"></div>
    <div style="position:absolute;top:50%;width:3px;height:22px;background:#1d1d1f;border-radius:1px;transform:translate(-50%,-50%);
      left:{round((val(last,'median_price')-val(last,'min_price'))/(val(last,'max_price')-val(last,'min_price'))*100,1)}%;"></div>
    <div style="position:absolute;top:50%;width:2px;height:28px;background:#e6a817;border-radius:1px;transform:translate(-50%,-50%);
      left:{round((329-val(last,'min_price'))/(val(last,'max_price')-val(last,'min_price'))*100,1)}%;"></div>
  </div>
  <div style="display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:#6e6e73;">
    <span style="display:flex;align-items:center;gap:5px;"><span style="width:10px;height:10px;background:#1d1d1f;border-radius:2px;display:inline-block;"></span>Mediaan €{val(last,'median_price')}</span>
    <span style="display:flex;align-items:center;gap:5px;"><span style="width:10px;height:10px;background:#0969da;border-radius:2px;display:inline-block;opacity:.75;"></span>25e–75e percentiel (€{val(last,'p25_price')}–€{val(last,'p75_price')})</span>
    <span style="display:flex;align-items:center;gap:5px;"><span style="width:10px;height:10px;background:#e6a817;border-radius:2px;display:inline-block;"></span>Face value €329</span>
  </div>
</div>

<div class="chart-box">
  <h2>Prijsontwikkeling over tijd</h2>
  <div class="legend">
    <span><span class="dot" style="background:#1a7f37"></span>Minimumprijs</span>
    <span><span class="dot" style="background:#0969da"></span>Gemiddelde</span>
    <span><span class="dot" style="background:#6f42c1"></span>Mediaan</span>
    <span><span class="dot" style="background:#c0392b"></span>Maximumprijs</span>
    <span><span class="dot" style="background:#e6a817;border-radius:50%"></span>Face value €329</span>
  </div>
  <div style="position:relative;height:260px;">
    <canvas id="priceChart" role="img" aria-label="Prijsontwikkeling DTRH resale tickets over tijd"></canvas>
  </div>
</div>

<div class="two-col">
  <div class="chart-box">
    <h2>Aantal listings over tijd</h2>
    <div style="position:relative;height:200px;">
      <canvas id="listingsChart" role="img" aria-label="Aantal beschikbare listings over tijd"></canvas>
    </div>
  </div>
  <div class="chart-box">
    <h2>Onder / op / boven face value <span style="font-weight:400;color:#6e6e73">(laatste meting)</span></h2>
    <div class="legend">
      <span><span class="dot" style="background:#1a7f37"></span>Onder €329 ({below})</span>
      <span><span class="dot" style="background:#e6a817"></span>Op €329 ({at_})</span>
      <span><span class="dot" style="background:#c0392b"></span>Boven €329 ({above})</span>
    </div>
    <div style="position:relative;height:160px;">
      <canvas id="donutChart" role="img" aria-label="Verdeling onder, op en boven face value"></canvas>
    </div>
  </div>
</div>

<p class="footer">Gegenereerd door dtrh_scraper.py &nbsp;·&nbsp; Data: dtrh_resale_data.csv</p>

<script>
const dates    = {js_dates};
const minP     = {js_min};
const avgP     = {js_avg};
const medP     = {js_median};
const maxP     = {js_max};
const p25      = {js_p25};
const p75      = {js_p75};
const listings = {js_listings};
const tickets  = {js_tickets};

const gridColor = 'rgba(0,0,0,0.05)';
const tickColor = '#6e6e73';
const fmtEur = v => v === null ? null : '€' + v;

new Chart(document.getElementById('priceChart'), {{
  type: 'line',
  data: {{
    labels: dates,
    datasets: [
      {{ label:'Min', data:minP, borderColor:'#1a7f37', backgroundColor:'rgba(26,127,55,.08)',
         borderWidth:2, pointRadius:4, fill:false, tension:.3 }},
      {{ label:'Gem', data:avgP, borderColor:'#0969da', backgroundColor:'transparent',
         borderWidth:2, pointRadius:4, fill:false, tension:.3 }},
      {{ label:'Med', data:medP, borderColor:'#6f42c1', backgroundColor:'transparent',
         borderWidth:1.5, borderDash:[5,3], pointRadius:3, fill:false, tension:.3 }},
      {{ label:'Max', data:maxP, borderColor:'#c0392b', backgroundColor:'transparent',
         borderWidth:1.5, borderDash:[3,3], pointRadius:3, fill:false, tension:.3 }},
    ]
  }},
  options: {{
    responsive:true, maintainAspectRatio:false,
    plugins:{{
      legend:{{display:false}},
      annotation:{{
        annotations:{{ faceLine:{{
          type:'line', yMin:329, yMax:329,
          borderColor:'#e6a817', borderWidth:1.5, borderDash:[6,3],
          label:{{content:'Face value €329', display:true, position:'end',
                  backgroundColor:'rgba(230,168,23,.15)', color:'#9a6f00', font:{{size:11}}}}
        }}}}
      }}
    }},
    scales:{{
      x:{{ ticks:{{color:tickColor, maxTicksLimit:10, font:{{size:11}}}}, grid:{{color:gridColor}} }},
      y:{{ ticks:{{callback: v=>'€'+v, color:tickColor, font:{{size:11}}}}, grid:{{color:gridColor}},
           min:250, max:450 }}
    }}
  }}
}});

new Chart(document.getElementById('listingsChart'), {{
  type: 'line',
  data: {{
    labels: dates,
    datasets: [{{
      label:'Listings', data:listings,
      borderColor:'#0969da', backgroundColor:'rgba(9,105,218,.08)',
      borderWidth:2, pointRadius:4, fill:true, tension:.3
    }}]
  }},
  options: {{
    responsive:true, maintainAspectRatio:false,
    plugins:{{ legend:{{display:false}} }},
    scales:{{
      x:{{ ticks:{{color:tickColor, maxTicksLimit:6, font:{{size:11}}}}, grid:{{color:gridColor}} }},
      y:{{ ticks:{{color:tickColor, font:{{size:11}}}}, grid:{{color:gridColor}}, min:0 }}
    }}
  }}
}});

new Chart(document.getElementById('donutChart'), {{
  type: 'doughnut',
  data: {{
    labels:['Onder €329','Op €329','Boven €329'],
    datasets:[{{ data:[{below},{at_},{above}],
      backgroundColor:['#1a7f37','#e6a817','#c0392b'],
      borderWidth:2, borderColor:'#fff' }}]
  }},
  options:{{
    responsive:true, maintainAspectRatio:false, cutout:'62%',
    plugins:{{ legend:{{display:false}} }}
  }}
}});
</script>
</body>
</html>"""

    out = PROJECT_DIR / "dtrh_dashboard.html"
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"Dashboard bijgewerkt: {out}")

# ─── CSV opslaan ──────────────────────────────────────────────────────────────
def save_row(total_listings: int, total_tickets: int,
             price_stats: Dict, notes: str = "") -> None:
    today = date.today()
    row = {
        "timestamp":           datetime.now().isoformat(timespec="seconds"),
        "date":                today.isoformat(),
        "days_until_festival": (FESTIVAL_START - today).days,
        "total_listings":      total_listings,
        "total_tickets":       total_tickets,
        "face_value":          FACE_VALUE_EUR,
        "notes":               notes,
        **price_stats,
    }
    file_exists = CSV_FILE.exists()
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)
    log(
        f"✓ Opgeslagen | listings: {total_listings} | kaarten: {total_tickets} | "
        f"min €{price_stats.get('min_price', 'N/A')} | "
        f"max €{price_stats.get('max_price', 'N/A')} | "
        f"gem €{price_stats.get('avg_price', 'N/A')}"
    )

# ─── Main ─────────────────────────────────────────────────────────────────────
async def main_async() -> None:
    log("=" * 60)
    log("DTRH Resale Tracker gestart")
    log(f"Festival: {FESTIVAL_START}  |  Nog {(FESTIVAL_START - date.today()).days} dagen")

    # Prijzen + aantallen via Playwright (browser onderschept de API)
    prices, total_listings, total_tickets = await fetch_prices_playwright()

    # Samenvoegen en opslaan
    price_stats = calc_price_stats(prices)

    if total_listings == 0 and not prices:
        notes = "Geen data gevonden — mogelijk geen kaarten beschikbaar of API gewijzigd"
    elif total_listings == 0:
        notes = "Aantallen niet beschikbaar (API niet onderschept); prijzen via DOM"
    elif not prices:
        notes = "Prijzen via DOM niet beschikbaar; alleen aantallen via API"
    else:
        notes = ""

    save_row(total_listings, total_tickets, price_stats, notes=notes)
    generate_dashboard()


def main() -> None:
    try:
        import playwright  # noqa: F401
    except ImportError:
        log("Playwright niet geïnstalleerd. Voer uit:", "ERROR")
        log("  pip3 install playwright", "ERROR")
        log("  /Users/richie/Library/Python/3.9/bin/playwright install chromium", "ERROR")
        return
    asyncio.run(main_async())


if __name__ == "__main__":
    main()

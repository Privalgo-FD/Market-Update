import os
import re
import ssl
import json
import time
import smtplib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import feedparser
import requests
import anthropic


RECIPIENTS = [
    "rwallroth@privalgo.eu",
    "mheijnen@privalgo.eu",
    "Bhessels@privalgo.eu",
    "ton@stadsmeester.nl",
    "alogean@privalgo.eu",
    "b.veenbrink@stadiumconsultancy.com",
    "plucassen@privalgo.eu",
    "sales@privalgo.co.uk",
    "whendriks@privalgo.co.uk",
]

ECB_RSS = "https://www.ecb.europa.eu/press/rss/press_release.en.rss"
FED_RSS = "https://www.federalreserve.gov/feeds/press_monetary.xml"
REUTERS_BUSINESS_RSS = "https://feeds.reuters.com/reuters/businessNews"
REUTERS_WEALTH_RSS = "https://feeds.reuters.com/news/wealth"

# Forex Factory economic calendar (served by FairEconomy Media as structured JSON).
# The public website blocks automated access; these weekly feeds are the supported route.
# Rate limit: max 2 downloads per 5 minutes across all file types.
FF_THISWEEK_JSON = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_NEXTWEEK_JSON = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"

# Only surface events for the currencies Privalgo actually runs risk on.
RELEVANT_CURRENCIES = {"USD", "EUR", "GBP", "CHF", "JPY"}


SMTP_SERVER = os.environ["SMTP_SERVER"]
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ["SMTP_USERNAME"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]
MAIL_FROM = os.environ["MAIL_FROM"]

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ALPHA_VANTAGE_API_KEY = os.environ["ALPHA_VANTAGE_API_KEY"]
FRED_API_KEY = os.environ["FRED_API_KEY"]

# Anthropic client — replaces OpenAI
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

ET = ZoneInfo("America/New_York")
CET = ZoneInfo("Europe/Amsterdam")


def safe_get_json(url: str, params: dict | None = None) -> dict:
    response = requests.get(url, params=params, timeout=25)
    response.raise_for_status()
    return response.json()



def fetch_alpha_vantage_fx() -> dict:
    pairs = {
        "EURUSD": ("EUR", "USD"),
        "EURGBP": ("EUR", "GBP"),
        "EURCHF": ("EUR", "CHF"),
        "EURJPY": ("EUR", "JPY"),
        "USDJPY": ("USD", "JPY"),
    }

    results = {}

    for label, (from_ccy, to_ccy) in pairs.items():
        data = safe_get_json(
            "https://www.alphavantage.co/query",
            params={
                "function": "CURRENCY_EXCHANGE_RATE",
                "from_currency": from_ccy,
                "to_currency": to_ccy,
                "apikey": ALPHA_VANTAGE_API_KEY,
            },
        )

        block = data.get("Realtime Currency Exchange Rate", {})
        rate = block.get("5. Exchange Rate")

        if not rate:
            raise ValueError(f"Missing FX rate for {label}: {data}")

        results[label] = round(float(rate), 4)
        time.sleep(1.1)

    return results


def fetch_yfinance_price(ticker: str) -> float:
    """
    Generic yfinance price fetch. Works for equities, indices, crypto, futures.
    Returns the last traded price, falling back to most recent closing price.
    """
    import yfinance as yf

    t = yf.Ticker(ticker)
    info = t.fast_info
    price = getattr(info, "last_price", None) or getattr(info, "regularMarketPrice", None)

    if price:
        return round(float(price), 2)

    hist = t.history(period="2d")
    if not hist.empty:
        return round(float(hist["Close"].iloc[-1]), 2)

    raise ValueError(f"Unable to fetch price for {ticker} from Yahoo Finance")


def fetch_gold_spot() -> float:
    return fetch_yfinance_price("GC=F")


def fetch_bitcoin() -> float:
    """BTC/USD spot price via Yahoo Finance."""
    return fetch_yfinance_price("BTC-USD")


def fetch_nikkei() -> float:
    """Nikkei 225 index level via Yahoo Finance."""
    return fetch_yfinance_price("^N225")


def fetch_fred_latest(series_id: str) -> dict:
    data = safe_get_json(
        "https://api.stlouisfed.org/fred/series/observations",
        params={
            "series_id": series_id,
            "api_key": FRED_API_KEY,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 5,
        },
    )

    observations = data.get("observations", [])
    for obs in observations:
        value = obs.get("value")
        if value not in (".", None, ""):
            return {
                "date": obs.get("date"),
                "value": float(value),
            }

    raise ValueError(f"No usable FRED data for {series_id}")


def fetch_us10y() -> float:
    """US 10Y Treasury yield via yfinance ^TNX (real-time during market hours)."""
    return fetch_yfinance_price("^TNX")


def fetch_brent() -> float:
    """Brent crude front-month futures via yfinance BZ=F (continuously traded)."""
    return fetch_yfinance_price("BZ=F")


def fetch_market_indicators() -> dict:
    sp500 = fetch_fred_latest("SP500")
    gold = fetch_gold_spot()
    time.sleep(1.1)

    vix = fetch_fred_latest("VIXCLS")
    us10y = fetch_us10y()
    time.sleep(1.1)

    brent = fetch_brent()
    time.sleep(1.1)

    bitcoin = fetch_bitcoin()
    time.sleep(1.1)
    nikkei = fetch_nikkei()

    return {
        "SP500": sp500,
        "GOLD": gold,
        "VIX": vix,
        "US10Y": us10y,
        "BRENT": brent,
        "BITCOIN": bitcoin,
        "NIKKEI": nikkei,
    }


def fetch_rss_items(feed_url: str, max_items: int = 4) -> list[dict]:
    feed = feedparser.parse(feed_url)
    items = []

    for entry in feed.entries[:max_items]:
        items.append(
            {
                "title": entry.get("title", "").strip(),
                "summary": entry.get("summary", "").strip(),
                "link": entry.get("link", "").strip(),
            }
        )

    return items


def fetch_headlines() -> dict:
    return {
        "ecb": fetch_rss_items(ECB_RSS, max_items=3),
        "fed": fetch_rss_items(FED_RSS, max_items=3),
        "reuters_business": fetch_rss_items(REUTERS_BUSINESS_RSS, max_items=4),
        "reuters_wealth": fetch_rss_items(REUTERS_WEALTH_RSS, max_items=3),
    }


def flatten_headlines(headlines: dict) -> str:
    lines = []

    for bucket, items in headlines.items():
        lines.append(f"{bucket.upper()}:")
        if not items:
            lines.append("- No items")
            continue

        for item in items:
            title = item.get("title", "")
            summary = item.get("summary", "")
            lines.append(f"- {title} | {summary}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Economic calendar
#
# Primary source: Forex Factory (via FairEconomy Media JSON feed). This gives
# high-impact macro releases and central bank activity across all of Privalgo's
# currencies, which is the material the readership actually wants.
# Fallback source: FRED release calendar (US only), used if Forex Factory is
# unavailable or rate-limited.
# ---------------------------------------------------------------------------

# Substrings (lower-case) that identify a central bank event in a Forex Factory title.
CENTRAL_BANK_KEYWORDS = (
    "fomc", "federal funds rate", "fed chair", "fed monetary", "beige book",
    "ecb", "main refinancing rate", "deposit facility rate", "lagarde",
    "monetary policy statement", "monetary policy summary", "monetary policy report",
    "monetary policy meeting accounts", "rate statement", "press conference",
    "official bank rate", "bank rate", "mpc official", "mpc member", "gov bailey",
    "snb", "snb policy rate", "snb chairman",
    "boj", "boj policy rate", "boj outlook", "boj press",
    "rate decision", "policy rate", "interest rate", "rate vote",
)

# Titles of central bank speakers whose "Speaks" events should count as CB activity.
CENTRAL_BANK_SPEAKERS = (
    "fed ", "fomc", "ecb", "boe", "boj", "snb",
    "gov ", "governor", "president", "chair", "mpc",
)


def is_central_bank_event(title: str) -> bool:
    t = (title or "").lower()
    if any(keyword in t for keyword in CENTRAL_BANK_KEYWORDS):
        return True
    if "speaks" in t and any(speaker in t for speaker in CENTRAL_BANK_SPEAKERS):
        return True
    return False


def fetch_ff_json(url: str) -> list:
    """
    Fetch a Forex Factory weekly calendar feed.
    When rate-limited or blocked, the endpoint returns an HTML page rather than
    JSON. We detect that and raise, so the caller can fall back cleanly.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; PrivalgoMarketUpdate/1.0)",
        "Accept": "application/json",
    }
    response = requests.get(url, headers=headers, timeout=25)
    response.raise_for_status()

    text = response.text.strip()
    if not text or text[0] not in "[{" or "Request Denied" in text:
        raise RuntimeError("Forex Factory returned a block/rate-limit page instead of JSON")

    data = json.loads(text)
    if not isinstance(data, list):
        raise RuntimeError("Unexpected Forex Factory payload (not a list)")
    return data


def parse_ff_events(raw: list) -> list[dict]:
    """Convert raw Forex Factory rows into the unified event shape, filtered to
    Privalgo's relevant currencies."""
    events = []
    for item in raw:
        currency = (item.get("country") or "").upper()
        if currency not in RELEVANT_CURRENCIES:
            continue

        date_str = item.get("date")
        if not date_str:
            continue
        try:
            # ISO 8601 with an embedded ET offset, e.g. 2026-07-29T14:00:00-04:00
            release_dt = datetime.fromisoformat(date_str)
        except ValueError:
            continue

        title = (item.get("title") or "").strip()
        events.append(
            {
                "label": title,
                "currency": currency,
                "impact": (item.get("impact") or "").strip() or "Low",
                "forecast": (item.get("forecast") or "").strip(),
                "previous": (item.get("previous") or "").strip(),
                "release_et": release_dt.astimezone(ET),
                "is_central_bank": is_central_bank_event(title),
                "reference_period": "",
            }
        )
    return events


def build_calendar_from_events(events: list[dict], source: str) -> dict:
    """Bucket a flat list of unified events into the structure the rest of the
    pipeline consumes."""
    now_cet = datetime.now(CET)
    today_cet = now_cet.date()

    events_sorted = sorted(events, key=lambda e: e["release_et"])

    def cet_date(event):
        return event["release_et"].astimezone(CET).date()

    today = [e for e in events_sorted if cet_date(e) == today_cet]

    upcoming = [e for e in events_sorted if e["release_et"] >= now_cet]
    high_impact_upcoming = [e for e in upcoming if e["impact"].lower() == "high"][:8]

    central_banks = [
        e for e in events_sorted
        if e["is_central_bank"] and cet_date(e) >= today_cet
    ][:8]

    # Primary list used for the template and prompt: today's events if any,
    # otherwise the next high-impact events.
    primary = today if today else high_impact_upcoming[:5]

    # Market mover: soonest upcoming high-impact event, then soonest CB event,
    # then simply the next event.
    market_mover = next((e for e in upcoming if e["impact"].lower() == "high"), None)
    if market_mover is None:
        market_mover = next((e for e in upcoming if e["is_central_bank"]), None)
    if market_mover is None and upcoming:
        market_mover = upcoming[0]

    return {
        "events": primary[:8],
        "today": today,
        "high_impact_upcoming": high_impact_upcoming,
        "central_banks": central_banks,
        "market_mover": market_mover,
        "source": source,
    }


def fetch_forexfactory_calendar() -> dict:
    """Primary calendar source. Always pulls the current week; adds next week
    only late in the week so there is still forward visibility, while staying
    within the 2-requests-per-5-minutes limit."""
    raw = fetch_ff_json(FF_THISWEEK_JSON)

    # Thursday (3) or Friday (4): pull next week too for lookahead. Best-effort.
    if datetime.now(CET).weekday() >= 3:
        try:
            raw = raw + fetch_ff_json(FF_NEXTWEEK_JSON)
        except Exception as e:
            print(f"Note: Forex Factory next-week feed unavailable ({e}). Using this week only.")

    events = parse_ff_events(raw)
    if not events:
        raise ValueError("No relevant Forex Factory events after filtering")

    return build_calendar_from_events(events, source="Forex Factory")


def fetch_economic_calendar() -> dict:
    """
    Fallback source. Upcoming US macro releases from the FRED release calendar
    API (CPI, PPI, Employment Situation), mapped into the unified event shape.
    """
    target_releases = {
        10: "US CPI",
        52: "US PPI",
        50: "US Employment Situation (NFP / Unemployment)",
    }

    now_et = datetime.now(ET)
    date_from = now_et.strftime("%Y-%m-%d")
    date_to = (now_et + timedelta(days=90)).strftime("%Y-%m-%d")

    events = []

    for release_id, label in target_releases.items():
        data = safe_get_json(
            "https://api.stlouisfed.org/fred/release/dates",
            params={
                "release_id": release_id,
                "api_key": FRED_API_KEY,
                "file_type": "json",
                "realtime_start": date_from,
                "realtime_end": date_to,
                "include_release_dates_with_no_data": "true",
                "sort_order": "asc",
                "limit": 5,
            },
        )

        release_dates = data.get("release_dates", [])
        for entry in release_dates:
            date_str = entry.get("date")
            if not date_str:
                continue

            # FRED dates are date-only; BLS releases at 08:30 ET
            dt_et = datetime.strptime(date_str, "%Y-%m-%d").replace(
                hour=8, minute=30, tzinfo=ET
            )

            if dt_et < now_et:
                continue

            events.append({
                "label": label,
                "currency": "USD",
                "impact": "High",
                "forecast": "",
                "previous": "",
                "release_et": dt_et,
                "is_central_bank": is_central_bank_event(label),
                "reference_period": "upcoming release",
            })
            break  # Only take the next upcoming date per release

        time.sleep(0.5)

    if not events:
        raise ValueError("No upcoming events found in FRED release calendar")

    return build_calendar_from_events(events, source="FRED (fallback)")


def format_event_line(event: dict) -> str:
    dt_cet = event["release_et"].astimezone(CET)
    dt_et = event["release_et"].astimezone(ET)

    cet_str = dt_cet.strftime("%a %d %b, %H:%M CET")
    et_str = dt_et.strftime("%H:%M ET")

    currency = event.get("currency", "")
    impact = event.get("impact", "")
    tag = f" ({currency}, {impact})" if currency else ""

    forecast = event.get("forecast", "")
    previous = event.get("previous", "")
    figures = ""
    if forecast or previous:
        figures = f" [fc {forecast or 'n/a'} / prev {previous or 'n/a'}]"

    return f"{event['label']}{tag}{figures}: {cet_str} / {et_str}"


def fallback_calendar() -> dict:
    """Minimal calendar structure used when every calendar source is unavailable."""
    return {
        "events": [],
        "today": [],
        "high_impact_upcoming": [],
        "central_banks": [],
        "market_mover": None,
        "source": "unavailable",
    }


def build_econ_calendar_html(calendar: dict) -> str:
    today = calendar.get("today") or []
    central_banks = calendar.get("central_banks") or []
    sections = []

    if today:
        rows = "<br>".join(f"&bull; {format_event_line(e)}" for e in today)
        sections.append(f"<strong>Today's scheduled releases</strong><br>{rows}")
    else:
        upcoming = calendar.get("high_impact_upcoming") or calendar.get("events") or []
        if upcoming:
            rows = "<br>".join(f"&bull; {format_event_line(e)}" for e in upcoming[:5])
            sections.append(
                f"<strong>No major releases today. Next high-impact events</strong><br>{rows}"
            )
        else:
            sections.append("Economic calendar temporarily unavailable.")

    if central_banks:
        rows = "<br>".join(f"&bull; {format_event_line(e)}" for e in central_banks)
        sections.append(f"<strong>Central bank focus (this week)</strong><br>{rows}")

    return "<br><br>".join(sections)


def build_market_mover_text(calendar: dict) -> str:
    mover = calendar.get("market_mover")
    if not mover:
        return "Next market mover data temporarily unavailable."
    return f"Next key scheduled mover: {format_event_line(mover)}"


def build_prompt(fx_rates: dict, indicators: dict, headlines: dict, calendar: dict) -> str:
    headline_text = flatten_headlines(headlines)
    generated_at = datetime.now(CET).strftime("%Y-%m-%d %H:%M CET")

    def fmt_list(events):
        lines = [f"- {format_event_line(e)}" for e in (events or [])]
        return "\n".join(lines) if lines else "- None scheduled"

    today_lines = fmt_list(calendar.get("today"))
    cb_lines = fmt_list(calendar.get("central_banks"))
    hi_lines = fmt_list(calendar.get("high_impact_upcoming"))
    source = calendar.get("source", "unknown")

    return f"""
You are a senior FX and macro strategist at Privalgo, an EMI specialising in FX risk
management and international payment rails for corporate clients. Write a substantive
morning briefing for colleagues in trading, relationship management, and client-facing roles.

Use ONLY the data provided below. Do not invent numbers, events, causal explanations,
or market narratives. If the data does not explain a move, describe the observation only.

EDITORIAL PRIORITY (important):
This briefing is about MACRO-ECONOMIC EVENTS and CENTRAL BANK ACTIVITY first. Lead with
scheduled data releases and central bank actions. Commodity, crypto and equity-index levels
(gold, Brent, Bitcoin, Nikkei) are SECONDARY CONTEXT only. Do NOT open the briefing with
where gold is trading. Only mention gold if it is genuinely relevant to the day's macro story.

DATA TIMESTAMP: {generated_at}
CALENDAR SOURCE: {source}

FX SNAPSHOT (core):
EUR/USD: {fx_rates['EURUSD']}
EUR/GBP: {fx_rates['EURGBP']}
EUR/CHF: {fx_rates['EURCHF']}
EUR/JPY: {fx_rates['EURJPY']}
USD/JPY: {fx_rates['USDJPY']}

TODAY'S SCHEDULED MACRO RELEASES (Forex Factory calendar, times shown CET / ET):
{today_lines}

CENTRAL BANK ACTIVITY THIS WEEK (rate decisions, statements, press conferences, official speeches):
{cb_lines}

HIGH-IMPACT EVENTS AHEAD:
{hi_lines}

RATES AND RISK CONTEXT:
S&P 500: {indicators['SP500']['value']} (date: {indicators['SP500']['date']})
VIX: {indicators['VIX']['value']} (date: {indicators['VIX']['date']})
US 10Y yield: {indicators['US10Y']}
Brent spot: {indicators['BRENT']}

SECONDARY CONTEXT (do not lead with these):
Gold spot: {indicators['GOLD']}
Bitcoin (BTC/USD): {indicators['BITCOIN']}
Nikkei 225: {indicators['NIKKEI']}

HEADLINES (ECB / Fed / Reuters):
{headline_text}

FIELD DEFINITIONS:
- key_market: The single most important MACRO or CENTRAL BANK development for today, drawn from
  the scheduled releases and central bank activity above. Lead with the event and its forecast
  vs previous where given. Do NOT lead with gold, oil, crypto or index levels unless there is
  genuinely no macro or central bank event of note today.
- central_banks: PRIORITY SECTION. Summarise the central bank events listed and their read-through
  for EUR, USD, GBP, CHF and JPY. Cover rate decisions, policy statements, press conferences and
  official speeches. Connect them to rate expectations and FX implications. If none:
  "No central bank events scheduled in the relevant window."
- macro_watch: Walk through today's scheduled releases in time order (CET). For each material
  release, state forecast vs previous where given and why it matters for the relevant currency
  pair and for corporate payment flows. If nothing is scheduled today, point to the next
  high-impact events instead.
- fx: EUR/USD, EUR/GBP, EUR/CHF, EUR/JPY, USD/JPY. Direction and the likely macro driver from the
  events above. Flag whether current levels represent a hedging opportunity or risk for corporates
  with EUR exposure. Reference the actual rate levels.
- impact: A specific corporate use-case affected by today's conditions (hedging timing, invoice
  currency exposure, cash repatriation). Be concrete: reference actual rate levels and the scheduled
  events that could move them, and what action that implies.
- client_talking_point: One practical, grounded observation a relationship manager could use in a
  client call today, anchored in the day's macro or central bank calendar. Commercially useful,
  not pushy.

RULES:
- Return valid JSON only. No preamble, no markdown. First character must be {{.
- Both "en" and "nl" fields are required.
- Each field: 3-5 sentences. Be substantive and specific. Reference the actual data values and the
  scheduled events. Avoid vague generalisations and avoid em dashes.
- Dutch must be a natural rewrite using standard financial terminology (wisselkoers,
  renteverwachtingen, valutarisico, rentebesluit), not a literal translation.

Return this structure:
{{
  "en": {{
    "key_market": "...",
    "fx": "...",
    "central_banks": "...",
    "macro_watch": "...",
    "impact": "...",
    "client_talking_point": "..."
  }},
  "nl": {{
    "key_market": "...",
    "fx": "...",
    "central_banks": "...",
    "macro_watch": "...",
    "impact": "...",
    "client_talking_point": "..."
  }}
}}
"""


def strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    return text.strip()


def generate_market_update(fx_rates: dict, indicators: dict, headlines: dict, calendar: dict) -> dict:
    prompt = build_prompt(fx_rates, indicators, headlines, calendar)

    max_retries = 4
    backoff_seconds = [10, 20, 40, 60]

    for attempt, wait in enumerate(backoff_seconds, start=1):
        try:
            response = anthropic_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                system="You write precise, substantive market briefings for corporate FX clients and return strict JSON only. No preamble, no markdown fences.",
                messages=[
                    {
                        "role": "user",
                        "content": prompt,
                    }
                ],
            )
            content = strip_code_fences(response.content[0].text)
            return json.loads(content)

        except anthropic.APIStatusError as e:
            if e.status_code == 529 and attempt < max_retries:
                print(f"Anthropic API overloaded (529). Retrying in {wait}s (attempt {attempt}/{max_retries})...")
                time.sleep(wait)
            else:
                raise

    raise RuntimeError("Anthropic API remained overloaded after all retry attempts.")


def load_template(path: str = "email_template.html") -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def fill_template(template: str, fx_rates: dict, indicators: dict, market_update: dict, calendar: dict) -> str:
    today = datetime.now(CET).strftime("%d %B %Y")

    disclaimer = (
        "This update is for information purposes only and does not constitute investment advice, "
        "a recommendation, or an offer to buy or sell any financial instrument. Market levels, "
        "economic dates and commentary are provided on a best-efforts basis and may be delayed, "
        "revised or incomplete. Recipients remain responsible for their own assessment and any "
        "decision-making."
    )

    replacements = {
        "{{DATE}}": today,

        "{{EURUSD}}": fx_rates["EURUSD"],
        "{{EURGBP}}": fx_rates["EURGBP"],
        "{{EURCHF}}": fx_rates["EURCHF"],
        "{{EURJPY}}": fx_rates["EURJPY"],
        "{{USDJPY}}": fx_rates["USDJPY"],

        "{{SP500}}": f"{indicators['SP500']['value']}",
        "{{GOLD}}": f"{indicators['GOLD']}",
        "{{VIX}}": f"{indicators['VIX']['value']}",
        "{{US10Y}}": f"{indicators['US10Y']}",
        "{{BRENT}}": f"{indicators['BRENT']}",
        "{{BITCOIN}}": f"{indicators['BITCOIN']:,.0f}",
        "{{NIKKEI}}": f"{indicators['NIKKEI']:,.0f}",

        "{{EN_KEY_MARKET}}": market_update["en"]["key_market"],
        "{{EN_FX}}": market_update["en"]["fx"],
        "{{EN_CB}}": market_update["en"]["central_banks"],
        "{{EN_MACRO}}": market_update["en"]["macro_watch"],
        "{{EN_IMPACT}}": market_update["en"]["impact"],
        "{{EN_TALKING_POINT}}": market_update["en"]["client_talking_point"],

        "{{NL_KEY_MARKET}}": market_update["nl"]["key_market"],
        "{{NL_FX}}": market_update["nl"]["fx"],
        "{{NL_CB}}": market_update["nl"]["central_banks"],
        "{{NL_MACRO}}": market_update["nl"]["macro_watch"],
        "{{NL_IMPACT}}": market_update["nl"]["impact"],
        "{{NL_TALKING_POINT}}": market_update["nl"]["client_talking_point"],

        "{{ECON_CALENDAR}}": build_econ_calendar_html(calendar),
        "{{MARKET_MOVER}}": build_market_mover_text(calendar),
        "{{DISCLAIMER}}": disclaimer,
    }

    html = template
    for key, value in replacements.items():
        html = html.replace(key, str(value))

    return html


def send_email(html_body: str) -> None:
    subject = f"Privalgo Daily Market Update – {datetime.now(CET).strftime('%d %B %Y')}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_FROM
    msg["Reply-To"] = "whendriks@privalgo.co.uk"

    text_body = "This email contains an HTML market update. Please view it in an HTML-compatible email client."
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls(context=context)
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(MAIL_FROM, [MAIL_FROM] + RECIPIENTS, msg.as_string())


def main():
    fx_rates = fetch_alpha_vantage_fx()
    indicators = fetch_market_indicators()
    headlines = fetch_headlines()

    try:
        calendar = fetch_forexfactory_calendar()
    except Exception as e:
        print(f"Warning: Forex Factory calendar fetch failed ({e}). Falling back to FRED release calendar.")
        try:
            calendar = fetch_economic_calendar()
        except Exception as e2:
            print(f"Warning: FRED calendar also failed ({e2}). Proceeding with empty calendar.")
            calendar = fallback_calendar()

    market_update = generate_market_update(fx_rates, indicators, headlines, calendar)
    template = load_template()
    html_body = fill_template(template, fx_rates, indicators, market_update, calendar)
    send_email(html_body)
    print("Market update email sent successfully.")


if __name__ == "__main__":
    main()
    

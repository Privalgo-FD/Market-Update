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
    "plucassen@privalgo.eu",
    "whendriks@privalgo.co.uk",
]

ECB_RSS = "https://www.ecb.europa.eu/press/rss/press_release.en.rss"
FED_RSS = "https://www.federalreserve.gov/feeds/press_monetary.xml"
REUTERS_BUSINESS_RSS = "https://feeds.reuters.com/reuters/businessNews"
REUTERS_WEALTH_RSS = "https://feeds.reuters.com/news/wealth"


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


def fetch_economic_calendar() -> dict:
    """
    Fetches upcoming US macro releases from the FRED release calendar API.
    Targets CPI, PPI and Employment Situation (NFP).
    Uses include_release_dates_with_no_data=true to retrieve scheduled
    future dates which have not yet been published.
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
                "reference_period": "upcoming release",
                "release_et": dt_et,
            })
            break  # Only take the next upcoming date per release

        time.sleep(0.5)

    if not events:
        raise ValueError("No upcoming events found in FRED release calendar")

    events_sorted = sorted(events, key=lambda x: x["release_et"])

    return {
        "events": events_sorted[:3],
        "market_mover": events_sorted[0],
    }



def format_event_line(event: dict) -> str:
    dt_et = event["release_et"]
    dt_cet = dt_et.astimezone(CET)

    et_str = dt_et.strftime("%d %b %Y, %H:%M ET")
    cet_str = dt_cet.strftime("%d %b %Y, %H:%M CET")

    return f"{event['label']} ({event['reference_period']}): {et_str} / {cet_str}"


def fallback_calendar() -> dict:
    """Returns a minimal calendar structure used when the BLS feed is unavailable."""
    return {
        "events": [],
        "market_mover": None,
    }


def build_econ_calendar_html(calendar: dict) -> str:
    if not calendar["events"]:
        return "Economic calendar temporarily unavailable."
    lines = []
    for event in calendar["events"]:
        lines.append(f"• {format_event_line(event)}")
    return "<br>".join(lines)


def build_market_mover_text(calendar: dict) -> str:
    if not calendar["market_mover"]:
        return "Next market mover data temporarily unavailable."
    mover = calendar["market_mover"]
    return f"Next key scheduled mover: {format_event_line(mover)}"


def build_prompt(fx_rates: dict, indicators: dict, headlines: dict, calendar: dict) -> str:
    headline_text = flatten_headlines(headlines)
    generated_at = datetime.now(CET).strftime("%Y-%m-%d %H:%M CET")
    econ_lines = "\n".join([f"- {format_event_line(e)}" for e in calendar["events"]])

    return f"""
You are a senior FX analyst at Privalgo, an EMI specialising in FX risk management 
and international payment rails for corporate clients. Write a substantive morning briefing 
for colleagues in trading, relationship management, and client-facing roles.

Use ONLY the data provided below. Do not invent numbers, events, causal explanations, 
or market narratives. If the data does not explain a move, describe the observation 
only — do not speculate on the cause.

DATA TIMESTAMP: {generated_at}

FX SNAPSHOT:
EUR/USD: {fx_rates['EURUSD']}
EUR/GBP: {fx_rates['EURGBP']}
EUR/CHF: {fx_rates['EURCHF']}
EUR/JPY: {fx_rates['EURJPY']}
USD/JPY: {fx_rates['USDJPY']}

MARKET INDICATORS:
S&P 500: {indicators['SP500']['value']} (date: {indicators['SP500']['date']})
Gold spot: {indicators['GOLD']}
VIX: {indicators['VIX']['value']} (date: {indicators['VIX']['date']})
US 10Y yield: {indicators['US10Y']}
Brent spot: {indicators['BRENT']}
Bitcoin (BTC/USD): {indicators['BITCOIN']}
Nikkei 225: {indicators['NIKKEI']}

ECONOMIC CALENDAR:
{econ_lines}

MARKET MOVER:
{build_market_mover_text(calendar)}

HEADLINES:
{headline_text}

FIELD DEFINITIONS:
- key_market: The single most important macro or market development. Lead with a number. 
  Provide context: what level is this relative to recent history, and why does it matter 
  for corporate FX clients?
- fx: EUR/USD, EUR/GBP, EUR/CHF — direction, magnitude, likely driver. Flag notable levels. 
  Comment on whether current levels represent a hedging opportunity or risk for corporates 
  with EUR exposure.
- central_banks: Relevant central bank signals or rate expectations from the data/headlines. 
  Connect rate expectations to FX implications where the data supports it. 
  If nothing relevant: "No major central bank signals today."
- macro_watch: Today's scheduled releases — what is expected and why it matters. 
  Reference the upcoming calendar events and explain their relevance to EUR pairs and 
  corporate payment flows. If calendar is empty: "No major scheduled releases today — 
  markets may trade on technicals."
- impact: Name a specific corporate use-case affected by today's conditions (hedging timing, 
  invoice currency exposure, cash repatriation, etc.). Be concrete: reference actual rate 
  levels and what action that implies.
- client_talking_point: One practical, grounded observation a relationship manager could 
  use in a client call today. Commercially useful, not pushy. Should feel like something 
  a trusted advisor would say — not a generic market comment.

RULES:
- Return valid JSON only. No preamble, no markdown. First character must be {{.
- Both "en" and "nl" fields are required.
- Each field: 3–5 sentences. Be substantive and specific — reference the actual data 
  values provided. Avoid vague generalisations.
- Dutch must be a natural rewrite using standard financial terminology 
  (wisselkoers, renteverwachtingen, valutarisico), not a literal translation.
- Refer to actual indicator values where relevant.

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
        calendar = fetch_economic_calendar()
    except Exception as e:
        print(f"Warning: economic calendar fetch failed ({e}). Proceeding with fallback.")
        calendar = fallback_calendar()

    market_update = generate_market_update(fx_rates, indicators, headlines, calendar)
    template = load_template()
    html_body = fill_template(template, fx_rates, indicators, market_update, calendar)
    send_email(html_body)
    print("Market update email sent successfully.")


if __name__ == "__main__":
    main()

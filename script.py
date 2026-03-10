import os
import re
import ssl
import json
import time
import smtplib
from datetime import datetime
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import feedparser
import requests
from openai import OpenAI


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

BLS_ICS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"

SMTP_SERVER = os.environ["SMTP_SERVER"]
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ["SMTP_USERNAME"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]
MAIL_FROM = os.environ["MAIL_FROM"]

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
ALPHA_VANTAGE_API_KEY = os.environ["ALPHA_VANTAGE_API_KEY"]
FRED_API_KEY = os.environ["FRED_API_KEY"]

client = OpenAI(api_key=OPENAI_API_KEY)

ET = ZoneInfo("America/New_York")
CET = ZoneInfo("Europe/Amsterdam")


def safe_get_json(url: str, params: dict | None = None) -> dict:
    response = requests.get(url, params=params, timeout=25)
    response.raise_for_status()
    return response.json()


def safe_get_ics(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/calendar,text/plain,*/*",
        "Cache-Control": "no-cache",
    }
    response = requests.get(url, headers=headers, timeout=25)
    response.raise_for_status()
    return response.text


def fetch_alpha_vantage_fx() -> dict:
    pairs = {
        "EURUSD": ("EUR", "USD"),
        "EURGBP": ("EUR", "GBP"),
        "EURCHF": ("EUR", "CHF"),
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


def fetch_gold_spot() -> float:
    """
    Fetch gold spot price (XAU/USD).

    Source priority:
      1. metals.live free public API (~15 min delayed, no key required)
      2. Alpha Vantage COMMODITY_EXCHANGE_RATE (XAU -> USD) — fallback
    """
    # --- Source 1: metals.live ---
    try:
        data = safe_get_json("https://metals.live/api/v1/spot")
        for item in data:
            if isinstance(item, dict) and item.get("metal", "").upper() in ("XAU", "GOLD"):
                price = item.get("price") or item.get("ask")
                if price:
                    return round(float(price), 2)
    except Exception:
        pass

    # --- Source 2: Alpha Vantage fallback ---
    data = safe_get_json(
        "https://www.alphavantage.co/query",
        params={
            "function": "COMMODITY_EXCHANGE_RATE",
            "from_commodity": "XAU",
            "to_currency": "USD",
            "apikey": ALPHA_VANTAGE_API_KEY,
        },
    )
    block = data.get("Realtime Commodity Exchange Rate", {})
    rate = block.get("5. Exchange Rate")
    if rate:
        return round(float(rate), 2)

    raise ValueError("Unable to fetch gold spot price from any source")


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


def fetch_market_indicators() -> dict:
    sp500 = fetch_fred_latest("SP500")
    gold = fetch_gold_spot()
    time.sleep(1.1)

    vix = fetch_fred_latest("VIXCLS")
    us10y = fetch_fred_latest("DGS10")
    brent = fetch_fred_latest("DCOILBRENTEU")

    return {
        "SP500": sp500,
        "GOLD": gold,
        "VIX": vix,
        "US10Y": us10y,
        "BRENT": brent,
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


def parse_ics_datetime(dt_raw: str) -> datetime | None:
    dt_clean = dt_raw.strip().replace("Z", "")

    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            dt = datetime.strptime(dt_clean, fmt)
            return dt.replace(tzinfo=ET)
        except ValueError:
            continue
    return None


def fetch_economic_calendar() -> dict:
    ics_text = safe_get_ics(BLS_ICS_URL)
    now_et = datetime.now(ET)

    target_map = {
        "Consumer Price Index": "US CPI",
        "Producer Price Index": "US PPI",
        "Employment Situation": "US Employment Situation (NFP / Unemployment)",
    }

    events = []
    blocks = ics_text.split("BEGIN:VEVENT")

    for block in blocks:
        if "SUMMARY:" not in block or "DTSTART" not in block:
            continue

        summary_match = re.search(r"SUMMARY:(.+)", block)
        dtstart_match = re.search(r"DTSTART(?:;[^:]+)?:([0-9T]+Z?)", block)

        if not summary_match or not dtstart_match:
            continue

        summary = summary_match.group(1).strip()
        dt_raw = dtstart_match.group(1).strip()

        matched_label = None
        for title, label in target_map.items():
            if summary.startswith(title):
                matched_label = label
                break

        if not matched_label:
            continue

        dt = parse_ics_datetime(dt_raw)
        if not dt or dt < now_et:
            continue

        reference_period = "upcoming release"
        m = re.search(r"for (.+)$", summary)
        if m:
            reference_period = m.group(1).strip()

        events.append(
            {
                "label": matched_label,
                "reference_period": reference_period,
                "release_et": dt,
            }
        )

    if not events:
        raise ValueError("No upcoming CPI/PPI/Employment Situation releases found in BLS ICS feed")

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


def build_econ_calendar_html(calendar: dict) -> str:
    lines = []
    for event in calendar["events"]:
        lines.append(f"• {format_event_line(event)}")
    return "<br>".join(lines)


def build_market_mover_text(calendar: dict) -> str:
    mover = calendar["market_mover"]
    return f"Next key scheduled mover: {format_event_line(mover)}"


def build_prompt(fx_rates: dict, indicators: dict, headlines: dict, calendar: dict) -> str:
    headline_text = flatten_headlines(headlines)
    generated_at = datetime.now(CET).strftime("%Y-%m-%d %H:%M CET")
    econ_lines = "\n".join([f"- {format_event_line(e)}" for e in calendar["events"]])

    return f"""
You are a senior FX analyst at Privalgo, an EMI specialising in FX risk management 
and international payment rails for corporate clients. Write a concise morning briefing 
for colleagues in trading, relationship management, and client-facing roles.

Use ONLY the data provided below. Do not invent numbers, events, causal explanations, 
or market narratives. If the data does not explain a move, describe the observation 
only — do not speculate on the cause.

DATA TIMESTAMP: {generated_at}

FX SNAPSHOT:
EUR/USD: {fx_rates['EURUSD']}
EUR/GBP: {fx_rates['EURGBP']}
EUR/CHF: {fx_rates['EURCHF']}

MARKET INDICATORS:
S&P 500: {indicators['SP500']['value']} (date: {indicators['SP500']['date']})
Gold spot: {indicators['GOLD']}
VIX: {indicators['VIX']['value']} (date: {indicators['VIX']['date']})
US 10Y yield: {indicators['US10Y']['value']} (date: {indicators['US10Y']['date']})
Brent spot: {indicators['BRENT']['value']} (date: {indicators['BRENT']['date']})

ECONOMIC CALENDAR:
{econ_lines}

MARKET MOVER:
{build_market_mover_text(calendar)}

HEADLINES:
{headline_text}

FIELD DEFINITIONS:
- key_market: The single most important macro or market development. Lead with a number.
- fx: EUR/USD, EUR/GBP, EUR/CHF — direction, magnitude, likely driver. Flag notable levels.
- central_banks: Relevant central bank signals or rate expectations from the data/headlines. 
  If nothing relevant: "No major central bank signals today."
- macro_watch: Today's scheduled releases — what is expected and why it matters. 
  If calendar is empty: "No major scheduled releases today — markets may trade on technicals."
- impact: Name a specific corporate use-case affected by today's conditions (hedging timing, 
  invoice currency exposure, cash repatriation, etc.). Not a generic observation.
- client_talking_point: One practical, grounded observation a relationship manager could 
  use in a client call today. Commercially useful, not pushy.

RULES:
- Return valid JSON only. No preamble, no markdown. First character must be {{.
- Both "en" and "nl" fields are required.
- Each field: maximum 2 sentences.
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

    response = client.chat.completions.create(
        model="gpt-4o",  # gpt-5 does not exist; use gpt-4o (or gpt-4o-mini for lower cost)
        messages=[
            {
                "role": "system",
                "content": "You write precise market briefings and return strict JSON only."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
    )

    content = strip_code_fences(response.choices[0].message.content)
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

        "{{SP500}}": f"{indicators['SP500']['value']}",
        "{{GOLD}}": f"{indicators['GOLD']}",
        "{{VIX}}": f"{indicators['VIX']['value']}",
        "{{US10Y}}": f"{indicators['US10Y']['value']}",
        "{{BRENT}}": f"{indicators['BRENT']['value']}",

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
    msg["To"] = ", ".join(RECIPIENTS)
    msg["Reply-To"] = "whendriks@privalgo.co.uk"

    text_body = "This email contains an HTML market update. Please view it in an HTML-compatible email client."
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls(context=context)
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(MAIL_FROM, RECIPIENTS, msg.as_string())


def main():
    fx_rates = fetch_alpha_vantage_fx()
    indicators = fetch_market_indicators()
    headlines = fetch_headlines()
    calendar = fetch_economic_calendar()
    market_update = generate_market_update(fx_rates, indicators, headlines, calendar)
    template = load_template()
    html_body = fill_template(template, fx_rates, indicators, market_update, calendar)
    send_email(html_body)
    print("Market update email sent successfully.")


if __name__ == "__main__":
    main()

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
from bs4 import BeautifulSoup
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

BLS_CPI_SCHEDULE = "https://www.bls.gov/schedule/news_release/cpi.htm"
BLS_PPI_SCHEDULE = "https://www.bls.gov/schedule/news_release/ppi.htm"
BLS_EMPSIT_SCHEDULE = "https://www.bls.gov/schedule/news_release/empsit.htm"

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


def safe_get_text(url: str) -> str:
    response = requests.get(url, timeout=25)
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


def fetch_alpha_vantage_gold_spot() -> float:
    data = safe_get_json(
        "https://www.alphavantage.co/query",
        params={
            "function": "GOLD_SILVER_SPOT",
            "symbol": "GOLD",
            "apikey": ALPHA_VANTAGE_API_KEY,
        },
    )

    # Alpha Vantage commodity payloads are not as uniform as FX/global quote,
    # so parse defensively.
    possible_paths = [
        ("data",),
        ("spot_price",),
        ("price",),
        ("value",),
        ("Gold Spot Price",),
        ("Realtime Gold Spot Price",),
    ]

    for path in possible_paths:
        cur = data
        found = True
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                found = False
                break
        if found:
            try:
                if isinstance(cur, list) and cur:
                    # if list of dicts
                    first = cur[0]
                    if isinstance(first, dict):
                        for k in ["value", "price", "spot_price", "close"]:
                            if k in first:
                                return round(float(first[k]), 2)
                return round(float(cur), 2)
            except Exception:
                pass

    # broader fallback search
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, (int, float, str)):
                key_lower = str(k).lower()
                if "gold" in key_lower or "spot" in key_lower or "price" in key_lower or "value" in key_lower:
                    try:
                        return round(float(v), 2)
                    except Exception:
                        continue

    raise ValueError(f"Missing gold spot price: {data}")


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
    # Actual S&P 500 daily close, not SPY proxy
    sp500 = fetch_fred_latest("SP500")
    gold = fetch_alpha_vantage_gold_spot()
    time.sleep(1.1)

    # FRED series
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


def parse_bls_datetime(date_str: str, time_str: str) -> datetime | None:
    date_str = date_str.strip().replace("Sept.", "Sep.").replace("Sept", "Sep")
    time_str = time_str.strip()

    date_formats = [
        "%b. %d, %Y",
        "%B %d, %Y",
        "%A, %B %d, %Y",
        "%a, %b. %d, %Y",
    ]
    time_formats = [
        "%I:%M %p",
        "%I:%M%p",
    ]

    for df in date_formats:
        for tf in time_formats:
            try:
                dt = datetime.strptime(f"{date_str} {time_str}", f"{df} {tf}")
                return dt.replace(tzinfo=ET)
            except ValueError:
                continue
    return None


def fetch_next_bls_release(schedule_url: str, label: str) -> dict:
    html = safe_get_text(schedule_url)
    soup = BeautifulSoup(html, "html.parser")
    now_et = datetime.now(ET)

    table = soup.find("table")
    if not table:
        raise ValueError(f"No schedule table found for {label}")

    rows = table.find_all("tr")
    candidates = []

    for row in rows:
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) < 3:
            continue

        ref_month = cells[0]
        release_date = cells[1]
        release_time = cells[2]

        if "release date" in release_date.lower():
            continue

        dt = parse_bls_datetime(release_date, release_time)
        if dt and dt >= now_et:
            candidates.append(
                {
                    "label": label,
                    "reference_period": ref_month,
                    "release_et": dt,
                }
            )

    if not candidates:
        raise ValueError(f"No upcoming BLS release found for {label}")

    return sorted(candidates, key=lambda x: x["release_et"])[0]


def fetch_economic_calendar() -> dict:
    cpi = fetch_next_bls_release(BLS_CPI_SCHEDULE, "US CPI")
    ppi = fetch_next_bls_release(BLS_PPI_SCHEDULE, "US PPI")
    jobs = fetch_next_bls_release(BLS_EMPSIT_SCHEDULE, "US Employment Situation (NFP / Unemployment)")

    events = [cpi, ppi, jobs]
    events_sorted = sorted(events, key=lambda x: x["release_et"])
    market_mover = events_sorted[0]

    return {
        "events": events_sorted,
        "market_mover": market_mover,
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
You are preparing an internal Bloomberg-style morning briefing for Privalgo.

Use ONLY the data below. Do not invent numbers, events, or explanations.
Be concise, commercially useful, professional, and clear.

DATA TIMESTAMP:
{generated_at}

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

Return valid JSON with exactly this structure:
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

Rules:
- English and Dutch must both sound natural.
- Each field max 2 sentences.
- Refer to the actual indicators where relevant.
- The macro_watch should mention the scheduled releases if relevant.
- The client_talking_point must be practical and commercially useful, but not pushy.
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
        model="gpt-5",
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

import os
import smtplib
import ssl
import json
from datetime import datetime, timezone
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

SMTP_SERVER = os.environ["SMTP_SERVER"]
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ["SMTP_USERNAME"]
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]
MAIL_FROM = os.environ["MAIL_FROM"]

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
ALPHA_VANTAGE_API_KEY = os.environ["ALPHA_VANTAGE_API_KEY"]
FRED_API_KEY = os.environ["FRED_API_KEY"]

client = OpenAI(api_key=OPENAI_API_KEY)


def safe_get_json(url: str, params: dict | None = None) -> dict:
    response = requests.get(url, params=params, timeout=25)
    response.raise_for_status()
    return response.json()

import time

def fetch_alpha_vantage_fx():

    pairs = {
        "EURUSD": ("EUR", "USD"),
        "EURGBP": ("EUR", "GBP"),
        "EURCAD": ("EUR", "CAD"),
        "EURCHF": ("EUR", "CHF"),
        "EURNOK": ("EUR", "NOK"),
        "EURSEK": ("EUR", "SEK"),
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

def fetch_alpha_vantage_global_quote(symbol: str) -> dict:
    data = safe_get_json(
        "https://www.alphavantage.co/query",
        params={
            "function": "GLOBAL_QUOTE",
            "symbol": symbol,
            "apikey": ALPHA_VANTAGE_API_KEY,
        },
    )

    block = data.get("Global Quote", {})
    price = block.get("05. price")
    change_percent = block.get("10. change percent")

    if not price:
        raise ValueError(f"Missing market quote for {symbol}: {data}")

    return {
        "price": round(float(price), 2),
        "change_percent": change_percent or "n/a",
    }


def fetch_fred_latest(series_id: str) -> dict:
    data = safe_get_json(
        "https://api.stlouisfed.org/fred/series/observations",
        params={
            "series_id": series_id,
            "api_key": FRED_API_KEY,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 2,
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


def fetch_market_indicators():
    sp500 = fetch_alpha_vantage_global_quote("SPY")
    time.sleep(1.1)

    gold = fetch_alpha_vantage_global_quote("GLD")
    time.sleep(1.1)

    # FRED series:
    # VIXCLS = CBOE VIX close
    # DGS10  = 10-Year Treasury Constant Maturity Rate
    # DCOILBRENTEU = Brent spot price FOB
    vix = fetch_fred_latest("VIXCLS")
    us10y = fetch_fred_latest("DGS10")
    brent = fetch_fred_latest("DCOILBRENTEU")

    return {
        "SP500_PROXY": sp500,
        "GOLD_PROXY": gold,
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


def build_prompt(fx_rates: dict, indicators: dict, headlines: dict) -> str:
    headline_text = flatten_headlines(headlines)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return f"""
You are preparing an internal Bloomberg-style morning briefing for Privalgo.

Use ONLY the data below. Do not invent numbers, events, or explanations.
Be concise, commercial, professional, and useful for colleagues speaking to internationally active clients.

DATA TIMESTAMP:
{generated_at}

FX SNAPSHOT:
EUR/USD: {fx_rates['EURUSD']}
EUR/GBP: {fx_rates['EURGBP']}
EUR/CAD: {fx_rates['EURCAD']}
EUR/CHF: {fx_rates['EURCHF']}
EUR/NOK: {fx_rates['EURNOK']}
EUR/SEK: {fx_rates['EURSEK']}

MARKET INDICATORS:
S&P 500 proxy (SPY): {indicators['SP500_PROXY']['price']} ({indicators['SP500_PROXY']['change_percent']})
Gold proxy (GLD): {indicators['GOLD_PROXY']['price']} ({indicators['GOLD_PROXY']['change_percent']})
VIX: {indicators['VIX']['value']} (date: {indicators['VIX']['date']})
US 10Y yield: {indicators['US10Y']['value']} (date: {indicators['US10Y']['date']})
Brent spot: {indicators['BRENT']['value']} (date: {indicators['BRENT']['date']})

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
- Do not mention missing data unless absolutely necessary.
- The client_talking_point must be practical and commercially useful, but not pushy.
"""


def generate_market_update(fx_rates: dict, indicators: dict, headlines: dict) -> dict:
    prompt = build_prompt(fx_rates, indicators, headlines)

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

    content = response.choices[0].message.content
    return json.loads(content)


def load_template(path: str = "email_template.html") -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def fill_template(template: str, fx_rates: dict, indicators: dict, market_update: dict) -> str:
    today = datetime.now().strftime("%d %B %Y")

    replacements = {
        "{{DATE}}": today,

        "{{EURUSD}}": fx_rates["EURUSD"],
        "{{EURGBP}}": fx_rates["EURGBP"],
        "{{EURCAD}}": fx_rates["EURCAD"],
        "{{EURCHF}}": fx_rates["EURCHF"],
        "{{EURNOK}}": fx_rates["EURNOK"],
        "{{EURSEK}}": fx_rates["EURSEK"],

        "{{SP500}}": f"{indicators['SP500_PROXY']['price']} ({indicators['SP500_PROXY']['change_percent']})",
        "{{GOLD}}": f"{indicators['GOLD_PROXY']['price']} ({indicators['GOLD_PROXY']['change_percent']})",
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
    }

    html = template
    for key, value in replacements.items():
        html = html.replace(key, str(value))

    return html


def send_email(html_body: str) -> None:
    subject = f"Privalgo Daily Market Update – {datetime.now().strftime('%d %B %Y')}"

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
    market_update = generate_market_update(fx_rates, indicators, headlines)
    template = load_template()
    html_body = fill_template(template, fx_rates, indicators, market_update)
    send_email(html_body)
    print("Market update email sent successfully.")


if __name__ == "__main__":
    main()

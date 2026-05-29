"""
Gold Signal Bot - XAU/USD Telegram Bot
بۆتی سیگناڵی زێڕ بۆ تەلەگرام
"""

import os
import asyncio
import logging
import json
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from telegram import Bot
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─── Config ───────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
SIGNAL_INTERVAL  = int(os.getenv("SIGNAL_INTERVAL_MINUTES", "60"))

PIP   = 0.10
SL_P  = 20
TP1_P = 30
TP2_P = 50

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

price_history: list = []

# ─── Indicators ───────────────────────────────────────────

def calc_ma(prices, period):
    if len(prices) < period:
        return None
    return round(sum(prices[-period:]) / period, 2)

def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        d = prices[i] - prices[i - 1]
        if d > 0: gains += d
        else: losses -= d
    rs = gains / (losses or 1e-9)
    return round(100 - 100 / (1 + rs), 1)

def calc_fib(prices):
    s = prices[-50:] if len(prices) >= 50 else prices
    hi, lo = max(s), min(s)
    d = hi - lo
    return {
        "high": round(hi,2), "low": round(lo,2),
        "fib786": round(hi - d*0.786, 2),
        "fib618": round(hi - d*0.618, 2),
        "fib500": round(hi - d*0.500, 2),
        "fib382": round(hi - d*0.382, 2),
        "fib236": round(hi - d*0.236, 2),
    }

def calc_levels(signal, entry):
    if signal == "HOLD": return {}
    d = 1 if signal == "BUY" else -1
    return {
        "sl":  round(entry - d * SL_P  * PIP, 2),
        "tp1": round(entry + d * TP1_P * PIP, 2),
        "tp2": round(entry + d * TP2_P * PIP, 2),
    }

# ─── API helpers ──────────────────────────────────────────

HEADERS = lambda: {
    "x-api-key": ANTHROPIC_KEY,
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
}

async def claude(body: dict, timeout=40) -> list:
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post("https://api.anthropic.com/v1/messages", headers=HEADERS(), json=body)
        return r.json().get("content", [])

def first_text(blocks: list) -> str:
    for b in blocks:
        if b.get("type") == "text":
            return b["text"].replace("```json","").replace("```","").strip()
    return "{}"

# ─── Live Price ───────────────────────────────────────────

async def fetch_live_price() -> Optional[float]:
    blocks = await claude({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 200,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "system": 'Search for current XAU/USD gold spot price. Return ONLY JSON: {"price": NUMBER}. No other text.',
        "messages": [{"role": "user", "content": "Current XAU/USD gold spot price right now?"}],
    })
    text = first_text(blocks)
    try:
        p = float(json.loads(text).get("price", 0))
        if 1000 < p < 5000:
            log.info(f"Live price: ${p}")
            return p
    except Exception:
        m = re.search(r"(\d{3,4}(?:\.\d+)?)", text)
        if m:
            v = float(m.group(1))
            if 1000 < v < 5000:
                return v
    log.error("Price parse failed")
    return None

# ─── Fundamental News ─────────────────────────────────────

async def fetch_news() -> list:
    blocks = await claude({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 600,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "system": (
            'Search latest gold market news today. '
            'Return ONLY JSON array of 3: [{"title":"...","impact":"BULLISH"|"BEARISH"|"NEUTRAL","summary":"one sentence Kurdish Sorani"}]'
        ),
        "messages": [{"role": "user", "content": "Latest gold XAU news today: Fed, USD, geopolitics."}],
    })
    try:
        arr = json.loads(first_text(blocks))
        if isinstance(arr, list):
            return arr[:3]
    except Exception:
        pass
    return []

# ─── Analysis ─────────────────────────────────────────────

async def analyze(price: float, prices: list, news: list) -> Optional[dict]:
    fib  = calc_fib(prices) if len(prices) >= 5 else {"high": price+20,"low": price-20,"fib618": price-5,"fib382": price+5,"fib500": price,"fib786": price-10,"fib236": price+10}
    rsi  = calc_rsi(prices)
    ma20 = calc_ma(prices, 20)
    ma50 = calc_ma(prices, 50)

    fib_pos = ("Above 0.618 (strong bullish)" if price > fib["fib618"]
               else "Between 0.5-0.618 (neutral/bullish)" if price > fib["fib500"]
               else "Between 0.382-0.5 (neutral/bearish)" if price > fib["fib382"]
               else "Below 0.382 (bearish)")

    ma_sig = ("MA20>MA50 BULLISH" if ma20 and ma50 and ma20 > ma50
              else "MA20<MA50 BEARISH" if ma20 and ma50
              else "N/A")

    news_str = "; ".join(f"{n['title']}: {n['impact']}" for n in news) if news else "N/A"

    prompt = (
        f"XAU/USD price: ${price}\n"
        f"RSI(14): {rsi or 'N/A'}\n"
        f"MA20: {ma20 or 'N/A'}, MA50: {ma50 or 'N/A'}, Signal: {ma_sig}\n"
        f"Fib(high={fib['high']},low={fib['low']}): "
        f"0.786={fib['fib786']} 0.618={fib['fib618']} 0.500={fib['fib500']} "
        f"0.382={fib['fib382']} 0.236={fib['fib236']}\n"
        f"Price position: {fib_pos}\n"
        f"Recent 10 prices: {prices[-10:]}\n"
        f"News: {news_str}\n"
        f"SL=20pip($2) TP1=30pip($3) TP2=50pip($5)"
    )

    blocks = await claude({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 800,
        "system": (
            'Expert XAU/USD analyst. Return ONLY JSON:\n'
            '{"signal":"BUY"|"SELL"|"HOLD","confidence":0-100,'
            '"fibZone":"...","maSignal":"BULLISH_CROSS"|"BEARISH_CROSS"|"NEUTRAL",'
            '"fundamentalBias":"BULLISH"|"BEARISH"|"NEUTRAL","rsiLevel":number,'
            '"trend":"BULLISH"|"BEARISH"|"NEUTRAL","support":number,"resistance":number,'
            '"reasoning":"2-3 sentences Kurdish Sorani about fib+MA+fundamental"}'
        ),
        "messages": [{"role": "user", "content": prompt}],
    })
    try:
        return json.loads(first_text(blocks))
    except Exception as e:
        log.error(f"Analysis parse error: {e}")
        return None

# ─── Message Formatter ────────────────────────────────────

def esc(s: str) -> str:
    """Escape MarkdownV2 special chars."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        s = s.replace(ch, f"\\{ch}")
    return s

def format_message(price: float, a: dict, news: list) -> str:
    sig  = a.get("signal", "HOLD")
    conf = a.get("confidence", 0)
    lvl  = calc_levels(sig, price)
    now  = datetime.now(timezone.utc).strftime("%Y\\-%m\\-%d %H:%M UTC")

    sig_map  = {"BUY": ("🟢", "کڕین ▲"), "SELL": ("🔴", "فرۆشتن ▼"), "HOLD": ("🟡", "چاوەڕێ ●")}
    ma_map   = {"BULLISH_CROSS": "⬆ بەرز", "BEARISH_CROSS": "⬇ نزم", "NEUTRAL": "➡ ناڕاست"}
    fund_map = {"BULLISH": "🟢 بەرز", "BEARISH": "🔴 نزم", "NEUTRAL": "🟡 ناڕاست"}
    trend_map= {"BULLISH": "📈 بەرز", "BEARISH": "📉 نزم", "NEUTRAL": "➡ ناڕاست"}

    se, sk  = sig_map.get(sig, ("🟡","چاوەڕێ ●"))
    bar     = "█" * round(conf/10) + "░" * (10 - round(conf/10))

    lines = [
        f"{se} *XAU/USD · بۆتی زێڕ*",
        f"🕐 `{now}`",
        "",
        "━━━━━━━━━━━━━━━━━━━",
        f"📊 *سیگناڵ:*  `{sk}`",
        f"💰 *نرخ:*  `${price:.2f}`",
        f"🎯 *ئارامی:*  `{conf}%`  `{bar}`",
        "",
        "━━━━━━━━━━━━━━━━━━━",
        "📐 *ئاستەکانی مامەڵە*",
    ]

    if sig != "HOLD" and lvl:
        lines += [
            f"🔵 چوونەژوورەوە:  `${price:.2f}`",
            f"🔴 ستۆپ لوز:      `${lvl['sl']:.2f}`  \\(20 پیپ\\)",
            f"🟡 TP1 یەکەم:     `${lvl['tp1']:.2f}`  \\(30 پیپ\\)",
            f"🟢 TP2 دووەم:     `${lvl['tp2']:.2f}`  \\(50 پیپ\\)",
            f"⚖️ R\\:R → TP1 `1:1\\.5`  \\|  TP2 `1:2\\.5`",
        ]
    else:
        lines.append("_هیچ پۆزیشنێک نەکرابێتەوە_")

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━",
        "🔬 *شیکاری تەکنیکی*",
        f"📐 Fib Zone:   `{esc(str(a.get('fibZone','—')))}`",
        f"📈 MA Signal:  `{ma_map.get(a.get('maSignal','NEUTRAL'),'—')}`",
        f"💹 RSI\\(14\\):  `{a.get('rsiLevel','—')}`",
        f"🧭 ئاراستە:   `{trend_map.get(a.get('trend','NEUTRAL'),'—')}`",
        f"🔽 پشتیوانی:  `${a.get('support',0):.1f}`",
        f"🔼 بەربەست:   `${a.get('resistance',0):.1f}`",
        "",
        "━━━━━━━━━━━━━━━━━━━",
        f"🌍 *فەندەمێنتاڵ:*  {fund_map.get(a.get('fundamentalBias','NEUTRAL'),'—')}",
    ]

    if news:
        lines.append("")
        for n in news[:3]:
            imp = n.get("impact","NEUTRAL")
            em  = "🟢" if imp=="BULLISH" else "🔴" if imp=="BEARISH" else "🟡"
            lines.append(f"{em} {esc(n.get('summary',''))}")

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━",
        "🧠 *شیکاری AI*",
        f"_{esc(a.get('reasoning',''))}_",
        "",
        f"⚠️ _ئەمە زانیاریی تەکنیکیە، مەشوەرەتی داراییی نییە_",
        f"🤖 _بۆتی زێڕ · هەر {SIGNAL_INTERVAL} خولەک_",
    ]
    return "\n".join(lines)

# ─── Bot & Scheduler ──────────────────────────────────────

bot = Bot(token=TELEGRAM_TOKEN)

async def send(text: str):
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode=ParseMode.MARKDOWN_V2)
        log.info("✅ Message sent")
    except Exception as e:
        log.warning(f"MarkdownV2 failed ({e}), retrying plain…")
        plain = re.sub(r"[*_`\[\]()~>#+=|{}.!\\]", "", text)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=plain)
        log.info("✅ Plain text sent")

async def run_job():
    log.info("⏱ Running signal job…")
    price = await fetch_live_price()
    if not price:
        log.warning("No price — skip")
        return

    price_history.append(price)
    if len(price_history) > 200:
        price_history.pop(0)

    news     = await fetch_news()
    analysis = await analyze(price, price_history, news)
    if not analysis:
        return

    sig = analysis.get("signal","HOLD")
    log.info(f"→ {sig} @ ${price} conf={analysis.get('confidence')}%")

    # بۆ دانێرانی هەموو سیگناڵ (تێبینی: HOLD ناردن دابخرێت ئەگەر نارامت دەکات)
    await send(format_message(price, analysis, news))

async def main():
    log.info(f"🤖 Gold Bot start — every {SIGNAL_INTERVAL} min")
    startup = (
        "🏅 *بۆتی سیگناڵی زێڕ چالاک بوو\\!*\n\n"
        f"⏱ هەر `{SIGNAL_INTERVAL}` خولەک سیگناڵ دەنێرم\\.\n"
        "📐 Fibonacci \\+ Moving Average \\+ Fundamental\n"
        "🔴 SL: 20 پیپ  \\|  🟡 TP1: 30 پیپ  \\|  🟢 TP2: 50 پیپ\n\n"
        "⚠️ _ئەمە بۆ مەبەستی زانیاریە_"
    )
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=startup, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        log.warning(f"Startup msg failed: {e}")

    await run_job()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(run_job, "interval", minutes=SIGNAL_INTERVAL)
    scheduler.start()

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())

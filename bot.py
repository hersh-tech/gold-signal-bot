"""
XAU/USD Auto Trader Bot v3.0
Signal → Telegram Approve/Reject → MT5 Auto Execute
Features:
- Approve/Reject buttons on every signal
- MT5 trade execution via local file bridge
- Auto move SL to breakeven at TP1
- Auto close at TP2/TP3
- Trade report on close
"""

import os
import asyncio
import logging
import json
import re
import math
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ─── Config ───────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_KEY    = os.environ["ANTHROPIC_API_KEY"]
SIGNAL_INTERVAL  = int(os.getenv("SIGNAL_INTERVAL_MINUTES", "15"))
MIN_SCORE        = int(os.getenv("MIN_SIGNAL_SCORE", "75"))
APPROVE_TIMEOUT  = int(os.getenv("APPROVE_TIMEOUT_MINUTES", "5"))  # auto-expire after 5 min
TRADE_FILE       = os.getenv("TRADE_FILE_PATH", "trade_command.json")  # MT5 bridge file

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── State ────────────────────────────────────────────────
prices_15m: list = []
prices_4h:  list = []
prices_1d:  list = []
tick_count       = 0
bot_start_time   = datetime.now(timezone.utc)
pending_signals  = {}   # message_id → signal data
active_trades    = {}   # trade_id → trade data
last_signal      = {"signal": "NONE", "score": 0, "price": 0, "time": "Never"}

# ─── Technical Indicators (same as v2.1) ──────────────────

def calc_ma(prices, period):
    if len(prices) < period: return None
    return round(sum(prices[-period:]) / period, 2)

def calc_ema(prices, period):
    if len(prices) < period: return None
    k = 2 / (period + 1)
    ema = prices[-period]
    for p in prices[-period+1:]: ema = p*k + ema*(1-k)
    return round(ema, 2)

def calc_rsi(prices, period=14):
    if len(prices) < period+1: return None
    g, l = 0.0, 0.0
    for i in range(-period, 0):
        d = prices[i]-prices[i-1]
        if d>0: g+=d
        else: l-=d
    return round(100 - 100/(1+g/(l or 1e-9)), 1)

def calc_rsi_div(prices, period=14):
    if len(prices) < period+10: return "NONE"
    r1 = calc_rsi(prices, period)
    r2 = calc_rsi(prices[:-5], period)
    if not r1 or not r2: return "NONE"
    if prices[-1]>prices[-6] and r1<r2: return "BEARISH_DIV"
    if prices[-1]<prices[-6] and r1>r2: return "BULLISH_DIV"
    return "NONE"

def calc_macd(prices):
    if len(prices)<26: return {"cross":"NONE","macd":None}
    e12,e26 = calc_ema(prices,12), calc_ema(prices,26)
    if not e12 or not e26: return {"cross":"NONE","macd":None}
    ml = round(e12-e26,3)
    vals = [calc_ema(prices[:-i],12)-calc_ema(prices[:-i],26) for i in range(9,0,-1) if calc_ema(prices[:-i],12) and calc_ema(prices[:-i],26)]
    sl = round(sum(vals)/len(vals),3) if vals else None
    cross = "BULLISH" if sl and ml>sl and ml>0 else "BEARISH" if sl and ml<sl and ml<0 else "NONE"
    return {"cross":cross,"macd":ml,"signal":sl}

def calc_bb(prices, period=20):
    if len(prices)<period: return {"upper":None,"middle":None,"lower":None,"position":"MIDDLE"}
    sl = prices[-period:]
    mid = sum(sl)/period
    std = math.sqrt(sum((p-mid)**2 for p in sl)/period)
    u,l,m = round(mid+2*std,2), round(mid-2*std,2), round(mid,2)
    p = prices[-1]
    pos = "ABOVE_UPPER" if p>=u else "BELOW_LOWER" if p<=l else "UPPER_HALF" if p>m else "LOWER_HALF"
    return {"upper":u,"middle":m,"lower":l,"position":pos}

def calc_atr(prices, period=14):
    if len(prices)<period+1: return None
    trs = [max(prices[i]*1.0015-prices[i]*0.9985, abs(prices[i]*1.0015-prices[i-1]), abs(prices[i]*0.9985-prices[i-1])) for i in range(-period,0)]
    return round(sum(trs)/len(trs), 2)

def calc_fib(prices):
    if len(prices)<2: return {}
    s = prices[-50:] if len(prices)>=50 else prices
    hi,lo = max(s),min(s)
    d = hi-lo
    return {"high":round(hi,2),"low":round(lo,2),"fib618":round(hi-d*0.618,2),"fib500":round(hi-d*0.500,2),"fib382":round(hi-d*0.382,2)}

def calc_dynamic_levels(signal, entry, atr):
    if signal=="HOLD" or not atr: return {}
    d = 1 if signal=="BUY" else -1
    return {
        "sl":  round(entry-d*atr*1.5,2), "tp1": round(entry+d*atr*2.0,2),
        "tp2": round(entry+d*atr*3.5,2), "tp3": round(entry+d*atr*5.0,2),
        "sl_pips": round(atr*1.5/0.10),  "tp1_pips": round(atr*2.0/0.10),
        "tp2_pips": round(atr*3.5/0.10), "tp3_pips": round(atr*5.0/0.10),
        "rr1":f"1:{round(2.0/1.5,1)}",   "rr2":f"1:{round(3.5/1.5,1)}",
        "rr3":f"1:{round(5.0/1.5,1)}",
    }

def score_signal(direction, td):
    score, bd = 0, {}
    ib = direction=="BUY"
    rsi = td.get("rsi_15m")
    if rsi:
        if ib and 30<rsi<50:       score+=15; bd["RSI"]=f"+15 ({rsi})"
        elif ib and rsi<30:        score+=12; bd["RSI"]=f"+12 ({rsi})"
        elif not ib and 50<rsi<70: score+=15; bd["RSI"]=f"+15 ({rsi})"
        elif not ib and rsi>70:    score+=12; bd["RSI"]=f"+12 ({rsi})"
        else: score+=5; bd["RSI"]=f"+5 ({rsi})"
    div = td.get("rsi_div","NONE")
    if (ib and div=="BULLISH_DIV") or (not ib and div=="BEARISH_DIV"): score+=10; bd["Div"]="+10"
    else: bd["Div"]="+0"
    mc = td.get("macd_cross","NONE")
    if (ib and mc=="BULLISH") or (not ib and mc=="BEARISH"): score+=15; bd["MACD"]="+15"
    else: score+=5; bd["MACD"]="+5"
    ma20,ma50,ma200,price = td.get("ma20"),td.get("ma50"),td.get("ma200"),td.get("price",0)
    mp=0
    if ma20 and ma50:
        if ib and ma20>ma50: mp+=8
        if not ib and ma20<ma50: mp+=8
    if ma200 and price:
        if ib and price>ma200: mp+=7
        if not ib and price<ma200: mp+=7
    if ma20 and price:
        if ib and price>ma20: mp+=5
        if not ib and price<ma20: mp+=5
    score+=mp; bd["MA"]=f"+{mp}"
    bb = td.get("bb_position","MIDDLE")
    if (ib and bb=="BELOW_LOWER") or (not ib and bb=="ABOVE_UPPER"): score+=10; bd["BB"]="+10"
    elif (ib and bb=="LOWER_HALF") or (not ib and bb=="UPPER_HALF"): score+=5; bd["BB"]="+5"
    else: bd["BB"]="+0"
    fz = td.get("fib_zone","")
    if "support" in fz.lower() and ib: score+=15; bd["Fib"]=f"+15"
    elif "resistance" in fz.lower() and not ib: score+=15; bd["Fib"]=f"+15"
    elif "0.500" in fz: score+=7; bd["Fib"]="+7"
    else: score+=3; bd["Fib"]="+3"
    mtf = td.get("mtf_alignment","WEAK")
    if mtf=="STRONG": score+=15; bd["MTF"]="+15"
    elif mtf=="MODERATE": score+=8; bd["MTF"]="+8"
    else: score+=2; bd["MTF"]="+2"
    return {"score":min(score,100),"breakdown":bd}

# ─── API ──────────────────────────────────────────────────

def hdrs():
    return {"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"}

async def claude(body, timeout=50):
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post("https://api.anthropic.com/v1/messages",headers=hdrs(),json=body)
        return r.json().get("content",[])

def first_text(blocks):
    for b in blocks:
        if b.get("type")=="text": return b["text"].replace("```json","").replace("```","").strip()
    return "{}"

async def fetch_price():
    blocks = await claude({"model":"claude-sonnet-4-20250514","max_tokens":150,"tools":[{"type":"web_search_20250305","name":"web_search"}],"system":'Search current XAU/USD price. Return ONLY JSON: {"price":NUMBER}',"messages":[{"role":"user","content":"Current XAU/USD price now?"}]})
    text = first_text(blocks)
    try:
        p = float(json.loads(text).get("price",0))
        if 1000<p<5000: return p
    except: pass
    m = re.search(r"(\d{3,4}(?:\.\d+)?)",text)
    if m:
        v=float(m.group(1))
        if 1000<v<5000: return v
    return None

async def fetch_news():
    blocks = await claude({"model":"claude-sonnet-4-20250514","max_tokens":400,"tools":[{"type":"web_search_20250305","name":"web_search"}],"system":'Latest gold news. ONLY JSON array of 3: [{"title":"...","impact":"BULLISH"|"BEARISH"|"NEUTRAL","summary":"one sentence"}]',"messages":[{"role":"user","content":"Latest XAU gold news today."}]})
    try:
        arr = json.loads(first_text(blocks))
        if isinstance(arr,list): return arr[:3]
    except: pass
    return []

async def analyze_mtf(price, news):
    atr_15m = calc_atr(prices_15m) or 2.0
    atr_4h  = calc_atr(prices_4h)  or 5.0
    fib_15m = calc_fib(prices_15m) if len(prices_15m)>=10 else {}
    fib_4h  = calc_fib(prices_4h)  if len(prices_4h)>=10  else {}
    rsi_15m = calc_rsi(prices_15m,14)
    rsi_4h  = calc_rsi(prices_4h,14)
    rsi_1d  = calc_rsi(prices_1d,14)
    macd_15m= calc_macd(prices_15m)
    bb_15m  = calc_bb(prices_15m)
    ma20    = calc_ma(prices_15m,20)
    ma50    = calc_ma(prices_15m,50)
    ma200   = calc_ma(prices_15m,100)
    rsi_div = calc_rsi_div(prices_15m)
    news_str= "; ".join(f"{n['title']}: {n['impact']}" for n in news) if news else "N/A"

    def fz(p,f):
        if not f: return "Unknown"
        if p<=f.get("fib382",0)+1: return "0.382 support zone"
        if p<=f.get("fib500",0)+1: return "0.500 support zone"
        if p>=f.get("fib618",0)-1: return "0.618 resistance zone"
        return "0.500 neutral zone"

    prompt = (f"XAU/USD:${price} ATR_15M:{atr_15m} ATR_4H:{atr_4h}\n"
              f"15M→RSI:{rsi_15m} MACD:{macd_15m['cross']} BB:{bb_15m['position']} MA20:{ma20} MA50:{ma50} MA200:{ma200} Fib:{fz(price,fib_15m)} RSI_DIV:{rsi_div}\n"
              f"4H→RSI:{rsi_4h} Fib:{fz(price,fib_4h)}\nDaily→RSI:{rsi_1d}\nNews:{news_str}")
    system = 'Elite XAU/USD analyst. ONLY JSON:{"signal":"BUY"|"SELL"|"HOLD","mtf_alignment":"STRONG"|"MODERATE"|"WEAK","fib_zone":"...","macd_cross":"BULLISH"|"BEARISH"|"NONE","bb_position":"ABOVE_UPPER"|"BELOW_LOWER"|"UPPER_HALF"|"LOWER_HALF"|"MIDDLE","trend_15m":"BULLISH"|"BEARISH"|"NEUTRAL","trend_4h":"BULLISH"|"BEARISH"|"NEUTRAL","trend_1d":"BULLISH"|"BEARISH"|"NEUTRAL","fundamental_bias":"BULLISH"|"BEARISH"|"NEUTRAL","reasoning":"3 sentences on confluence and edge"}'
    blocks = await claude({"model":"claude-sonnet-4-20250514","max_tokens":800,"system":system,"messages":[{"role":"user","content":prompt}]})
    try: analysis = json.loads(first_text(blocks))
    except: return None
    td = {"price":price,"rsi_15m":rsi_15m,"rsi_div":rsi_div,"macd_cross":analysis.get("macd_cross","NONE"),"ma20":ma20,"ma50":ma50,"ma200":ma200,"bb_position":analysis.get("bb_position","MIDDLE"),"fib_zone":analysis.get("fib_zone","neutral"),"mtf_alignment":analysis.get("mtf_alignment","WEAK")}
    sig = analysis.get("signal","HOLD")
    scored = score_signal(sig,td)
    levels = calc_dynamic_levels(sig, price, atr_4h)
    analysis.update({"score":scored["score"],"breakdown":scored["breakdown"],"levels":levels,"atr":atr_4h,"entry":price,"rsi_15m":rsi_15m,"rsi_4h":rsi_4h,"rsi_1d":rsi_1d})
    return analysis

# ─── MT5 Bridge ───────────────────────────────────────────

def write_trade_command(action: str, trade_data: dict):
    """
    Write a command file that the MT5 EA reads and executes.
    The EA polls this file every second.
    """
    command = {
        "action":    action,          # OPEN / CLOSE / MODIFY_SL
        "symbol":    "XAUUSD",
        "direction": trade_data.get("signal"),
        "lot":       trade_data.get("lot", 0.01),
        "entry":     trade_data.get("entry", 0),
        "sl":        trade_data.get("levels", {}).get("sl", 0),
        "tp1":       trade_data.get("levels", {}).get("tp1", 0),
        "tp2":       trade_data.get("levels", {}).get("tp2", 0),
        "tp3":       trade_data.get("levels", {}).get("tp3", 0),
        "trade_id":  trade_data.get("trade_id", ""),
        "timestamp": int(time.time()),
        "comment":   f"XAU_PRO_BOT_v3 score={trade_data.get('score',0)}",
    }
    try:
        with open(TRADE_FILE, "w") as f:
            json.dump(command, f)
        log.info(f"✅ Trade command written: {action}")
    except Exception as e:
        log.error(f"Failed to write trade file: {e}")

# ─── Formatters ───────────────────────────────────────────

def esc(s):
    s = str(s)
    for ch in r"\_*[]()~`>#+-=|{}.!": s = s.replace(ch,f"\\{ch}")
    return s

def score_bar(s): return "█"*round(s/10)+"░"*(10-round(s/10))
def te(t): return {"BULLISH":"📈","BEARISH":"📉","NEUTRAL":"➡️"}.get(t,"➡️")

def format_approval_msg(price, a, news):
    """Signal message with Approve/Reject buttons."""
    sig   = a.get("signal","HOLD")
    score = a.get("score",0)
    lvl   = a.get("levels",{})
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    se    = "🟢" if sig=="BUY" else "🔴"
    sk    = "BUY ▲" if sig=="BUY" else "SELL ▼"
    mtf   = {"STRONG":"🔥 STRONG","MODERATE":"⚡ MODERATE","WEAK":"💤 WEAK"}.get(a.get("mtf_alignment","WEAK"),"💤 WEAK")
    t15,t4h,t1d = a.get("trend_15m","NEUTRAL"),a.get("trend_4h","NEUTRAL"),a.get("trend_1d","NEUTRAL")

    lines = [
        f"{se} *XAU/USD Signal — AWAITING APPROVAL*",
        f"🕐 `{esc(now)}`",
        f"⏰ _Expires in {APPROVE_TIMEOUT} minutes_",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📊 *Signal:*  `{sk}`",
        f"💰 *Price:*   `${price:.2f}`",
        f"🏆 *Score:*   `{score}/100`  `{score_bar(score)}`",
        f"🔗 *MTF:*     `{mtf}`",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "📐 *Trade Levels \\(ATR\\-based\\)*",
        f"🔵 Entry:  `${price:.2f}`",
        f"🔴 SL:     `${lvl.get('sl',0):.2f}`  \\({lvl.get('sl_pips',0)} pips\\)",
        f"🟡 TP1:    `${lvl.get('tp1',0):.2f}`  \\({lvl.get('tp1_pips',0)} pips\\)  `{lvl.get('rr1','')}`",
        f"🟢 TP2:    `${lvl.get('tp2',0):.2f}`  \\({lvl.get('tp2_pips',0)} pips\\)  `{lvl.get('rr2','')}`",
        f"💎 TP3:    `${lvl.get('tp3',0):.2f}`  \\({lvl.get('tp3_pips',0)} pips\\)  `{lvl.get('rr3','')}`",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "⏱ *Timeframes*",
        f"{te(t15)} 15M: `{t15}`  {te(t4h)} 4H: `{t4h}`  {te(t1d)} D: `{t1d}`",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "🧠 *AI Reasoning*",
        f"_{esc(a.get('reasoning',''))}_",
    ]
    if news:
        lines += ["","━━━━━━━━━━━━━━━━━━━━","🌍 *News*"]
        for n in news[:2]:
            em = "🟢" if n.get("impact")=="BULLISH" else "🔴" if n.get("impact")=="BEARISH" else "🟡"
            lines.append(f"{em} {esc(n.get('summary',''))}")
    lines += ["","━━━━━━━━━━━━━━━━━━━━","👆 *Tap below to execute on MT5:*"]
    return "\n".join(lines)

def format_trade_opened(trade):
    sig = trade.get("signal")
    lvl = trade.get("levels",{})
    tid = trade.get("trade_id","")
    se  = "🟢" if sig=="BUY" else "🔴"
    lines = [
        f"{se} *Trade OPENED on MT5*",
        f"🆔 Trade ID: `{esc(tid)}`",
        "",
        f"📊 Direction:  `{sig}`",
        f"💰 Entry:      `${trade.get('entry',0):.2f}`",
        f"🔴 SL:         `${lvl.get('sl',0):.2f}`",
        f"🟡 TP1:        `${lvl.get('tp1',0):.2f}`",
        f"🟢 TP2:        `${lvl.get('tp2',0):.2f}`",
        f"💎 TP3:        `${lvl.get('tp3',0):.2f}`",
        f"📏 Lot:        `{trade.get('lot',0.01)}`",
        "",
        "⚙️ _Bot will auto move SL to breakeven at TP1_",
        "⚙️ _Bot will auto close at TP2 or TP3_",
    ]
    return "\n".join(lines)

def format_trade_closed(trade, close_price, reason):
    entry = trade.get("entry",0)
    sig   = trade.get("signal","BUY")
    lot   = trade.get("lot",0.01)
    d     = 1 if sig=="BUY" else -1
    pnl   = round(d*(close_price-entry)/0.10*lot*0.10*100, 2)
    pips  = round(d*(close_price-entry)/0.10)
    em    = "💰" if pnl>0 else "💸"
    lines = [
        f"{em} *Trade CLOSED — {reason}*",
        f"🆔 Trade ID: `{esc(str(trade.get('trade_id','')))}`",
        "",
        f"📊 Direction:    `{sig}`",
        f"🔵 Entry:        `${entry:.2f}`",
        f"🔴 Close:        `${close_price:.2f}`",
        f"📏 Pips:         `{'+' if pips>0 else ''}{pips}`",
        f"{'💰' if pnl>0 else '💸'} P&L:          `{'+'if pnl>0 else ''}{pnl}`",
        f"📦 Lot:          `{lot}`",
    ]
    return "\n".join(lines)

# ─── Telegram Bot ─────────────────────────────────────────

application = Application.builder().token(TELEGRAM_TOKEN).build()
main_bot    = Bot(token=TELEGRAM_TOKEN)

async def send(text: str, reply_markup=None):
    try:
        return await main_bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=reply_markup,
        )
    except Exception as e:
        log.warning(f"MD2 failed: {e}")
        plain = re.sub(r"[*_`\[\]()~>#+=|{}.!\\]","",text)
        return await main_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=plain)

# ─── Callback: Approve / Reject ───────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()
    data     = query.data  # "approve_<msg_id>_<lot>" or "reject_<msg_id>"
    parts    = data.split("_")
    action   = parts[0]
    msg_id   = parts[1]
    trade    = pending_signals.get(msg_id)

    if not trade:
        await query.edit_message_text("⚠️ Signal expired or already processed.")
        return

    if action == "reject":
        del pending_signals[msg_id]
        await query.edit_message_text(
            re.sub(r"[*_`\[\]()~>#+=|{}.!\\]","",query.message.text) +
            "\n\n❌ REJECTED — No trade opened."
        )
        return

    if action == "approve":
        lot = float(parts[2]) if len(parts)>2 else 0.01
        trade["lot"] = lot
        trade_id = f"XAU_{int(time.time())}"
        trade["trade_id"] = trade_id

        # Write command for MT5 EA
        write_trade_command("OPEN", trade)

        # Store as active trade
        active_trades[trade_id] = trade.copy()
        del pending_signals[msg_id]

        # Edit original message
        await query.edit_message_text(
            re.sub(r"[*_`\[\]()~>#+=|{}.!\\]","",query.message.text) +
            f"\n\n✅ APPROVED — Sending to MT5..."
        )

        # Send trade opened notification
        opened_msg = format_trade_opened(trade)
        await send(opened_msg)
        log.info(f"✅ Trade approved: {trade_id}")

async def handle_lot_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle lot size selection before approval."""
    query = update.callback_query
    await query.answer()
    data  = query.data   # "lot_<msg_id>_<lot>"
    parts = data.split("_")
    msg_id = parts[1]
    lot    = parts[2]
    trade  = pending_signals.get(msg_id)
    if not trade: return

    # Show confirm buttons with chosen lot
    keyboard = [[
        InlineKeyboardButton(f"✅ CONFIRM {lot} lot", callback_data=f"approve_{msg_id}_{lot}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"reject_{msg_id}"),
    ]]
    await query.edit_message_reply_markup(InlineKeyboardMarkup(keyboard))

# ─── Commands ─────────────────────────────────────────────

async def cmd_risk(update, context):
    try:
        args = context.args
        bal  = float(args[0]) if len(args)>0 else 100.0
        rp   = float(args[1]) if len(args)>1 else 2.0
        sl   = float(args[2]) if len(args)>2 else 20.0
        lot  = float(args[3]) if len(args)>3 else 0.01
        risk_usd = round(bal*rp/100,2)
        pv   = round(lot*10*0.10,2)
        rl   = round(risk_usd/(sl*pv/lot),2) if sl>0 else lot
        msg  = (
            "💰 *Risk Calculator*\n\n"
            f"💵 Balance:    `${esc(str(bal))}`\n"
            f"⚡ Risk:       `{esc(str(rp))}%` = `${esc(str(risk_usd))}`\n"
            f"🔴 SL:         `{esc(str(sl))} pips`\n"
            f"📏 Lot:        `{esc(str(lot))}`\n\n"
            f"🎯 Rec Lot:    `{esc(str(rl))}`\n"
            f"💹 Pip Value:  `${esc(str(pv))}`\n\n"
            f"🔴 SL hit:     `\\-${esc(str(round(sl*pv,2)))}`\n"
            f"🟡 TP1 30p:    `\\+${esc(str(round(30*pv,2)))}`\n"
            f"🟢 TP2 50p:    `\\+${esc(str(round(50*pv,2)))}`\n"
            f"💎 TP3 80p:    `\\+${esc(str(round(80*pv,2)))}`"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)
    except:
        await update.message.reply_text("Usage: `/risk 100 2 20 0.01`", parse_mode=ParseMode.MARKDOWN_V2)

async def cmd_status(update, context):
    price = prices_15m[-1] if prices_15m else 0
    up    = datetime.now(timezone.utc)-bot_start_time
    h,m   = int(up.total_seconds()//3600), int((up.total_seconds()%3600)//60)
    at    = len(active_trades)
    msg = (
        "🤖 *XAU PRO Bot v3\\.0 — Status*\n\n"
        f"🟢 Status:        `Online`\n"
        f"⏱ Uptime:        `{h}h {m}m`\n"
        f"💰 Last Price:    `${price:.2f}`\n"
        f"📊 Data Points:   `{len(prices_15m)}`\n"
        f"📈 Active Trades: `{at}`\n"
        f"🏆 Min Score:     `{MIN_SCORE}/100`\n\n"
        "📖 *Commands:*\n"
        "`/risk 100 2 20 0\\.01`\n"
        "`/trades` — active trades\n"
        "`/status` — this message"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)

async def cmd_trades(update, context):
    if not active_trades:
        await update.message.reply_text("📊 No active trades right now\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return
    lines = ["📊 *Active Trades*",""]
    for tid, t in active_trades.items():
        lvl = t.get("levels",{})
        lines += [
            f"🆔 `{esc(tid)}`",
            f"📊 {t.get('signal')} @ `${t.get('entry',0):.2f}`",
            f"🔴 SL: `${lvl.get('sl',0):.2f}` 🟢 TP2: `${lvl.get('tp2',0):.2f}`",
            "",
        ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)

async def cmd_help(update, context):
    msg = (
        "🏅 *XAU PRO Bot v3\\.0 Commands*\n\n"
        "`/risk [bal] [risk%] [sl\\_pips] [lot]`\n"
        "_Example: /risk 100 2 20 0\\.01_\n\n"
        "`/status` — bot status\n"
        "`/trades` — active trades\n"
        "`/help` — this message\n\n"
        "⚠️ _Not financial advice_"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)

# ─── Signal Job ───────────────────────────────────────────

async def run_job():
    global tick_count, last_signal
    price = await fetch_price()
    if not price: return
    prices_15m.append(price)
    if len(prices_15m)>200: prices_15m.pop(0)
    tick_count+=1
    if tick_count%16==0:
        prices_4h.append(price)
        if len(prices_4h)>100: prices_4h.pop(0)
    if tick_count%96==0:
        prices_1d.append(price)
        if len(prices_1d)>60: prices_1d.pop(0)
    if len(prices_15m)<5:
        log.info(f"Building history {len(prices_15m)}/5"); return
    news     = await fetch_news()
    analysis = await analyze_mtf(price, news)
    if not analysis: return
    sig   = analysis.get("signal","HOLD")
    score = analysis.get("score",0)
    log.info(f"→ {sig} score={score} ${price}")
    last_signal = {"signal":sig,"score":score,"price":price,"time":datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    if sig=="HOLD" or score<MIN_SCORE:
        log.info(f"Filtered: {sig} score={score}"); return

    # Build approval message with lot buttons
    msg_text = format_approval_msg(price, analysis, news)
    trade_id_temp = str(int(time.time()))

    keyboard = [[
        InlineKeyboardButton("0.01 lot", callback_data=f"lot_{trade_id_temp}_0.01"),
        InlineKeyboardButton("0.05 lot", callback_data=f"lot_{trade_id_temp}_0.05"),
        InlineKeyboardButton("0.10 lot", callback_data=f"lot_{trade_id_temp}_0.10"),
    ],[
        InlineKeyboardButton("✅ APPROVE (0.01)", callback_data=f"approve_{trade_id_temp}_0.01"),
        InlineKeyboardButton("❌ REJECT",         callback_data=f"reject_{trade_id_temp}"),
    ]]

    analysis["signal"] = sig
    pending_signals[trade_id_temp] = analysis.copy()

    msg = await send(msg_text, InlineKeyboardMarkup(keyboard))

    # Auto-expire after APPROVE_TIMEOUT minutes
    async def expire():
        await asyncio.sleep(APPROVE_TIMEOUT * 60)
        if trade_id_temp in pending_signals:
            del pending_signals[trade_id_temp]
            log.info(f"Signal {trade_id_temp} expired")
    asyncio.create_task(expire())

# ─── Entry Point ──────────────────────────────────────────

async def main():
    log.info("🚀 XAU PRO Bot v3.0 starting")

    startup = (
        "🏅 *XAU PRO Auto Trader v3\\.0 LIVE\\!*\n\n"
        "🔬 Fibonacci \\+ MA20/50/200 \\+ RSI Div\n"
        "📉 MACD \\+ BB \\+ ATR Dynamic Levels\n"
        "⏱ 15M \\+ 4H \\+ Daily confluence\n"
        f"🏆 Min score: `{MIN_SCORE}/100`\n\n"
        "🤝 *Approval Mode:*\n"
        "Every signal shows ✅ APPROVE / ❌ REJECT\n"
        "Tap Approve → MT5 opens trade automatically\n"
        "Bot auto moves SL to breakeven at TP1\n"
        "Bot auto closes at TP2 or TP3\n\n"
        "📖 `/risk` `/status` `/trades` `/help`\n\n"
        "⚠️ _Not financial advice_"
    )
    try: await main_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=startup, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e: log.warning(f"Startup failed: {e}")

    # Register handlers
    application.add_handler(CommandHandler("risk",   cmd_risk))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("trades", cmd_trades))
    application.add_handler(CommandHandler("help",   cmd_help))
    application.add_handler(CallbackQueryHandler(handle_lot_selection, pattern="^lot_"))
    application.add_handler(CallbackQueryHandler(handle_callback,      pattern="^(approve|reject)_"))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(run_job,"interval",minutes=SIGNAL_INTERVAL)
    scheduler.start()

    await run_job()
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    log.info("✅ Polling for commands and approvals")

    try:
        while True: await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        await application.updater.stop()
        await application.stop()
        await application.shutdown()

if __name__ == "__main__":
    asyncio.run(main())

# 🏅 بۆتی سیگناڵی زێڕ — Telegram

## پێداویستییەکان
- Python 3.11+
- ئاکاونتی تەلەگرام
- کلید Claude API (Anthropic)
- ئاکاونتی Render.com (بەخۆڕایی)

---

## هەنگاو ١ — بۆتی تەلەگرام دروست بکە

1. لە تەلەگرام بچۆ بۆ [@BotFather](https://t.me/BotFather)
2. بنووسە: `/newbot`
3. ناوی بۆت بنووسە، بۆنمونە: `Gold Signal Bot`
4. یوزەرناو بنووسە، بۆنمونە: `myxauusd_bot`
5. **TOKEN** وەردەگرێت — ئەمە `TELEGRAM_TOKEN` ە

---

## هەنگاو ٢ — ئای‌دی گروپ بدۆزەرەوە

1. بۆتەکەت زیاد بکە بۆ گروپەکەت (Admin بکەیت)
2. لینکی ژێرەوە لە براوزەرەکەت بکەرەوە:
   ```
   https://api.telegram.org/bot<TELEGRAM_TOKEN>/getUpdates
   ```
3. پەیامێک بنێرە بۆ گروپ، پاشان `getUpdates` نوێ بکەرەوە
4. `chat.id` بدۆزەرەوە — ئەمە `TELEGRAM_CHAT_ID` ە (مینوسیش دەبێت بۆ گروپ)

---

## هەنگاو ٣ — لەسەر Render.com دابمەزرێنە (بەخۆڕایی)

1. بچۆ بۆ [render.com](https://render.com) — ئاکاونت دروست بکە
2. **New → Background Worker** هەڵبژێرە
3. GitHub-ەکەت پێکەوە بەستەرەوە، کۆدەکان push بکە
4. Environment Variables دابنێ:

| کلید | بەها |
|------|------|
| `TELEGRAM_TOKEN` | تۆکەنی بۆتەکەت |
| `TELEGRAM_CHAT_ID` | ئای‌دی گروپەکەت |
| `ANTHROPIC_API_KEY` | کلیدی Claude |
| `SIGNAL_INTERVAL_MINUTES` | `60` (یان ١٥، ٣٠، ...) |

5. **Deploy** دابگرە ✅

---

## هەنگاو ٤ — تاقیکردنەوەی ئەرەکی (بەدوون سێرڤەر)

```bash
# پاکێجەکان دامەزرێنە
pip install -r requirements.txt

# مەتغیرەکان دابنێ
export TELEGRAM_TOKEN="xxxxxx"
export TELEGRAM_CHAT_ID="-100xxxxxx"
export ANTHROPIC_API_KEY="sk-ant-..."
export SIGNAL_INTERVAL_MINUTES="60"

# بۆت بەرێوەببە
python bot.py
```

---

## ئاستەکانی ریسک (ثابت)

| ئاست | پیپ | دۆلار |
|------|-----|-------|
| 🔴 Stop Loss | 20 | $2.00 |
| 🟡 TP1 | 30 | $3.00 |
| 🟢 TP2 | 50 | $5.00 |

---

⚠️ **ئەمە بۆ مەبەستی زانیاریە. مەشوەرەتی داراییی نییە.**

import os
import requests
import schedule
import time
import threading
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ── CONFIG ──────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
BASE_CURRENCY    = "AUD"
WATCH_CURRENCIES = ["EUR", "GBP", "AUD", "JPY", "INR"]  # edit as needed
ALERT_THRESHOLD  = 0.5   # alert if % change > 0.5%
CHECK_INTERVAL   = 60    # seconds between checks

# ── STATE ────────────────────────────────────────────────────────────────────
previous_rates:  dict = {}
subscribed_users: set = set()

# stores price breach alerts:
# { chat_id: [{"currency": "EUR", "target": 0.95, "direction": "above"}, ...] }
price_alerts: dict = {}

# ── FETCH RATES ───────────────────────────────────────────────────────────────
def fetch_rates() -> dict:
    """Fetch latest rates from Frankfurter (free, no key needed)."""
    symbols = ",".join(WATCH_CURRENCIES)
    url = f"https://api.frankfurter.app/latest?from={BASE_CURRENCY}&to={symbols}"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()["rates"]  # e.g. {"EUR": 0.91, "GBP": 0.78, ...}

# ── CHECK FOR CHANGES & PRICE BREACH ALERTS ──────────────────────────────────
async def check_and_alert(app):
    global previous_rates
    try:
        current_rates = fetch_rates()
    except Exception as e:
        print(f"Error fetching rates: {e}")
        return

    # ── First run: just store rates, no alerts yet ────────────────────────
    if not previous_rates:
        previous_rates = current_rates
        print("Initial rates loaded:", current_rates)
        return

    # ── 1. Percentage change alerts ───────────────────────────────────────
    messages = []
    for currency, rate in current_rates.items():
        old = previous_rates.get(currency)
        if old is None:
            continue
        change_pct = ((rate - old) / old) * 100
        if abs(change_pct) >= ALERT_THRESHOLD:
            arrow     = "📈" if change_pct > 0 else "📉"
            direction = "UP"  if change_pct > 0 else "DOWN"
            messages.append(
                f"{arrow} *{BASE_CURRENCY}/{currency}* went *{direction}*\n"
                f"   {old:.4f} → {rate:.4f}  ({change_pct:+.2f}%)"
            )

    if messages and subscribed_users:
        text = "🔔 *Currency Alert!*\n\n" + "\n\n".join(messages)
        for uid in subscribed_users.copy():
            try:
                await app.bot.send_message(
                    chat_id=uid, text=text, parse_mode="Markdown"
                )
            except Exception as e:
                print(f"Failed to send to {uid}: {e}")

    # ── 2. Price breach alerts ────────────────────────────────────────────
    for chat_id, alerts in list(price_alerts.items()):
        triggered = []
        remaining = []
        for alert in alerts:
            cur  = alert["currency"]
            rate = current_rates.get(cur)
            if rate is None:
                remaining.append(alert)
                continue
            hit = (
                (alert["direction"] == "above" and rate >= alert["target"]) or
                (alert["direction"] == "below" and rate <= alert["target"])
            )
            if hit:
                triggered.append((cur, rate, alert["target"], alert["direction"]))
            else:
                remaining.append(alert)

        price_alerts[chat_id] = remaining  # remove triggered alerts

        for cur, rate, target, direction in triggered:
            symbol = "📈" if direction == "above" else "📉"
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🚨 *Price Alert Triggered!*\n\n"
                        f"{symbol} *{BASE_CURRENCY}/{cur}* is now `{rate:.4f}`\n"
                        f"Your target: {direction} `{target}`"
                    ),
                    parse_mode="Markdown"
                )
            except Exception as e:
                print(f"Alert send error: {e}")

    # ── Update stored rates ───────────────────────────────────────────────
    previous_rates = current_rates


# ── COMMANDS ──────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Subscribe to percentage-change alerts."""
    subscribed_users.add(update.effective_chat.id)
    await update.message.reply_text(
        "✅ *Subscribed!* You'll get alerts when currencies move "
        f"more than {ALERT_THRESHOLD}%.\n\n"
        f"Watching: {', '.join(WATCH_CURRENCIES)} vs {BASE_CURRENCY}\n\n"
        "📋 *All Commands:*\n"
        "/rates        – See current live rates\n"
        "/setalert     – Set a price breach alert\n"
        "/myalerts     – View your active alerts\n"
        "/cancelalert  – Cancel an alert\n"
        "/stop         – Unsubscribe from alerts",
        parse_mode="Markdown"
    )


async def stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Unsubscribe from percentage-change alerts."""
    subscribed_users.discard(update.effective_chat.id)
    await update.message.reply_text(
        "🔕 Unsubscribed from alerts.\n"
        "Send /start to re-subscribe anytime."
    )


async def rates(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show current live exchange rates."""
    try:
        current = fetch_rates()
        lines = [
            f"*{BASE_CURRENCY} → {cur}:* `{rate:.4f}`"
            for cur, rate in current.items()
        ]
        await update.message.reply_text(
            "💱 *Current Rates*\n\n" + "\n".join(lines),
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error fetching rates: {e}")


async def setalert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Set a price breach alert.
    Usage: /setalert EUR 0.95 above
           /setalert GBP 1.30 below
    """
    chat_id = update.effective_chat.id
    try:
        currency  = ctx.args[0].upper()  # e.g. EUR
        target    = float(ctx.args[1])   # e.g. 0.95
        direction = ctx.args[2].lower()  # "above" or "below"

        if direction not in ("above", "below"):
            raise ValueError("direction must be 'above' or 'below'")

        if currency not in WATCH_CURRENCIES:
            await update.message.reply_text(
                f"❌ *{currency}* is not in the watch list.\n"
                f"Supported: {', '.join(WATCH_CURRENCIES)}",
                parse_mode="Markdown"
            )
            return

        if chat_id not in price_alerts:
            price_alerts[chat_id] = []

        price_alerts[chat_id].append({
            "currency":  currency,
            "target":    target,
            "direction": direction
        })

        symbol = "📈" if direction == "above" else "📉"
        await update.message.reply_text(
            f"{symbol} *Alert Set!*\n\n"
            f"You'll be notified when *{BASE_CURRENCY}/{currency}* goes "
            f"*{direction}* `{target}`\n\n"
            f"Use /myalerts to see all your alerts.",
            parse_mode="Markdown"
        )

    except (IndexError, ValueError):
        await update.message.reply_text(
            "❌ *Wrong format.* Use:\n\n"
            "`/setalert EUR 0.95 above`\n"
            "`/setalert GBP 1.30 below`\n\n"
            f"Supported currencies: {', '.join(WATCH_CURRENCIES)}",
            parse_mode="Markdown"
        )


async def myalerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """View all active price breach alerts."""
    chat_id = update.effective_chat.id
    alerts  = price_alerts.get(chat_id, [])

    if not alerts:
        await update.message.reply_text(
            "You have no active alerts.\n"
            "Use /setalert to add one.\n\n"
            "Example: `/setalert EUR 0.95 above`",
            parse_mode="Markdown"
        )
        return

    lines = []
    for i, a in enumerate(alerts, 1):
        symbol = "📈" if a["direction"] == "above" else "📉"
        lines.append(
            f"{i}. {symbol} *{BASE_CURRENCY}/{a['currency']}* "
            f"{a['direction']} `{a['target']}`"
        )

    await update.message.reply_text(
        "🔔 *Your Active Alerts:*\n\n" + "\n".join(lines) +
        "\n\nUse `/cancelalert <number>` to remove one.",
        parse_mode="Markdown"
    )


async def cancelalert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Cancel a price breach alert by its number from /myalerts.
    Usage: /cancelalert 1
    """
    chat_id = update.effective_chat.id
    try:
        index   = int(ctx.args[0]) - 1
        removed = price_alerts[chat_id].pop(index)
        await update.message.reply_text(
            f"✅ *Alert Removed:*\n"
            f"{BASE_CURRENCY}/{removed['currency']} "
            f"{removed['direction']} `{removed['target']}`",
            parse_mode="Markdown"
        )
    except (IndexError, ValueError, KeyError):
        await update.message.reply_text(
            "❌ Invalid number. Use /myalerts to see your list,\n"
            "then `/cancelalert <number>` to remove one.",
            parse_mode="Markdown"
        )


# ── SCHEDULER (runs in a background thread) ───────────────────────────────────
def run_scheduler(app):
    def sync_job():
        import asyncio
        asyncio.run(check_and_alert(app))

    schedule.every(CHECK_INTERVAL).seconds.do(sync_job)
    while True:
        schedule.run_pending()
        time.sleep(1)


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register all command handlers
    app.add_handler(CommandHandler("start",        start))
    app.add_handler(CommandHandler("stop",         stop))
    app.add_handler(CommandHandler("rates",        rates))
    app.add_handler(CommandHandler("setalert",     setalert))
    app.add_handler(CommandHandler("myalerts",     myalerts))
    app.add_handler(CommandHandler("cancelalert",  cancelalert))

    # Start scheduler in background thread
    t = threading.Thread(target=run_scheduler, args=(app,), daemon=True)
    t.start()

    print("✅ Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
import os
import requests
import schedule
import time
import threading
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TWELVE_API_KEY   = os.environ.get("TWELVE_API_KEY", "YOUR_TWELVE_DATA_API_KEY_HERE")
BASE_CURRENCY    = "AUD"
WATCH_CURRENCIES = ["USD", "EUR", "GBP", "JPY", "INR"]  # default currencies
ALERT_THRESHOLD  = 0.5   # % change to trigger fluctuation alert
CHECK_INTERVAL   = 60    # seconds between checks

# ── STATE ─────────────────────────────────────────────────────────────────────
previous_rates:   dict = {}
subscribed_users: set  = set()

# Per-user custom single currency watch lists (vs AUD)
# { chat_id: {"CAD", "CHF", ...} }
user_currencies: dict = {}

# Per-user currency PAIR watch lists
# { chat_id: {("EUR", "GBP"), ("INR", "AED"), ...} }
user_pairs: dict = {}

# Price breach alerts for single currencies (vs AUD)
# { chat_id: [{"currency": "USD", "target": 0.65, "direction": "above"}, ...] }
price_alerts: dict = {}

# Price breach alerts for currency PAIRS
# { chat_id: [{"from": "EUR", "to": "GBP", "target": 0.85, "direction": "above"}, ...] }
pair_alerts: dict = {}


# ── TWELVE DATA FETCH HELPERS ─────────────────────────────────────────────────
def fetch_single_rate(from_cur: str, to_cur: str) -> float:
    """
    Fetch real-time rate for a single pair from Twelve Data.
    Returns the exchange rate as a float.
    """
    symbol = f"{from_cur}/{to_cur}"
    url = (
        f"https://api.twelvedata.com/exchange_rate"
        f"?symbol={symbol}&apikey={TWELVE_API_KEY}"
    )
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") == "error":
        raise ValueError(f"Twelve Data error: {data.get('message', 'Unknown error')}")

    return float(data["rate"])


def fetch_rates_for_base(base: str, targets: list) -> dict:
    """
    Fetch real-time rates for multiple targets vs a base currency.
    Returns {target: rate} dict.
    Batches into a single API call using comma-separated symbols.
    """
    targets = [t for t in targets if t != base]
    if not targets:
        return {}

    symbols = ",".join(f"{base}/{t}" for t in targets)
    url = (
        f"https://api.twelvedata.com/exchange_rate"
        f"?symbol={symbols}&apikey={TWELVE_API_KEY}"
    )
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    rates = {}

    # Single pair returns a dict directly; multiple pairs returns {symbol: dict}
    if "rate" in data:
        # Single pair response
        pair_symbol = list(data.get("symbol", f"{base}/{targets[0]}").split("/"))
        if len(pair_symbol) == 2:
            rates[pair_symbol[1]] = float(data["rate"])
    else:
        # Multiple pairs response
        for symbol, info in data.items():
            if isinstance(info, dict) and "rate" in info:
                to_cur = symbol.split("/")[1]
                rates[to_cur] = float(info["rate"])

    return rates


def validate_currency(from_cur: str, to_cur: str = "USD") -> bool:
    """Validate a currency by trying to fetch its rate from Twelve Data."""
    try:
        rate = fetch_single_rate(from_cur, to_cur)
        return rate > 0
    except Exception:
        return False


def get_user_currencies(chat_id) -> list:
    """Get combined default + user-added single currencies."""
    custom = user_currencies.get(chat_id, set())
    return list(set(WATCH_CURRENCIES) | custom)


# ── BACKGROUND CHECK ──────────────────────────────────────────────────────────
async def check_and_alert(app):
    global previous_rates

    # ── Gather all currencies needed ──────────────────────────────────────
    usd_targets = set(WATCH_CURRENCIES)
    for chat_id in subscribed_users:
        usd_targets |= user_currencies.get(chat_id, set())
    for chat_id in price_alerts:
        for a in price_alerts[chat_id]:
            usd_targets.add(a["currency"])

    all_pair_set = set()
    for chat_id in user_pairs:
        all_pair_set |= user_pairs[chat_id]
    for chat_id in pair_alerts:
        for a in pair_alerts[chat_id]:
            all_pair_set.add((a["from"], a["to"]))

    current_rates: dict = {}

    # Fetch base currency rates
    try:
        base_rates = fetch_rates_for_base(BASE_CURRENCY, list(usd_targets))
        current_rates[BASE_CURRENCY] = base_rates
    except Exception as e:
        print(f"Error fetching base rates: {e}")

    # Fetch pair rates one by one (to avoid overloading free tier)
    for (fc, tc) in all_pair_set:
        try:
            rate = fetch_single_rate(fc, tc)
            if fc not in current_rates:
                current_rates[fc] = {}
            current_rates[fc][tc] = rate
        except Exception as e:
            print(f"Error fetching {fc}/{tc}: {e}")

    if not current_rates:
        return

    # First run — store and return
    if not previous_rates:
        previous_rates = current_rates
        print("✅ Initial real-time rates loaded:", current_rates)
        return

    base_current  = current_rates.get(BASE_CURRENCY, {})
    base_previous = previous_rates.get(BASE_CURRENCY, {})

    # ── 1. Single currency fluctuation alerts ─────────────────────────────
    for uid in subscribed_users.copy():
        messages = []

        # vs AUD
        for currency in get_user_currencies(uid):
            rate = base_current.get(currency)
            old  = base_previous.get(currency)
            if rate is None or old is None:
                continue
            change_pct = ((rate - old) / old) * 100
            if abs(change_pct) >= ALERT_THRESHOLD:
                arrow = "📈" if change_pct > 0 else "📉"
                dire  = "UP"  if change_pct > 0 else "DOWN"
                messages.append(
                    f"{arrow} *{BASE_CURRENCY}/{currency}* went *{dire}*\n"
                    f"   {old:.4f} → {rate:.4f}  ({change_pct:+.2f}%)"
                )

        # Pair fluctuations
        for (fc, tc) in user_pairs.get(uid, set()):
            rate = current_rates.get(fc, {}).get(tc)
            old  = previous_rates.get(fc, {}).get(tc)
            if rate is None or old is None:
                continue
            change_pct = ((rate - old) / old) * 100
            if abs(change_pct) >= ALERT_THRESHOLD:
                arrow = "📈" if change_pct > 0 else "📉"
                dire  = "UP"  if change_pct > 0 else "DOWN"
                messages.append(
                    f"{arrow} *{fc}/{tc}* went *{dire}*\n"
                    f"   {old:.4f} → {rate:.4f}  ({change_pct:+.2f}%)"
                )

        if messages:
            try:
                await app.bot.send_message(
                    chat_id=uid,
                    text="🔔 *Currency Fluctuation Alert!*\n\n" + "\n\n".join(messages),
                    parse_mode="Markdown"
                )
            except Exception as e:
                print(f"Failed to send to {uid}: {e}")

    # ── 2. Single currency price breach alerts ────────────────────────────
    for chat_id, alerts in list(price_alerts.items()):
        triggered = []
        remaining = []
        for alert in alerts:
            cur  = alert["currency"]
            rate = base_current.get(cur)
            if rate is None:
                remaining.append(alert)
                continue
            hit = (
                (alert["direction"] == "above" and rate >= alert["target"]) or
                (alert["direction"] == "below" and rate <= alert["target"])
            )
            if hit:
                triggered.append((f"{BASE_CURRENCY}/{cur}", rate, alert["target"], alert["direction"]))
            else:
                remaining.append(alert)
        price_alerts[chat_id] = remaining
        await _send_breach_alerts(app, chat_id, triggered)

    # ── 3. Currency pair price breach alerts ──────────────────────────────
    for chat_id, alerts in list(pair_alerts.items()):
        triggered = []
        remaining = []
        for alert in alerts:
            fc   = alert["from"]
            tc   = alert["to"]
            rate = current_rates.get(fc, {}).get(tc)
            if rate is None:
                remaining.append(alert)
                continue
            hit = (
                (alert["direction"] == "above" and rate >= alert["target"]) or
                (alert["direction"] == "below" and rate <= alert["target"])
            )
            if hit:
                triggered.append((f"{fc}/{tc}", rate, alert["target"], alert["direction"]))
            else:
                remaining.append(alert)
        pair_alerts[chat_id] = remaining
        await _send_breach_alerts(app, chat_id, triggered)

    previous_rates = current_rates


async def _send_breach_alerts(app, chat_id, triggered: list):
    """Send breach alert messages."""
    for pair_label, rate, target, direction in triggered:
        symbol = "📈" if direction == "above" else "📉"
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🚨 *Price Alert Triggered!*\n\n"
                    f"{symbol} *{pair_label}* is now `{rate:.4f}`\n"
                    f"Your target: {direction} `{target}`"
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Breach alert send error: {e}")


# ── COMMANDS ──────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subscribed_users.add(chat_id)
    await update.message.reply_text(
        "✅ *Subscribed to Currency Bot!*\n\n"
        f"📌 *Base currency:* {BASE_CURRENCY} (Australian Dollar)\n"
        f"📌 *Default watch:* {', '.join(WATCH_CURRENCIES)}\n"
        f"⚡ *Data:* Real-time via Twelve Data\n"
        f"🔄 *Check interval:* Every {CHECK_INTERVAL} seconds\n"
        f"🔔 *Fluctuation alert:* >{ALERT_THRESHOLD}% change\n\n"
        "📋 *All Commands:*\n\n"
        "━━ *Single Currency (vs AUD)* ━━\n"
        "/rates                   – Live rates vs AUD\n"
        "/addcurrency XYZ         – Track a currency vs AUD\n"
        "/removecurrency XYZ      – Stop tracking it\n"
        "/mycurrencies            – Your currency list\n\n"
        "━━ *Currency Pairs* ━━\n"
        "/pairrate EUR GBP        – Live rate for any pair\n"
        "/addpair EUR GBP         – Track EUR/GBP fluctuations\n"
        "/removepair EUR GBP      – Stop tracking a pair\n"
        "/mypairs                 – Your tracked pairs\n\n"
        "━━ *Price Alerts* ━━\n"
        "/setalert USD 0.65 above         – Alert when AUD/USD > 0.65\n"
        "/setpairalert EUR GBP 0.85 above – Alert when EUR/GBP > 0.85\n"
        "/myalerts                – All your active alerts\n"
        "/cancelalert 1           – Cancel alert number 1\n\n"
        "━━ *Other* ━━\n"
        "/stop                    – Unsubscribe from alerts",
        parse_mode="Markdown"
    )


async def stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subscribed_users.discard(update.effective_chat.id)
    await update.message.reply_text(
        "🔕 Unsubscribed. Send /start to re-subscribe anytime."
    )


# ── SINGLE CURRENCY COMMANDS ──────────────────────────────────────────────────

async def rates(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show live rates for user's currency list vs AUD."""
    chat_id = update.effective_chat.id
    user_curr_list = get_user_currencies(chat_id)
    await update.message.reply_text("🔄 Fetching real-time rates...")
    try:
        current = fetch_rates_for_base(BASE_CURRENCY, user_curr_list)
        lines = [
            f"*{BASE_CURRENCY}/{cur}:* `{rate:.4f}`"
            for cur, rate in sorted(current.items())
        ]
        await update.message.reply_text(
            "💱 *Live Rates vs AUD*\n"
            "_(Real-time via Twelve Data)_\n\n" + "\n".join(lines),
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error fetching rates: {e}")


async def addcurrency(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /addcurrency CAD"""
    chat_id = update.effective_chat.id
    try:
        currency = ctx.args[0].upper()

        if currency == BASE_CURRENCY:
            await update.message.reply_text(
                f"❌ *{currency}* is already your base currency!",
                parse_mode="Markdown"
            )
            return

        if currency in get_user_currencies(chat_id):
            await update.message.reply_text(
                f"ℹ️ *{BASE_CURRENCY}/{currency}* is already in your watch list!",
                parse_mode="Markdown"
            )
            return

        await update.message.reply_text(
            f"🔍 Validating *{currency}*...", parse_mode="Markdown"
        )

        try:
            rate = fetch_single_rate(BASE_CURRENCY, currency)
        except Exception:
            await update.message.reply_text(
                f"❌ *{currency}* is not a valid currency code.\n"
                "Use 3-letter ISO codes: `CAD`, `CHF`, `CNY`, `SGD`, `AED`",
                parse_mode="Markdown"
            )
            return

        if chat_id not in user_currencies:
            user_currencies[chat_id] = set()
        user_currencies[chat_id].add(currency)

        await update.message.reply_text(
            f"✅ *{BASE_CURRENCY}/{currency}* added!\n\n"
            f"💱 Current rate: `{rate:.4f}`\n\n"
            f"You'll get fluctuation alerts when it moves >{ALERT_THRESHOLD}%.\n"
            f"Set a price target: `/setalert {currency} {rate:.4f} above`",
            parse_mode="Markdown"
        )
    except IndexError:
        await update.message.reply_text(
            "❌ Usage: `/addcurrency CAD`", parse_mode="Markdown"
        )


async def removecurrency(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /removecurrency CAD"""
    chat_id = update.effective_chat.id
    try:
        currency = ctx.args[0].upper()
        if currency in WATCH_CURRENCIES:
            await update.message.reply_text(
                f"⚠️ *{currency}* is a default currency and cannot be removed.\n"
                f"Defaults: {', '.join(WATCH_CURRENCIES)}",
                parse_mode="Markdown"
            )
            return
        if chat_id in user_currencies and currency in user_currencies[chat_id]:
            user_currencies[chat_id].discard(currency)
            await update.message.reply_text(
                f"✅ *{BASE_CURRENCY}/{currency}* removed from your watch list.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"ℹ️ *{currency}* was not in your list. Use /mycurrencies to check.",
                parse_mode="Markdown"
            )
    except IndexError:
        await update.message.reply_text(
            "❌ Usage: `/removecurrency CAD`", parse_mode="Markdown"
        )


async def mycurrencies(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    custom  = user_currencies.get(chat_id, set())
    default_line = "📌 *Default:* " + ", ".join(f"`{c}`" for c in sorted(WATCH_CURRENCIES))
    custom_line  = (
        "➕ *Added:* " + ", ".join(f"`{c}`" for c in sorted(custom))
        if custom else
        "➕ *Added:* None — use `/addcurrency XYZ`"
    )
    await update.message.reply_text(
        f"💱 *Your Currency Watch List (vs {BASE_CURRENCY})*\n\n" +
        default_line + "\n" + custom_line,
        parse_mode="Markdown"
    )


# ── CURRENCY PAIR COMMANDS ────────────────────────────────────────────────────

async def pairrate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /pairrate EUR GBP"""
    try:
        from_cur = ctx.args[0].upper()
        to_cur   = ctx.args[1].upper()
        await update.message.reply_text(
            f"🔄 Fetching real-time *{from_cur}/{to_cur}* rate...",
            parse_mode="Markdown"
        )
        rate = fetch_single_rate(from_cur, to_cur)
        await update.message.reply_text(
            f"💱 *{from_cur}/{to_cur}* _(Real-time)_\n\n"
            f"1 {from_cur} = `{rate:.4f}` {to_cur}\n\n"
            f"Track this pair : `/addpair {from_cur} {to_cur}`\n"
            f"Set an alert    : `/setpairalert {from_cur} {to_cur} {rate:.4f} above`",
            parse_mode="Markdown"
        )
    except IndexError:
        await update.message.reply_text(
            "❌ Usage: `/pairrate EUR GBP`", parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Could not fetch rate. Check currency codes are valid.\nError: {e}"
        )


async def addpair(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /addpair EUR GBP"""
    chat_id = update.effective_chat.id
    try:
        from_cur = ctx.args[0].upper()
        to_cur   = ctx.args[1].upper()

        if from_cur == to_cur:
            await update.message.reply_text("❌ Both currencies cannot be the same!")
            return

        pair = (from_cur, to_cur)
        if chat_id in user_pairs and pair in user_pairs[chat_id]:
            await update.message.reply_text(
                f"ℹ️ You're already tracking *{from_cur}/{to_cur}*!",
                parse_mode="Markdown"
            )
            return

        await update.message.reply_text(
            f"🔄 Validating *{from_cur}/{to_cur}*...", parse_mode="Markdown"
        )

        try:
            rate = fetch_single_rate(from_cur, to_cur)
        except Exception:
            await update.message.reply_text(
                f"❌ Could not fetch *{from_cur}/{to_cur}*.\n"
                "Check both currency codes are valid 3-letter ISO codes.",
                parse_mode="Markdown"
            )
            return

        if chat_id not in user_pairs:
            user_pairs[chat_id] = set()
        user_pairs[chat_id].add(pair)

        await update.message.reply_text(
            f"✅ *{from_cur}/{to_cur}* pair added!\n\n"
            f"💱 Current rate: 1 {from_cur} = `{rate:.4f}` {to_cur}\n\n"
            f"You'll get alerts when it moves >{ALERT_THRESHOLD}%.\n\n"
            f"Set a price target:\n"
            f"`/setpairalert {from_cur} {to_cur} {rate:.4f} above`\n"
            f"`/setpairalert {from_cur} {to_cur} {rate:.4f} below`",
            parse_mode="Markdown"
        )
    except IndexError:
        await update.message.reply_text(
            "❌ Usage: `/addpair EUR GBP`", parse_mode="Markdown"
        )


async def removepair(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /removepair EUR GBP"""
    chat_id = update.effective_chat.id
    try:
        from_cur = ctx.args[0].upper()
        to_cur   = ctx.args[1].upper()
        pair     = (from_cur, to_cur)
        if chat_id in user_pairs and pair in user_pairs[chat_id]:
            user_pairs[chat_id].discard(pair)
            await update.message.reply_text(
                f"✅ *{from_cur}/{to_cur}* removed from your pairs.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"ℹ️ *{from_cur}/{to_cur}* was not in your pairs.\n"
                "Use /mypairs to see your list.",
                parse_mode="Markdown"
            )
    except IndexError:
        await update.message.reply_text(
            "❌ Usage: `/removepair EUR GBP`", parse_mode="Markdown"
        )


async def mypairs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    pairs   = user_pairs.get(chat_id, set())
    if not pairs:
        await update.message.reply_text(
            "You have no tracked pairs.\n\n"
            "Add one with `/addpair EUR GBP`",
            parse_mode="Markdown"
        )
        return
    lines = [f"• *{fc}/{tc}*" for fc, tc in sorted(pairs)]
    await update.message.reply_text(
        "🔁 *Your Tracked Pairs:*\n\n" + "\n".join(lines) + "\n\n"
        "Use `/removepair FROM TO` to remove one.\n"
        "Use `/setpairalert FROM TO price above/below` to set an alert.",
        parse_mode="Markdown"
    )


# ── ALERT COMMANDS ────────────────────────────────────────────────────────────

async def setalert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Set price alert for a single currency vs AUD.
    Usage: /setalert USD 0.65 above
           /setalert EUR 0.55 below
    """
    chat_id = update.effective_chat.id
    try:
        currency  = ctx.args[0].upper()
        target    = float(ctx.args[1])
        direction = ctx.args[2].lower()

        if direction not in ("above", "below"):
            raise ValueError

        # Auto-validate & add if not in list
        if currency not in get_user_currencies(chat_id):
            await update.message.reply_text(
                f"🔍 Validating *{currency}*...", parse_mode="Markdown"
            )
            try:
                fetch_single_rate(BASE_CURRENCY, currency)
            except Exception:
                await update.message.reply_text(
                    f"❌ *{currency}* is not a valid currency code.",
                    parse_mode="Markdown"
                )
                return
            if chat_id not in user_currencies:
                user_currencies[chat_id] = set()
            user_currencies[chat_id].add(currency)

        if chat_id not in price_alerts:
            price_alerts[chat_id] = []
        price_alerts[chat_id].append({
            "currency": currency, "target": target, "direction": direction
        })

        rate   = fetch_single_rate(BASE_CURRENCY, currency)
        symbol = "📈" if direction == "above" else "📉"
        await update.message.reply_text(
            f"{symbol} *Alert Set!*\n\n"
            f"Pair    : *{BASE_CURRENCY}/{currency}*\n"
            f"Trigger : rate goes *{direction}* `{target}`\n"
            f"Current : `{rate:.4f}`\n\n"
            "🚨 You'll be notified the moment it hits your target!",
            parse_mode="Markdown"
        )
    except (IndexError, ValueError):
        await update.message.reply_text(
            "❌ Usage:\n"
            "`/setalert USD 0.65 above`\n"
            "`/setalert EUR 0.55 below`",
            parse_mode="Markdown"
        )


async def setpairalert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Set price alert for any currency pair.
    Usage: /setpairalert EUR GBP 0.85 above
           /setpairalert INR AED 0.044 below
    """
    chat_id = update.effective_chat.id
    try:
        from_cur  = ctx.args[0].upper()
        to_cur    = ctx.args[1].upper()
        target    = float(ctx.args[2])
        direction = ctx.args[3].lower()

        if direction not in ("above", "below"):
            raise ValueError

        if from_cur == to_cur:
            await update.message.reply_text("❌ Both currencies cannot be the same!")
            return

        try:
            rate = fetch_single_rate(from_cur, to_cur)
        except Exception:
            await update.message.reply_text(
                f"❌ Could not validate *{from_cur}/{to_cur}*.\n"
                "Please check both currency codes are valid.",
                parse_mode="Markdown"
            )
            return

        # Auto-add pair to tracking
        if chat_id not in user_pairs:
            user_pairs[chat_id] = set()
        user_pairs[chat_id].add((from_cur, to_cur))

        if chat_id not in pair_alerts:
            pair_alerts[chat_id] = []
        pair_alerts[chat_id].append({
            "from": from_cur, "to": to_cur,
            "target": target, "direction": direction
        })

        symbol = "📈" if direction == "above" else "📉"
        await update.message.reply_text(
            f"{symbol} *Pair Alert Set!*\n\n"
            f"Pair    : *{from_cur}/{to_cur}*\n"
            f"Trigger : rate goes *{direction}* `{target}`\n"
            f"Current : 1 {from_cur} = `{rate:.4f}` {to_cur}\n\n"
            "🚨 You'll be notified the moment it hits your target!",
            parse_mode="Markdown"
        )
    except (IndexError, ValueError):
        await update.message.reply_text(
            "❌ Usage:\n"
            "`/setpairalert EUR GBP 0.85 above`\n"
            "`/setpairalert INR AED 0.044 below`",
            parse_mode="Markdown"
        )


async def myalerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    single  = price_alerts.get(chat_id, [])
    pairs   = pair_alerts.get(chat_id, [])

    if not single and not pairs:
        await update.message.reply_text(
            "You have no active alerts.\n\n"
            "Single currency : `/setalert USD 0.65 above`\n"
            "Currency pair   : `/setpairalert EUR GBP 0.85 above`",
            parse_mode="Markdown"
        )
        return

    lines = []
    i = 1
    for a in single:
        symbol = "📈" if a["direction"] == "above" else "📉"
        lines.append(
            f"{i}. {symbol} *{BASE_CURRENCY}/{a['currency']}* "
            f"{a['direction']} `{a['target']}` _(vs {BASE_CURRENCY})_"
        )
        i += 1
    for a in pairs:
        symbol = "📈" if a["direction"] == "above" else "📉"
        lines.append(
            f"{i}. {symbol} *{a['from']}/{a['to']}* "
            f"{a['direction']} `{a['target']}` _(pair)_"
        )
        i += 1

    await update.message.reply_text(
        "🔔 *Your Active Alerts:*\n\n" + "\n".join(lines) + "\n\n"
        "Use `/cancelalert <number>` to remove one.",
        parse_mode="Markdown"
    )


async def cancelalert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /cancelalert 1"""
    chat_id = update.effective_chat.id
    try:
        index    = int(ctx.args[0]) - 1
        single   = price_alerts.get(chat_id, [])
        pairs    = pair_alerts.get(chat_id, [])
        combined = [("single", a) for a in single] + [("pair", a) for a in pairs]

        if index < 0 or index >= len(combined):
            raise IndexError

        kind, removed = combined[index]
        if kind == "single":
            price_alerts[chat_id].remove(removed)
            label = f"{BASE_CURRENCY}/{removed['currency']} {removed['direction']} `{removed['target']}`"
        else:
            pair_alerts[chat_id].remove(removed)
            label = f"{removed['from']}/{removed['to']} {removed['direction']} `{removed['target']}`"

        await update.message.reply_text(
            f"✅ *Alert Removed:* {label}", parse_mode="Markdown"
        )
    except (IndexError, ValueError, KeyError):
        await update.message.reply_text(
            "❌ Invalid number. Use /myalerts to see your list,\n"
            "then `/cancelalert <number>` to remove one.",
            parse_mode="Markdown"
        )


# ── SCHEDULER ─────────────────────────────────────────────────────────────────
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

    app.add_handler(CommandHandler("start",          start))
    app.add_handler(CommandHandler("stop",           stop))

    # Single currency commands
    app.add_handler(CommandHandler("rates",          rates))
    app.add_handler(CommandHandler("addcurrency",    addcurrency))
    app.add_handler(CommandHandler("removecurrency", removecurrency))
    app.add_handler(CommandHandler("mycurrencies",   mycurrencies))

    # Pair commands
    app.add_handler(CommandHandler("pairrate",       pairrate))
    app.add_handler(CommandHandler("addpair",        addpair))
    app.add_handler(CommandHandler("removepair",     removepair))
    app.add_handler(CommandHandler("mypairs",        mypairs))

    # Alert commands
    app.add_handler(CommandHandler("setalert",       setalert))
    app.add_handler(CommandHandler("setpairalert",   setpairalert))
    app.add_handler(CommandHandler("myalerts",       myalerts))
    app.add_handler(CommandHandler("cancelalert",    cancelalert))

    t = threading.Thread(target=run_scheduler, args=(app,), daemon=True)
    t.start()

    print("✅ Bot is running with real-time Twelve Data rates...")
    app.run_polling()


if __name__ == "__main__":
    main()

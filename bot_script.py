import os
import requests
import schedule
import time
import threading
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
BASE_CURRENCY    = "AUD"
WATCH_CURRENCIES = ["EUR", "GBP", "AUD", "JPY", "INR"]  # default currencies
ALERT_THRESHOLD  = 0.5   # % change to trigger fluctuation alert
CHECK_INTERVAL   = 60    # seconds between checks

# ── STATE ─────────────────────────────────────────────────────────────────────
previous_rates:   dict = {}   # { "USD": {"EUR": 0.91, ...}, "EUR": {"GBP": 0.85, ...} }
subscribed_users: set  = set()

# Per-user custom single currency watch lists (vs USD)
# { chat_id: {"CAD", "CHF", ...} }
user_currencies: dict = {}

# Per-user currency PAIR watch lists
# { chat_id: {("EUR", "GBP"), ("INR", "AED"), ...} }
user_pairs: dict = {}

# Price breach alerts for single currencies (vs USD)
# { chat_id: [{"currency": "EUR", "target": 0.95, "direction": "above"}, ...] }
price_alerts: dict = {}

# Price breach alerts for currency PAIRS
# { chat_id: [{"from": "EUR", "to": "GBP", "target": 0.85, "direction": "above"}, ...] }
pair_alerts: dict = {}


# ── FETCH HELPERS ─────────────────────────────────────────────────────────────
def fetch_rates_for_base(base: str, targets: list) -> dict:
    """Fetch rates: base → each target. Returns {target: rate}."""
    symbols = ",".join(t for t in targets if t != base)
    if not symbols:
        return {}
    url = f"https://api.frankfurter.app/latest?from={base}&to={symbols}"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()["rates"]


def get_pair_rate(from_cur: str, to_cur: str) -> float:
    """Get the live rate for any from_cur → to_cur pair."""
    if from_cur == to_cur:
        return 1.0
    url = f"https://api.frankfurter.app/latest?from={from_cur}&to={to_cur}"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()["rates"][to_cur]


def validate_currency(currency: str) -> bool:
    """Check if a currency code is valid via Frankfurter API."""
    try:
        url = f"https://api.frankfurter.app/latest?from=USD&to={currency}"
        resp = requests.get(url, timeout=10)
        return resp.status_code == 200
    except Exception:
        return False


def get_user_currencies(chat_id) -> list:
    """Get combined default + user-added single currencies."""
    custom = user_currencies.get(chat_id, set())
    return list(set(WATCH_CURRENCIES) | custom)


def pair_key(from_cur: str, to_cur: str) -> str:
    return f"{from_cur}/{to_cur}"


# ── BACKGROUND CHECK ──────────────────────────────────────────────────────────
async def check_and_alert(app):
    global previous_rates

    # ── Gather all bases needed ───────────────────────────────────────────
    # Single currency bases (always USD + any user pair bases)
    all_bases: dict = {}   # { base: set(targets) }

    # USD vs default + user currencies
    usd_targets = set(WATCH_CURRENCIES)
    for chat_id in subscribed_users:
        usd_targets |= user_currencies.get(chat_id, set())
    for chat_id in price_alerts:
        for a in price_alerts[chat_id]:
            usd_targets.add(a["currency"])
    all_bases[BASE_CURRENCY] = usd_targets

    # Pair bases
    all_pair_set = set()
    for chat_id in user_pairs:
        all_pair_set |= user_pairs[chat_id]
    for chat_id in pair_alerts:
        for a in pair_alerts[chat_id]:
            all_pair_set.add((a["from"], a["to"]))

    for (fc, tc) in all_pair_set:
        if fc not in all_bases:
            all_bases[fc] = set()
        all_bases[fc].add(tc)

    # ── Fetch all rates ───────────────────────────────────────────────────
    current_rates: dict = {}   # { base: {target: rate} }
    for base, targets in all_bases.items():
        targets_list = [t for t in targets if t != base]
        if not targets_list:
            continue
        try:
            rates = fetch_rates_for_base(base, targets_list)
            current_rates[base] = rates
        except Exception as e:
            print(f"Error fetching rates for {base}: {e}")

    if not current_rates:
        return

    # First run — store and return
    if not previous_rates:
        previous_rates = current_rates
        print("Initial rates loaded:", current_rates)
        return

    # ── 1. Single currency fluctuation alerts (vs USD) ────────────────────
    usd_current  = current_rates.get(BASE_CURRENCY, {})
    usd_previous = previous_rates.get(BASE_CURRENCY, {})

    for uid in subscribed_users.copy():
        messages = []
        for currency in get_user_currencies(uid):
            rate = usd_current.get(currency)
            old  = usd_previous.get(currency)
            if rate is None or old is None:
                continue
            change_pct = ((rate - old) / old) * 100
            if abs(change_pct) >= ALERT_THRESHOLD:
                arrow = "📈" if change_pct > 0 else "📉"
                dire  = "UP" if change_pct > 0 else "DOWN"
                messages.append(
                    f"{arrow} *{BASE_CURRENCY}/{currency}* went *{dire}*\n"
                    f"   {old:.4f} → {rate:.4f}  ({change_pct:+.2f}%)"
                )

        # Pair fluctuation alerts
        for (fc, tc) in user_pairs.get(uid, set()):
            rate = current_rates.get(fc, {}).get(tc)
            old  = previous_rates.get(fc, {}).get(tc)
            if rate is None or old is None:
                continue
            change_pct = ((rate - old) / old) * 100
            if abs(change_pct) >= ALERT_THRESHOLD:
                arrow = "📈" if change_pct > 0 else "📉"
                dire  = "UP" if change_pct > 0 else "DOWN"
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
            rate = usd_current.get(cur)
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

    # ── 3. Currency PAIR price breach alerts ──────────────────────────────
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
    """Send breach alert messages for triggered alerts."""
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
        f"📌 *Default watch (vs USD):* {', '.join(WATCH_CURRENCIES)}\n"
        f"⚡ Fluctuation alerts trigger at >{ALERT_THRESHOLD}% change\n\n"
        "📋 *All Commands:*\n\n"
        "━━ *Single Currency (vs USD)* ━━\n"
        "/rates               – Live rates vs USD\n"
        "/addcurrency XYZ     – Track a currency vs USD\n"
        "/removecurrency XYZ  – Stop tracking it\n"
        "/mycurrencies        – Your currency list\n\n"
        "━━ *Currency Pairs* ━━\n"
        "/pairrate EUR GBP    – Live rate for any pair\n"
        "/addpair EUR GBP     – Track EUR/GBP fluctuations\n"
        "/removepair EUR GBP  – Stop tracking a pair\n"
        "/mypairs             – Your tracked pairs\n\n"
        "━━ *Price Alerts* ━━\n"
        "/setalert EUR 0.95 above      – Alert when USD/EUR > 0.95\n"
        "/setpairalert EUR GBP 0.85 above – Alert when EUR/GBP > 0.85\n"
        "/myalerts            – All your active alerts\n"
        "/cancelalert 1       – Cancel alert by number\n\n"
        "━━ *Other* ━━\n"
        "/stop                – Unsubscribe from alerts",
        parse_mode="Markdown"
    )


async def stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subscribed_users.discard(update.effective_chat.id)
    await update.message.reply_text(
        "🔕 Unsubscribed. Send /start to re-subscribe anytime."
    )


# ── SINGLE CURRENCY COMMANDS ──────────────────────────────────────────────────

async def rates(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show live rates for user's currency list vs USD."""
    chat_id = update.effective_chat.id
    try:
        current = fetch_rates_for_base(BASE_CURRENCY, get_user_currencies(chat_id))
        lines = [
            f"*{BASE_CURRENCY}/{cur}:* `{rate:.4f}`"
            for cur, rate in sorted(current.items())
        ]
        await update.message.reply_text(
            "💱 *Your Rates vs USD*\n\n" + "\n".join(lines),
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def addcurrency(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage: /addcurrency CAD"""
    chat_id = update.effective_chat.id
    try:
        currency = ctx.args[0].upper()
        if currency in get_user_currencies(chat_id):
            await update.message.reply_text(
                f"ℹ️ *{BASE_CURRENCY}/{currency}* is already in your watch list!",
                parse_mode="Markdown"
            )
            return
        await update.message.reply_text(f"🔍 Validating *{currency}*...", parse_mode="Markdown")
        if not validate_currency(currency):
            await update.message.reply_text(
                f"❌ *{currency}* is not a valid currency code.\n"
                "Use 3-letter ISO codes: `CAD`, `CHF`, `CNY`, `SGD`, `AED`",
                parse_mode="Markdown"
            )
            return
        if chat_id not in user_currencies:
            user_currencies[chat_id] = set()
        user_currencies[chat_id].add(currency)
        rate = fetch_rates_for_base(BASE_CURRENCY, [currency]).get(currency)
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
    """Show user's currency watch list."""
    chat_id = update.effective_chat.id
    custom  = user_currencies.get(chat_id, set())
    default_line = "📌 *Default:* " + ", ".join(f"`{c}`" for c in sorted(WATCH_CURRENCIES))
    custom_line  = (
        "➕ *Added:* " + ", ".join(f"`{c}`" for c in sorted(custom))
        if custom else
        "➕ *Added:* None — use `/addcurrency XYZ`"
    )
    await update.message.reply_text(
        "💱 *Your Currency Watch List (vs USD)*\n\n" +
        default_line + "\n" + custom_line,
        parse_mode="Markdown"
    )


# ── CURRENCY PAIR COMMANDS ────────────────────────────────────────────────────

async def pairrate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Get live rate for any currency pair.
    Usage: /pairrate EUR GBP
           /pairrate INR AED
    """
    try:
        from_cur = ctx.args[0].upper()
        to_cur   = ctx.args[1].upper()
        await update.message.reply_text(
            f"🔍 Fetching *{from_cur}/{to_cur}* rate...", parse_mode="Markdown"
        )
        rate = get_pair_rate(from_cur, to_cur)
        await update.message.reply_text(
            f"💱 *{from_cur}/{to_cur}*\n\n"
            f"1 {from_cur} = `{rate:.4f}` {to_cur}\n\n"
            f"To track this pair: `/addpair {from_cur} {to_cur}`\n"
            f"To set an alert:    `/setpairalert {from_cur} {to_cur} {rate:.4f} above`",
            parse_mode="Markdown"
        )
    except (IndexError, KeyError):
        await update.message.reply_text(
            "❌ Usage: `/pairrate EUR GBP`\n"
            "Provide two valid 3-letter currency codes.",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Could not fetch rate. Check currency codes are valid.\nError: {e}"
        )


async def addpair(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Track fluctuations for a currency pair.
    Usage: /addpair EUR GBP
           /addpair INR AED
    """
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
            f"🔍 Validating *{from_cur}/{to_cur}*...", parse_mode="Markdown"
        )

        # Validate by fetching rate
        try:
            rate = get_pair_rate(from_cur, to_cur)
        except Exception:
            await update.message.reply_text(
                f"❌ Could not fetch *{from_cur}/{to_cur}*.\n"
                "Please check both currency codes are valid 3-letter ISO codes.",
                parse_mode="Markdown"
            )
            return

        if chat_id not in user_pairs:
            user_pairs[chat_id] = set()
        user_pairs[chat_id].add(pair)

        await update.message.reply_text(
            f"✅ *{from_cur}/{to_cur}* pair added!\n\n"
            f"💱 Current rate: 1 {from_cur} = `{rate:.4f}` {to_cur}\n\n"
            f"You'll get alerts when it moves >{ALERT_THRESHOLD}%.\n"
            f"Set a price target:\n"
            f"`/setpairalert {from_cur} {to_cur} {rate:.4f} above`\n"
            f"`/setpairalert {from_cur} {to_cur} {rate:.4f} below`",
            parse_mode="Markdown"
        )
    except IndexError:
        await update.message.reply_text(
            "❌ Usage: `/addpair EUR GBP`\n"
            "Provide two valid 3-letter currency codes.",
            parse_mode="Markdown"
        )


async def removepair(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Stop tracking a currency pair.
    Usage: /removepair EUR GBP
    """
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
    """Show all tracked currency pairs."""
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
    Set price alert for a single currency vs USD.
    Usage: /setalert EUR 0.95 above
           /setalert CAD 1.35 below
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
            if not validate_currency(currency):
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

        rate = fetch_rates_for_base(BASE_CURRENCY, [currency]).get(currency)
        symbol = "📈" if direction == "above" else "📉"
        await update.message.reply_text(
            f"{symbol} *Alert Set!*\n\n"
            f"Pair      : *{BASE_CURRENCY}/{currency}*\n"
            f"Trigger   : rate goes *{direction}* `{target}`\n"
            f"Current   : `{rate:.4f}`\n\n"
            "🚨 You'll be notified the moment it hits your target!",
            parse_mode="Markdown"
        )
    except (IndexError, ValueError):
        await update.message.reply_text(
            "❌ Usage:\n"
            "`/setalert EUR 0.95 above`\n"
            "`/setalert CAD 1.35 below`",
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

        # Validate pair by fetching rate
        try:
            rate = get_pair_rate(from_cur, to_cur)
        except Exception:
            await update.message.reply_text(
                f"❌ Could not validate *{from_cur}/{to_cur}*.\n"
                "Please check both currency codes are valid.",
                parse_mode="Markdown"
            )
            return

        # Auto-add pair to tracking list
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
            f"Pair      : *{from_cur}/{to_cur}*\n"
            f"Trigger   : rate goes *{direction}* `{target}`\n"
            f"Current   : 1 {from_cur} = `{rate:.4f}` {to_cur}\n\n"
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
    """Show all active alerts (single + pair)."""
    chat_id = update.effective_chat.id
    single  = price_alerts.get(chat_id, [])
    pairs   = pair_alerts.get(chat_id, [])

    if not single and not pairs:
        await update.message.reply_text(
            "You have no active alerts.\n\n"
            "Single currency: `/setalert EUR 0.95 above`\n"
            "Currency pair  : `/setpairalert EUR GBP 0.85 above`",
            parse_mode="Markdown"
        )
        return

    lines = []
    i = 1
    for a in single:
        symbol = "📈" if a["direction"] == "above" else "📉"
        lines.append(
            f"{i}. {symbol} *{BASE_CURRENCY}/{a['currency']}* "
            f"{a['direction']} `{a['target']}` _(vs USD)_"
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
    """
    Cancel any alert by its number from /myalerts.
    Usage: /cancelalert 1
    """
    chat_id = update.effective_chat.id
    try:
        index  = int(ctx.args[0]) - 1
        single = price_alerts.get(chat_id, [])
        pairs  = pair_alerts.get(chat_id, [])

        # Combined list mirrors /myalerts order
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

    print("✅ Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()

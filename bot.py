import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import jdatetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters,
)
from telegram.constants import ParseMode
from telegram.error import Forbidden, BadRequest

import database as db
import api_client
from scoring import calculate_points
from excel_export import generate_excel
from config import (
    TELEGRAM_BOT_TOKEN, POLL_INTERVAL_MINUTES,
    PTS_TOP_SCORER, PTS_CHAMPION,
)

logger = logging.getLogger(__name__)
PAGE_SIZE = 8

_FA_MONTHS = ["فروردین","اردیبهشت","خرداد","تیر","مرداد","شهریور",
              "مهر","آبان","آذر","دی","بهمن","اسفند"]

def _to_fa_digits(s: str) -> str:
    return s.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))

def _team(name: str, df: str = "fa") -> str:
    """Translate TBD and handle empty team names."""
    if not name or name.upper() == "TBD":
        return "نامشخص" if df == "fa" else "TBD"
    return name

_STAGE_FA = {
    "GROUP_STAGE":   "مرحله گروهی",
    "LAST_32":       "مرحله ۳۲ تیم",
    "LAST_16":       "یک‌هشتم نهایی",
    "QUARTER_FINALS":"ربع‌نهایی",
    "SEMI_FINALS":   "نیمه‌نهایی",
    "THIRD_PLACE":   "رده‌بندی سوم",
    "FINAL":         "فینال",
}
_STAGE_EN = {
    "GROUP_STAGE":   "Group Stage",
    "LAST_32":       "Round of 32",
    "LAST_16":       "Round of 16",
    "QUARTER_FINALS":"Quarter Final",
    "SEMI_FINALS":   "Semi Final",
    "THIRD_PLACE":   "3rd Place",
    "FINAL":         "Final",
}


# ═══════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════

def _is_locked(match) -> bool:
    dt = datetime.fromisoformat(match["match_date"])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= dt


def _group_settings(chat_id) -> dict:
    if not chat_id:
        return {"timezone": "Asia/Tehran", "date_format": "fa"}
    g = db.get_group(chat_id)
    return {
        "timezone":    (g["timezone"]    or "Asia/Tehran") if g else "Asia/Tehran",
        "date_format": (g["date_format"] or "fa")          if g else "fa",
    }


def _fmt_date(match, chat_id=None) -> str:
    s = _group_settings(chat_id)
    tz = ZoneInfo(s["timezone"])
    dt = datetime.fromisoformat(match["match_date"]).astimezone(tz)
    if s["date_format"] == "fa":
        jdt = jdatetime.datetime.fromgregorian(datetime=dt)
        # Persian digits ensure numbers flow RTL: ۲۱ خرداد ۲۱:۰۰
        day  = _to_fa_digits(str(jdt.day))
        time = _to_fa_digits(dt.strftime("%H:%M"))
        return f"{day} {_FA_MONTHS[jdt.month - 1]} {time}"
    return dt.strftime("%d %b %H:%M")


def _stage_label(match, date_format="fa") -> str:
    stage = match["stage"] or ""
    group = match["group_name"] or ""
    if stage == "GROUP_STAGE":
        letter = group.replace("GROUP_", "") if group else "؟"
        # RTL order: letter first so it sits on the right side
        return f"{letter} گروه" if date_format == "fa" else f"Group {letter}"
    if date_format == "fa":
        return _STAGE_FA.get(stage, stage)
    return _STAGE_EN.get(stage, stage)


def _ensure_registered(update: Update):
    user = update.effective_user
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        db.upsert_user(user.id, chat.id, user.username or "", user.first_name or "")


async def _try_dm(bot, user_id: int, text: str, **kwargs) -> bool:
    try:
        await bot.send_message(user_id, text, **kwargs)
        return True
    except (Forbidden, BadRequest):
        return False


async def _send_or_edit(update: Update, text: str, keyboard=None, **kwargs):
    """Send new message from command, or edit existing from callback."""
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=keyboard, **kwargs
        )
    else:
        await update.message.reply_text(text, reply_markup=keyboard, **kwargs)


def _active_group(ctx: ContextTypes.DEFAULT_TYPE):
    return ctx.user_data.get("active_group")


# ═══════════════════════════════════════════════════════════════════
#  GROUP SETUP KEYBOARD
# ═══════════════════════════════════════════════════════════════════

def _build_setup_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    s = _group_settings(chat_id)
    tz  = s["timezone"]
    df  = s["date_format"]

    def ck(condition): return "✅ " if condition else ""

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 زبان تاریخ:", callback_data="noop")],
        [
            InlineKeyboardButton(f"{ck(df=='fa')}شمسی (فارسی)", callback_data=f"gs:df:fa:{chat_id}"),
            InlineKeyboardButton(f"{ck(df=='en')}میلادی (English)", callback_data=f"gs:df:en:{chat_id}"),
        ],
        [InlineKeyboardButton("🕐 منطقه زمانی:", callback_data="noop")],
        [
            InlineKeyboardButton(f"{ck(tz=='Asia/Tehran')}تهران (UTC+3:30)", callback_data=f"gs:tz:ir:{chat_id}"),
            InlineKeyboardButton(f"{ck(tz=='Europe/Amsterdam')}آمستردام (UTC+2)", callback_data=f"gs:tz:nl:{chat_id}"),
        ],
        [InlineKeyboardButton("✅ ذخیره و نمایش دکمه ثبت‌نام", callback_data=f"gs:done:{chat_id}")],
    ])


# ═══════════════════════════════════════════════════════════════════
#  MAIN MENU
# ═══════════════════════════════════════════════════════════════════

async def _show_main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = _active_group(ctx)

    # Resolve group label
    if chat_id:
        group = db.get_group(chat_id)
        group_label = group["title"] if group else "گروه ناشناس"
    else:
        groups = db.get_user_groups(user_id)
        if not groups:
            await _send_or_edit(
                update,
                "👋 سلام!\n\n"
                "برای شروع باید توی یه گروه که این ربات اضافه شده عضو باشی "
                "و یه پیام توی اون گروه فرستاده باشی.\n\n"
                "بعد از اون برگرد اینجا و /start بزن.",
            )
            return
        if len(groups) == 1:
            chat_id = groups[0]["chat_id"]
            ctx.user_data["active_group"] = chat_id
            group_label = groups[0]["title"]
        else:
            # Multiple groups → show picker
            buttons = [
                [InlineKeyboardButton(g["title"], callback_data=f"grp:{g['chat_id']}")]
                for g in groups
            ]
            await _send_or_edit(
                update,
                "توی چند گروه عضوی. کدوم گروه رو می‌خوای؟",
                InlineKeyboardMarkup(buttons),
            )
            return

    is_admin = db.is_group_admin(user_id, chat_id)

    buttons = [
        [InlineKeyboardButton("⚽ پیش‌بینی بازی‌ها", callback_data="nav:matches")],
        [InlineKeyboardButton("📋 پیش‌بینی‌های من", callback_data="nav:mypreds")],
        [InlineKeyboardButton("🌟 پیش‌بینی ویژه", callback_data="nav:special")],
    ]
    if is_admin:
        buttons.append([InlineKeyboardButton("🏆 جدول امتیازات", callback_data="adm:lb")])
        buttons.append([
            InlineKeyboardButton("📊 Excel", callback_data="adm:xl"),
            InlineKeyboardButton("🔄 Sync", callback_data="adm:sync"),
        ])
        buttons.append([InlineKeyboardButton("🏅 اهدای امتیاز ویژه", callback_data="adm:award")])

    if len(db.get_user_groups(user_id)) > 1:
        buttons.append([InlineKeyboardButton(f"📍 گروه: {group_label}  (تغییر)", callback_data="nav:switchgroup")])
    else:
        buttons.append([InlineKeyboardButton(f"📍 {group_label}", callback_data="noop")])

    await _send_or_edit(
        update,
        "⚽ <b>ربات جام جهانی ۲۰۲۶</b>\n\nیه گزینه انتخاب کن:",
        InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )


# ═══════════════════════════════════════════════════════════════════
#  MATCH LIST
# ═══════════════════════════════════════════════════════════════════

def _build_matches_keyboard(matches, page: int, chat_id=None, back_cb="nav:main"):
    s = _group_settings(chat_id)
    start = page * PAGE_SIZE
    page_matches = matches[start: start + PAGE_SIZE]
    total_pages = (len(matches) + PAGE_SIZE - 1) // PAGE_SIZE

    buttons = []
    for m in page_matches:
        locked = "🔒" if _is_locked(m) else "🟢"
        df    = s["date_format"]
        stage = _stage_label(m, df)
        date  = _fmt_date(m, chat_id)
        home  = _team(m["home_team"], df)
        away  = _team(m["away_team"], df)
        label = f"{locked} {home} – {away}  |  {stage}  |  {date}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"ms:{m['id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ قبلی", callback_data=f"mp:{page - 1}"))
    if start + PAGE_SIZE < len(matches):
        nav.append(InlineKeyboardButton("بعدی ▶", callback_data=f"mp:{page + 1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("🔙 منوی اصلی", callback_data=back_cb)])

    text = (
        f"⚽ <b>بازی‌های پیش رو</b>  ({page + 1}/{total_pages})\n\n"
        f"روی بازی کلیک کن تا پیش‌بینی کنی.\n"
        f"<i>🟢 باز  |  🔒 قفل شده</i>"
    )
    return text, InlineKeyboardMarkup(buttons)


# ═══════════════════════════════════════════════════════════════════
#  COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type in ("group", "supergroup"):
        # Register the group (admin is whoever ran /start)
        db.upsert_group(chat.id, chat.title or "", user.id)
        await update.message.reply_text(
            "⚙️ <b>تنظیمات اولیه ربات</b>\n\n"
            "قبل از شروع، لطفاً زبان تاریخ و منطقه زمانی رو انتخاب کن:",
            reply_markup=_build_setup_keyboard(chat.id),
            parse_mode=ParseMode.HTML,
        )
        return

    # DM — check payload
    payload = ctx.args[0] if ctx.args else ""

    # Join a group: /start join_-123456
    if payload.startswith("join_"):
        try:
            group_chat_id = int(payload[5:])
        except ValueError:
            await _show_main_menu(update, ctx)
            return
        group = db.get_group(group_chat_id)
        if not group:
            await update.message.reply_text("این گروه هنوز توسط ادمین فعال نشده. از ادمین بخواه /start بزنه.")
            return
        # Check if already registered
        existing_groups = [g["chat_id"] for g in db.get_user_groups(user.id)]
        if group_chat_id in existing_groups:
            ctx.user_data["active_group"] = group_chat_id
            await update.message.reply_text(
                f"✅ قبلاً توی <b>{group['title']}</b> ثبت‌نام کردی!\n\nداری به منوی اصلی می‌ری…",
                parse_mode=ParseMode.HTML,
            )
            await _show_main_menu(update, ctx)
            return
        await update.message.reply_text(
            f"⚽ می‌خوای توی <b>{group['title']}</b> شرکت کنی و پیش‌بینی کنی؟",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ بله، ثبت‌نام می‌کنم", callback_data=f"join:{group_chat_id}"),
                InlineKeyboardButton("❌ نه", callback_data="nav:main"),
            ]]),
            parse_mode=ParseMode.HTML,
        )
        return

    # Deep link to a match: /start m42
    if payload.startswith("m") and payload[1:].isdigit():
        match_id = int(payload[1:])
        ctx.user_data["pending_match"] = match_id
        match = db.get_match(match_id)
        if match and not _is_locked(match):
            chat_id = _active_group(ctx)
            existing = db.get_prediction(user.id, chat_id, match_id) if chat_id else None
            existing_str = f"\n✏️ پیش‌بینی فعلی: <b>{existing['home_score']}-{existing['away_score']}</b>" if existing else ""
            await update.message.reply_text(
                f"⚽ <b>{match['home_team']} – {match['away_team']}</b>\n"
                f"📅 {_fmt_date(match)}"
                f"{existing_str}\n\n"
                "نتیجه‌ات رو بنویس (مثلاً <code>2-1</code>):",
                parse_mode=ParseMode.HTML,
            )
            return

    await _show_main_menu(update, ctx)


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await _redirect_to_dm(update, ctx)
        return
    await _show_main_menu(update, ctx)


async def _redirect_to_dm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bot_username = (await ctx.bot.get_me()).username
    await update.message.reply_text(
        f"👉 این کار رو توی پیام خصوصی انجام بده:\n@{bot_username}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("باز کردن پیام خصوصی", url=f"https://t.me/{bot_username}")
        ]]),
    )


# ═══════════════════════════════════════════════════════════════════
#  CALLBACK HANDLERS
# ═══════════════════════════════════════════════════════════════════

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id

    if data == "noop":
        await query.answer()
        return

    await query.answer()

    # ── Group setup (runs in group chat, admin only) ─────────────
    if data.startswith("gs:"):
        parts = data.split(":")          # gs : action : value : chat_id
        action = parts[1]
        value  = parts[2]
        gcid   = int(parts[3])
        group  = db.get_group(gcid)
        if not group or group["admin_id"] != user_id:
            await query.answer("⛔ فقط ادمین می‌تونه تنظیمات رو تغییر بده.", show_alert=True)
            return
        if action == "tz":
            tz_map = {"ir": "Asia/Tehran", "nl": "Europe/Amsterdam"}
            db.update_group_settings(gcid, timezone=tz_map[value])
        elif action == "df":
            db.update_group_settings(gcid, date_format=value)
        elif action == "done":
            bot_username = (await ctx.bot.get_me()).username
            await query.edit_message_text(
                "✅ <b>تنظیمات ذخیره شد!</b>\n\n"
                "اعضای گروه می‌تونن روی دکمه زیر کلیک کنن تا ثبت‌نام کنن:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "✅ ثبت‌نام و شروع بازی",
                        url=f"https://t.me/{bot_username}?start=join_{gcid}",
                    )
                ]]),
                parse_mode=ParseMode.HTML,
            )
            return
        # Refresh the setup keyboard with updated checkmarks
        await query.edit_message_reply_markup(_build_setup_keyboard(gcid))
        return

    # ── Join / registration ──────────────────────────────────────
    if data.startswith("join:"):
        group_chat_id = int(data.split(":")[1])
        group = db.get_group(group_chat_id)
        db.upsert_user(user_id, group_chat_id, update.effective_user.username or "", update.effective_user.first_name or "")
        ctx.user_data["active_group"] = group_chat_id
        group_title = group["title"] if group else "گروه"
        await query.edit_message_text(
            f"✅ <b>ثبت‌نام انجام شد!</b>\n\nخوش اومدی به <b>{group_title}</b>.\n\nحالا می‌تونی بازی‌ها رو پیش‌بینی کنی 🎉",
            parse_mode=ParseMode.HTML,
        )
        await _show_main_menu(update, ctx)
        return

    # ── Group selection ──────────────────────────────────────────
    if data.startswith("grp:"):
        chat_id = int(data.split(":")[1])
        ctx.user_data["active_group"] = chat_id
        await _show_main_menu(update, ctx)
        return

    if data == "nav:switchgroup":
        ctx.user_data.pop("active_group", None)
        await _show_main_menu(update, ctx)
        return

    # ── Main nav ─────────────────────────────────────────────────
    if data == "nav:main":
        await _show_main_menu(update, ctx)
        return

    # All actions below require an active group
    chat_id = _active_group(ctx)
    if not chat_id:
        await _show_main_menu(update, ctx)
        return

    # ── Matches list ─────────────────────────────────────────────
    if data in ("nav:matches", "mp:0") or data.startswith("mp:"):
        page = int(data.split(":")[1]) if data.startswith("mp:") else 0
        matches = db.get_upcoming_matches(limit=104)
        if not matches:
            await query.edit_message_text("هیچ بازی‌ای پیدا نشد.")
            return
        text, kb = _build_matches_keyboard(matches, page, chat_id=chat_id)
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    # ── Match selected ───────────────────────────────────────────
    if data.startswith("ms:"):
        match_id = int(data.split(":")[1])
        match = db.get_match(match_id)
        if not match:
            await query.answer("بازی پیدا نشد.", show_alert=True)
            return
        if _is_locked(match):
            await query.answer("🔒 این بازی قفل شده.", show_alert=True)
            return
        ctx.user_data["pending_match"] = match_id
        s    = _group_settings(chat_id)
        df   = s["date_format"]
        home = _team(match["home_team"], df)
        away = _team(match["away_team"], df)
        existing = db.get_prediction(user_id, chat_id, match_id)
        existing_str = f"\n✏️ پیش‌بینی فعلی: <b>{existing['home_score']}-{existing['away_score']}</b>" if existing else ""
        await query.edit_message_text(
            f"⚽ <b>{home} – {away}</b>\n"
            f"🏷 {_stage_label(match, df)}\n"
            f"📅 {_fmt_date(match, chat_id)}"
            f"{existing_str}\n\n"
            "نتیجه‌ات رو اینجا بنویس (مثلاً <code>2-1</code>):\n\n"
            "<i>برای برگشت /menu بزن</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    # ── My predictions ───────────────────────────────────────────
    if data == "nav:mypreds":
        preds = db.get_user_predictions(user_id, chat_id)
        if not preds:
            await query.edit_message_text(
                "هنوز هیچ پیش‌بینی‌ای ثبت نکردی.\n\n"
                "از /menu → پیش‌بینی بازی‌ها شروع کن.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 منوی اصلی", callback_data="nav:main")
                ]]),
            )
            return
        lines = ["📋 <b>پیش‌بینی‌های تو</b>\n"]
        for p in preds:
            pred = f"{p['home_score']}-{p['away_score']}"
            if p["status"] == "FINISHED":
                actual = f"{p['actual_home']}-{p['actual_away']}"
                pts = p["points"] if p["points"] is not None else "…"
                icon = "✅" if (p["points"] or 0) > 0 else "❌"
                lines.append(f"{icon} {p['home_team']} – {p['away_team']}\n   تو: {pred}  |  واقعی: {actual}  |  <b>{pts} امتیاز</b>")
            else:
                lines.append(f"⏳ {p['home_team']} – {p['away_team']}: {pred}")
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 منوی اصلی", callback_data="nav:main")
            ]]),
            parse_mode=ParseMode.HTML,
        )
        return

    # ── Special predictions ──────────────────────────────────────
    if data == "nav:special":
        max_md = db.get_group_stage_max_matchday()
        ts_locked = max_md >= 3
        ch_locked = max_md >= 2
        ts = db.get_special(user_id, chat_id, "top_scorer")
        ch = db.get_special(user_id, chat_id, "champion")
        ts_val = f"✅ {ts['pred_value']}" if ts else "—"
        ch_val = f"✅ {ch['pred_value']}" if ch else "—"
        buttons = []
        if not ts_locked:
            buttons.append([InlineKeyboardButton(f"⚽ بهترین گلزن: {ts_val}", callback_data="sp:ts")])
        if not ch_locked:
            buttons.append([InlineKeyboardButton(f"🏆 قهرمان جام: {ch_val}", callback_data="sp:ch")])
        buttons.append([InlineKeyboardButton("🔙 منوی اصلی", callback_data="nav:main")])
        locked_note = ""
        if ts_locked:
            locked_note += "\n🔒 پیش‌بینی گلزن قفل شده"
        if ch_locked:
            locked_note += "\n🔒 پیش‌بینی قهرمان قفل شده"
        await query.edit_message_text(
            f"🌟 <b>پیش‌بینی‌های ویژه</b>\n"
            f"گلزن: {ts_val}  (۱۰ امتیاز)\n"
            f"قهرمان: {ch_val}  (۲۰ امتیاز)"
            f"{locked_note}\n\n"
            "برای تغییر روی دکمه کلیک کن:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "sp:ts":
        ctx.user_data["pending_special"] = "top_scorer"
        await query.edit_message_text(
            "⚽ <b>بهترین گلزن جام</b>\n\n"
            "اسم بازیکن رو بنویس (به انگلیسی مثل API):\n"
            "مثلاً: <code>Kylian Mbappe</code>\n\n"
            "<i>برای برگشت /menu بزن</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "sp:ch":
        ctx.user_data["pending_special"] = "champion"
        await query.edit_message_text(
            "🏆 <b>قهرمان جام جهانی</b>\n\n"
            "اسم تیم رو بنویس (به انگلیسی مثل API):\n"
            "مثلاً: <code>Brazil</code>\n\n"
            "<i>برای برگشت /menu بزن</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    # ── Admin actions ────────────────────────────────────────────
    if not db.is_group_admin(user_id, chat_id):
        await query.answer("⛔ فقط ادمین.", show_alert=True)
        return

    if data == "adm:lb":
        rows = db.get_leaderboard(chat_id)
        if not rows:
            text = "هنوز هیچ امتیازی ثبت نشده."
        else:
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            lines = ["🏆 <b>جدول امتیازات</b>\n"]
            for i, r in enumerate(rows, 1):
                name = r["first_name"] or r["username"] or "؟"
                lines.append(f"{medals.get(i, str(i)+'.') }  <b>{name}</b> — {r['total_points']} امتیاز  ({r['matches_scored']} بازی)")
            text = "\n".join(lines)
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 منوی اصلی", callback_data="nav:main")
            ]]),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "adm:xl":
        await query.answer("در حال تهیه فایل…")
        path = generate_excel(chat_id)
        await ctx.bot.send_document(
            user_id,
            document=open(path, "rb"),
            filename=f"wc2026_{chat_id}.xlsx",
            caption="📊 گزارش کامل پیش‌بینی‌ها",
        )
        return

    if data == "adm:sync":
        count = await _sync_matches_from_api()
        await query.edit_message_text(
            f"✅ {count} بازی از API به‌روز شد.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 منوی اصلی", callback_data="nav:main")
            ]]),
        )
        return

    if data == "adm:award":
        await query.edit_message_text(
            "🏅 <b>اهدای امتیاز ویژه</b>\n\n"
            "برای اهدای امتیاز گلزن:\n"
            "<code>/awardtopscorer نام‌بازیکن</code>\n\n"
            "برای اهدای امتیاز قهرمان:\n"
            "<code>/awardchampion نام‌تیم</code>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 منوی اصلی", callback_data="nav:main")
            ]]),
            parse_mode=ParseMode.HTML,
        )
        return


# ═══════════════════════════════════════════════════════════════════
#  PRIVATE MESSAGE HANDLER  (score + special prediction input)
# ═══════════════════════════════════════════════════════════════════

async def on_private_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    chat_id = _active_group(ctx)

    # ── Special prediction input ─────────────────────────────────
    pending_special = ctx.user_data.get("pending_special")
    if pending_special:
        if not chat_id:
            await update.message.reply_text("گروه فعال پیدا نشد. /menu بزن.")
            return
        ctx.user_data.pop("pending_special")
        db.upsert_special(user_id, chat_id, pending_special, text)
        label = "گلزن" if pending_special == "top_scorer" else "قهرمان"
        pts = PTS_TOP_SCORER if pending_special == "top_scorer" else PTS_CHAMPION
        await update.message.reply_text(
            f"✅ پیش‌بینی {label} ثبت شد: <b>{text}</b>\n({pts} امتیاز در صورت درست بودن)\n\n/menu",
            parse_mode=ParseMode.HTML,
        )
        return

    # ── Score input ───────────────────────────────────────────────
    pending_match = ctx.user_data.get("pending_match")
    if pending_match:
        score_match = re.match(r"^(\d+)\s*[-:]\s*(\d+)$", text)
        if not score_match:
            await update.message.reply_text(
                "فرمت درست نیست.\nمثال: <code>2-1</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        if not chat_id:
            await update.message.reply_text("گروه فعال پیدا نشد. /menu بزن.")
            return
        match = db.get_match(pending_match)
        if not match or _is_locked(match):
            ctx.user_data.pop("pending_match", None)
            await update.message.reply_text("🔒 این بازی قفل شده. /menu بزن و یه بازی دیگه انتخاب کن.")
            return
        pred_home = int(score_match.group(1))
        pred_away = int(score_match.group(2))
        existing = db.get_prediction(user_id, chat_id, pending_match)
        db.upsert_prediction(user_id, chat_id, pending_match, pred_home, pred_away)
        ctx.user_data.pop("pending_match")
        action = "به‌روز شد" if existing else "ثبت شد"
        await update.message.reply_text(
            f"✅ پیش‌بینی {action}!\n\n"
            f"<b>{match['home_team']}</b>  {pred_home} – {pred_away}  <b>{match['away_team']}</b>\n\n"
            "برای پیش‌بینی بازی دیگه: /menu",
            parse_mode=ParseMode.HTML,
        )
        return

    # No pending action — show menu
    await _show_main_menu(update, ctx)


# ═══════════════════════════════════════════════════════════════════
#  GROUP MESSAGE HANDLER  (register users silently)
# ═══════════════════════════════════════════════════════════════════

async def on_group_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pass  # Group messages are not processed — registration is via DM only


async def on_group_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Redirect any command used in group to DM."""
    await _redirect_to_dm(update, ctx)


# ═══════════════════════════════════════════════════════════════════
#  ADMIN TEXT COMMANDS  (used in DM by admin)
# ═══════════════════════════════════════════════════════════════════

async def cmd_awardtopscorer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = _active_group(ctx)
    if not chat_id or not db.is_group_admin(user_id, chat_id):
        await update.message.reply_text("⛔ فقط ادمین گروه.")
        return
    if not ctx.args:
        await update.message.reply_text("استفاده: /awardtopscorer نام‌بازیکن")
        return
    winner = " ".join(ctx.args).strip().lower()
    specials = db.get_all_specials(chat_id, "top_scorer")
    awarded = []
    for s in specials:
        if s["pred_value"].strip().lower() == winner:
            db.set_special_points(s["user_id"], chat_id, "top_scorer", PTS_TOP_SCORER)
            awarded.append(s["first_name"])
    if awarded:
        await update.message.reply_text(f"🏅 امتیاز گلزن داده شد به: {', '.join(awarded)}")
    else:
        await update.message.reply_text("هیچ‌کس این بازیکن رو پیش‌بینی نکرده بود.")


async def cmd_awardchampion(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = _active_group(ctx)
    if not chat_id or not db.is_group_admin(user_id, chat_id):
        await update.message.reply_text("⛔ فقط ادمین گروه.")
        return
    if not ctx.args:
        await update.message.reply_text("استفاده: /awardchampion نام‌تیم")
        return
    winner = " ".join(ctx.args).strip().lower()
    specials = db.get_all_specials(chat_id, "champion")
    awarded = []
    for s in specials:
        if s["pred_value"].strip().lower() == winner:
            db.set_special_points(s["user_id"], chat_id, "champion", PTS_CHAMPION)
            awarded.append(s["first_name"])
    if awarded:
        await update.message.reply_text(f"🏆 امتیاز قهرمان داده شد به: {', '.join(awarded)}")
    else:
        await update.message.reply_text("هیچ‌کس این تیم رو پیش‌بینی نکرده بود.")


# ═══════════════════════════════════════════════════════════════════
#  BACKGROUND JOBS
# ═══════════════════════════════════════════════════════════════════

async def _sync_matches_from_api() -> int:
    raw = api_client.fetch_matches()
    for m_raw in raw:
        m = api_client.parse_match(m_raw)
        db.upsert_match(
            m["api_id"], m["home"], m["away"], m["date"],
            m["stage"], m["group"], m["matchday"],
            m["status"], m["home_score"], m["away_score"],
        )
    return len(raw)


async def job_sync_and_score(ctx: ContextTypes.DEFAULT_TYPE):
    logger.info("Running periodic sync & score job")
    await _sync_matches_from_api()

    # Score finished matches
    for match in db.get_newly_finished_unscored():
        if match["home_score"] is None:
            continue
        for gid in db.get_all_group_ids():
            preds = db.get_predictions_for_match(match["id"], gid)
            if not preds:
                continue
            group_lines = [
                f"📊 <b>{match['home_team']} {match['home_score']} – {match['away_score']} {match['away_team']}</b>\n"
            ]
            for p in preds:
                pts = calculate_points(
                    p["home_score"], p["away_score"],
                    match["home_score"], match["away_score"],
                )
                db.set_prediction_points(p["user_id"], gid, match["id"], pts)
                name = p["first_name"] or p["username"] or "؟"
                icon = "✅" if pts > 0 else "❌"
                group_lines.append(f"{icon} {name}: {p['home_score']}-{p['away_score']} ← <b>{pts} امتیاز</b>")
                # Personal DM
                await _try_dm(
                    ctx.bot, p["user_id"],
                    f"📊 <b>نتیجه بازی</b>\n"
                    f"{match['home_team']} {match['home_score']} – {match['away_score']} {match['away_team']}\n\n"
                    f"پیش‌بینی تو: {p['home_score']}-{p['away_score']}\n"
                    f"{'✅' if pts > 0 else '❌'} <b>{pts} امتیاز</b>",
                    parse_mode=ParseMode.HTML,
                )
            try:
                await ctx.bot.send_message(gid, "\n".join(group_lines), parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.error("Results post failed for %s: %s", gid, e)

    # 24h reminders with deep-link button to DM
    now = datetime.now(timezone.utc)
    bot_username = (await ctx.bot.get_me()).username
    for m in db.get_upcoming_matches(limit=50):
        dt = datetime.fromisoformat(m["match_date"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if not (0 < (dt - now).total_seconds() / 3600 <= 24):
            continue
        for gid in db.get_all_group_ids():
            if db.was_notified(m["id"], gid):
                continue
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "⚽ پیش‌بینی کن",
                    url=f"https://t.me/{bot_username}?start=m{m['id']}",
                )
            ]])
            s  = _group_settings(gid)
            df = s["date_format"]
            try:
                await ctx.bot.send_message(
                    gid,
                    f"🔔 <b>۲۴ ساعت تا بازی!</b>\n\n"
                    f"<b>{_team(m['home_team'], df)} – {_team(m['away_team'], df)}</b>\n"
                    f"🏷 {_stage_label(m, df)}\n"
                    f"📅 {_fmt_date(m, gid)}\n\n"
                    "برای پیش‌بینی روی دکمه کلیک کن 👇",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML,
                )
                db.mark_notified(m["id"], gid)
            except Exception as e:
                logger.error("Reminder failed for %s match %s: %s", gid, m["id"], e)


# ═══════════════════════════════════════════════════════════════════
#  APPLICATION BUILDER
# ═══════════════════════════════════════════════════════════════════

def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # DM commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("awardtopscorer", cmd_awardtopscorer))
    app.add_handler(CommandHandler("awardchampion", cmd_awardchampion))

    # All callbacks (single handler, routed internally)
    app.add_handler(CallbackQueryHandler(on_callback))

    # Private text → score/special input or show menu
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        on_private_message,
    ))

    # Group: register users silently
    app.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        on_group_message,
    ), group=1)

    # Group commands (non-/start) → redirect to DM
    app.add_handler(MessageHandler(
        filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
        on_group_command,
    ), group=2)

    app.job_queue.run_repeating(
        job_sync_and_score,
        interval=POLL_INTERVAL_MINUTES * 60,
        first=10,
        name="sync_and_score",
    )

    return app

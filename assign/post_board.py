#!/usr/bin/env python3
"""
post_board.py
==============
Post Board — a shared drop box for reference numbers and photos (📥 Post
Board, /postboard). Anyone with 📋 Assignments menu access can both post
into it and check it — there's no separate poster/checker permission
split, matching how every other menu category works (see common.py's
per-user category access, which gates this whole category, not
individual buttons within it).

Posting: tap ➕ Post New Item(s), then send any mix of text messages
(each split via common._parse_list_input into one or more reference
numbers, one queue item per ref) and photos (one queue item per photo,
its caption if any carried along) across as many messages as needed, in
one continuous posting session. Tap ✅ Done Posting to exit back to the
menu.

Checking: tap 📋 View Queue to see every pending item, oldest first, each
as its own Telegram message (photos have to be — Telegram can't embed an
image inside a text block — so text ref items get the same one-message-
per-item treatment for consistency) with a ✅ Done button attached
directly to that message.

Done is deliberately not a match/verification step — tapping it doesn't
check the ref against DLV, an assignment, or anything else; it's a pure
"I've dealt with this, clear it" gesture, and removes the item from the
shared queue immediately. The ✅ Done CallbackQueryHandler is registered
as a standalone handler (not nested in the posting/menu ConversationHandler
states) since a checker can tap Done on an old queue message long after
whatever conversation showed it to them has ended.

Every item is tagged with who posted it (common.load_user_names' cached
display name, falling back to the raw Telegram ID) so a checker has
context, even though the queue itself isn't filterable by poster — it's
one flat shared list for everyone with access.

Call register(app) from bot.py's main() to wire this feature in.
"""

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from typing import Dict, List, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from common import (
    BTN_POST_BOARD,
    DATA_DIR,
    _atomic_json_write,
    _CANCEL_FILTER,
    _ensure_data_dir,
    _main_menu,
    _parse_list_input,
    allowed,
    cmd_cancel,
    deny,
    fallback,
    load_user_names,
    logger,
    md_escape,
    not_cancel,
)

SAVED_POST_BOARD_FILE = os.path.join(DATA_DIR, "saved_post_board.json")

# Reply-keyboard button shown only during an active posting session —
# distinct text from every ref/caption a poster would plausibly type, so
# it's unambiguous as an exit signal rather than more content to queue.
BTN_DONE_POSTING = "✅ Done Posting"


# ──────────────────────────────────────────────────────────
# States — Post Board conversation
# ──────────────────────────────────────────────────────────
class PB(Enum):
    MENU    = auto()   # ➕ Post New Item(s) / 📋 View Queue / 🛑 Close
    POSTING = auto()   # accepting text refs and/or photos until Done Posting


@dataclass
class PBSession:
    posted_count: int = 0   # items added so far in the current posting session


def _get_pb_sess(ctx: ContextTypes.DEFAULT_TYPE) -> PBSession:
    """Fetch (creating if absent) this chat's Post Board session, holding
    the running count of items added during the current posting session
    (reset each time posting starts, reported back when it ends)."""
    if "pb_session" not in ctx.user_data:
        ctx.user_data["pb_session"] = PBSession()
    return ctx.user_data["pb_session"]


# ──────────────────────────────────────────────────────────
# Persistence — saved_post_board.json
# ──────────────────────────────────────────────────────────

def load_post_board() -> List[Dict]:
    """Every pending (not yet marked Done) queue item: [{id, type
    ("text"|"photo"), text (ref number, text items only), photo_file_id
    (photo items only), caption (photo items only, optional),
    posted_by, posted_by_name, posted_at}, ...]. Done items are removed
    outright (see remove_post_item), not archived — Done is a pure
    "clear it" action, no history is kept."""
    try:
        with open(SAVED_POST_BOARD_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_post_board(items: List[Dict]) -> None:
    """Persist the full pending-item list, overwriting the file."""
    _ensure_data_dir()
    _atomic_json_write(SAVED_POST_BOARD_FILE, items, indent=2)


def add_post_item(item: Dict) -> str:
    """Assign a new id to item, append it to the saved queue, and return
    the id."""
    item_id = str(uuid.uuid4())[:8]
    items = load_post_board()
    items.append({**item, "id": item_id})
    save_post_board(items)
    return item_id


def get_post_item(item_id: str) -> Optional[Dict]:
    """Look up a single pending item by id, or None if it's already been
    cleared (Done) or never existed."""
    return next((i for i in load_post_board() if i.get("id") == item_id), None)


def remove_post_item(item_id: str) -> bool:
    """Delete an item by id (the Done action). Returns False if no item
    had that id — e.g. two checkers tapped Done on the same item at
    nearly the same time, and the second tap has nothing left to remove."""
    items = load_post_board()
    remaining = [i for i in items if i.get("id") != item_id]
    if len(remaining) == len(items):
        return False
    save_post_board(remaining)
    return True


# ──────────────────────────────────────────────────────────
# Keyboards
# ──────────────────────────────────────────────────────────

def _pb_menu_keyboard() -> InlineKeyboardMarkup:
    """➕ Post New Item(s) / 📋 View Queue / 🛑 Close."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Post New Item(s)", callback_data="pb_menu:post")],
        [InlineKeyboardButton("📋 View Queue", callback_data="pb_menu:view")],
        [InlineKeyboardButton("🛑 Close", callback_data="pb_menu:close")],
    ])


def _pb_posting_keyboard() -> ReplyKeyboardMarkup:
    """The reply keyboard shown while a posting session is active — just
    Done Posting, so a poster's actual ref/caption text never collides
    with a menu button."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_DONE_POSTING)]], resize_keyboard=True, is_persistent=True,
    )


def _pb_done_keyboard(item_id: str) -> InlineKeyboardMarkup:
    """The single ✅ Done button attached to one queue item's message."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done", callback_data=f"pb_done:{item_id}")]])


# ──────────────────────────────────────────────────────────
# Setup / menu conversation
# ──────────────────────────────────────────────────────────

def _pb_poster_label(item: Dict) -> str:
    """"Jane Doe" (or the raw id if no cached name) for an item's poster —
    shared by the posting-session confirmation and the queue view."""
    return item.get("posted_by_name") or str(item.get("posted_by", "—"))


async def cmd_post_board(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Entry point (/postboard or the 📥 Post Board button) — shows the
    # current pending count and the Post/View/Close menu.
    if not allowed(update): return await deny(update)
    pending = len(load_post_board())
    await update.message.reply_text(
        "📥 *Post Board*\n\n"
        "A shared drop box for reference numbers and photos — post items "
        "here, anyone with access can check them and clear (✅ Done) "
        "whatever they've dealt with. Done doesn't verify anything, it "
        "just removes the item from the queue.\n\n"
        f"*{pending}* item(s) currently pending.",
        parse_mode="Markdown",
        reply_markup=_pb_menu_keyboard(),
    )
    return PB.MENU


async def recv_pb_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Handle the Post/View/Close choice.
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]

    if action == "close":
        await query.edit_message_reply_markup(reply_markup=None)
        return ConversationHandler.END

    if action == "view":
        await _pb_send_queue(query.message.chat_id, ctx)
        await query.edit_message_reply_markup(reply_markup=None)
        return ConversationHandler.END

    # action == "post"
    _get_pb_sess(ctx).posted_count = 0
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        "➕ *Posting mode*\n\n"
        "Send reference numbers (one or more per message, one per line or "
        "comma-separated) and/or photos — as many messages as you like. "
        "Tap ✅ Done Posting when finished.",
        parse_mode="Markdown",
        reply_markup=_pb_posting_keyboard(),
    )
    return PB.POSTING


async def _pb_send_queue(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Render every pending item as its own message (photo items via
    send_photo, text items via send_message), each with its own ✅ Done
    button, oldest first. A photo item's message carries the same poster/
    posted-at context a text item's does, via its caption."""
    items = load_post_board()
    if not items:
        await ctx.bot.send_message(chat_id, "✅ No pending items.", reply_markup=_main_menu())
        return

    for item in items:
        poster = md_escape(_pb_poster_label(item))
        when = item.get("posted_at", "—")
        if item.get("type") == "photo":
            caption_line = f"\n{md_escape(item['caption'])}" if item.get("caption") else ""
            await ctx.bot.send_photo(
                chat_id,
                photo=item["photo_file_id"],
                caption=f"📸 Posted by *{poster}* — `{when}`{caption_line}",
                parse_mode="Markdown",
                reply_markup=_pb_done_keyboard(item["id"]),
            )
        else:
            await ctx.bot.send_message(
                chat_id,
                f"🔖 *{md_escape(item.get('text', '—'))}*\nPosted by *{poster}* — `{when}`",
                parse_mode="Markdown",
                reply_markup=_pb_done_keyboard(item["id"]),
            )
    await ctx.bot.send_message(chat_id, f"— {len(items)} item(s) total —", reply_markup=_main_menu())


# ──────────────────────────────────────────────────────────
# Posting session
# ──────────────────────────────────────────────────────────

def _pb_new_item_base(update: Update) -> Dict:
    """The poster/posted-at fields shared by both a text item and a photo
    item, built fresh from the current update."""
    user = update.effective_user
    names = load_user_names()
    return {
        "posted_by":      user.id,
        "posted_by_name": names.get(str(user.id), ""),
        "posted_at":      datetime.now().isoformat(timespec="seconds"),
    }


async def recv_pb_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # One message can contain multiple ref numbers (one per line or
    # comma-separated) — each becomes its own queue item.
    if not allowed(update): return await deny(update)
    refs = _parse_list_input(update.message.text or "")
    if not refs:
        await update.message.reply_text("Send a reference number, or a photo.")
        return PB.POSTING

    base = _pb_new_item_base(update)
    for ref in refs:
        add_post_item({**base, "type": "text", "text": ref})

    sess = _get_pb_sess(ctx)
    sess.posted_count += len(refs)
    await update.message.reply_text(
        f"✅ Added {len(refs)} item(s) — {sess.posted_count} this session.",
    )
    return PB.POSTING


async def recv_pb_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # A photo message — Telegram sends multiple resolutions, the last is
    # the largest. Its caption (if any) rides along with the queue item.
    if not allowed(update): return await deny(update)
    base = _pb_new_item_base(update)
    add_post_item({
        **base,
        "type":          "photo",
        "photo_file_id": update.message.photo[-1].file_id,
        "caption":       update.message.caption or "",
    })

    sess = _get_pb_sess(ctx)
    sess.posted_count += 1
    await update.message.reply_text(f"✅ Photo added — {sess.posted_count} item(s) this session.")
    return PB.POSTING


async def recv_pb_done_posting(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Exit posting mode, reporting how many items were added this session.
    if not allowed(update): return await deny(update)
    sess = _get_pb_sess(ctx)
    await update.message.reply_text(
        f"✅ Posting finished — {sess.posted_count} item(s) added.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text("Main menu.", reply_markup=_main_menu())
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────
# Done — standalone, works outside any active conversation
# ──────────────────────────────────────────────────────────

async def recv_pb_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Clear one item from the queue. Not tied to any conversation state —
    # a checker can tap Done on a queue message sent long ago, well after
    # whatever showed it to them has ended. Purely removes the item; it
    # doesn't verify the ref/photo against anything else.
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    item_id = query.data.split(":", 1)[1]

    removed = remove_post_item(item_id)
    if not removed:
        await query.edit_message_reply_markup(reply_markup=None)
        return

    who = _pb_poster_label({"posted_by_name": load_user_names().get(str(query.from_user.id), ""),
                             "posted_by": query.from_user.id})
    logger.info("Post Board: item %s cleared by %s", item_id, who)

    # No parse_mode on either edit — query.message.text/.caption comes
    # back from Telegram with the original send's Markdown *formatting*
    # stripped (entities are stored separately, not as literal
    # characters), but any md_escape()'d special character from the
    # original send (e.g. a ref number's underscore) comes back as its
    # bare, unescaped self. Re-parsing that as Markdown here would risk
    # exactly the "Can't parse entities" crash md_escape exists to
    # prevent, so the edit is plain text — a harmless drop of the
    # original bold/code styling on an item that's now done anyway.
    if query.message.photo:
        await query.edit_message_caption(
            caption=(query.message.caption or "") + f"\n\n✅ Done — cleared by {who}",
        )
    else:
        await query.edit_message_text(
            (query.message.text or "") + f"\n\n✅ Done — cleared by {who}",
        )


# ──────────────────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the Post Board conversation, plus the standalone ✅ Done
    handler, into the given Application."""
    pb_conv = ConversationHandler(
        entry_points=[
            CommandHandler("postboard", cmd_post_board),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_POST_BOARD)}$"), cmd_post_board),
        ],
        states={
            PB.MENU: [CallbackQueryHandler(recv_pb_menu, pattern=r"^pb_menu:")],
            PB.POSTING: [
                MessageHandler(filters.Regex(f"^{re.escape(BTN_DONE_POSTING)}$"), recv_pb_done_posting),
                MessageHandler(filters.PHOTO, recv_pb_photo),
                MessageHandler(not_cancel, recv_pb_text),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            MessageHandler(_CANCEL_FILTER, cmd_cancel),
            MessageHandler(filters.TEXT, fallback),
        ],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(pb_conv)
    app.add_handler(CallbackQueryHandler(recv_pb_done, pattern=r"^pb_done:"))

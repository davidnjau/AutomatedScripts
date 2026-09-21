#!/usr/bin/env python3
"""
post_board.py
==============
Post Board — a shared drop box for reference numbers and photos, its own
menu category (📥 Post Board) rather than a member of another one, so
it's independently grantable via access_control.py's Manage Access.
Anyone with 📥 Post Board access can both post into it and check it —
there's no separate poster/checker permission split.

Posting (➕ Post New Item(s), /postboard): send any mix of text messages
(each split via common._parse_list_input into one or more reference
numbers, one queue item per ref) and photos (one queue item per photo,
its caption if any carried along) across as many messages as needed, in
one continuous posting session. Tap ✅ Done Posting to exit.

The moment an item is added, it's broadcast immediately to every other
user who currently has 📥 Post Board access (common.ALLOWED_IDS filtered
by common.category_allowed) — each as its own message (photos have to
be, since Telegram can't embed an image inside a text block, so text
items get the same one-message-per-item treatment) with a ✅ Done button.
The poster themselves isn't re-notified about their own post (they
already know). Checking (📋 View Queue, /postboardqueue) delivers every
still-pending item to whoever asked, the same way — a catch-up path for
someone who joined access later or wants a refresher, complementing the
push rather than replacing it.

Every delivery of an item (whether the original broadcast or a later
View Queue catch-up) records its (chat_id, message_id) on the item
(add_post_item's "broadcasts" list, grown by append_post_item_broadcast)
so that when *any* recipient taps ✅ Done, recv_pb_done can go back and
edit *every* copy of that item — not just the one that was tapped — to
show it's been cleared, everywhere it was ever delivered.

Done is deliberately not a match/verification step — tapping it doesn't
check the ref against DLV, an assignment, or anything else; it's a pure
"I've dealt with this, clear it" gesture, and removes the item from the
shared queue immediately (no archive, no history kept). The ✅ Done
CallbackQueryHandler is registered as a standalone handler (not nested
in the posting ConversationHandler's states) since a checker can tap
Done on a queue message long after whatever sent it to them has ended.

_pb_render_text/_pb_render_caption build each item's display text fresh
from its own stored fields every time it's rendered (initial delivery or
a later Done-edit) rather than ever reading back Telegram's own
already-sent message content — Telegram strips Markdown syntax down to
plain text + separate formatting entities once a message is sent, so
re-parsing retrieved message text as Markdown a second time would risk
exactly the "Can't parse entities" crash common.md_escape exists to
prevent (a ref number's escaped underscore would come back bare).
Building fresh from stored fields sidesteps that entirely, safely, on
every edit — not just the first send.

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
    ALLOWED_IDS,
    BTN_CAT_POST_BOARD,
    BTN_PB_POST,
    BTN_PB_VIEW,
    DATA_DIR,
    _atomic_json_write,
    _CANCEL_FILTER,
    _ensure_data_dir,
    _main_menu,
    _parse_list_input,
    allowed,
    category_allowed,
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
# States — posting session
# ──────────────────────────────────────────────────────────
class PB(Enum):
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
    posted_by, posted_by_name, posted_at, broadcasts: [{chat_id,
    message_id}, ...]}, ...]. broadcasts accumulates one entry per
    delivery (the initial push, plus any later View Queue catch-up) so
    Done can find and clear every copy. Done items are removed outright
    (see remove_post_item), not archived — Done is a pure "clear it"
    action, no history is kept."""
    try:
        with open(SAVED_POST_BOARD_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_post_board(items: List[Dict]) -> None:
    """Persist the full pending-item list, overwriting the file."""
    _ensure_data_dir()
    _atomic_json_write(SAVED_POST_BOARD_FILE, items, indent=2)


def add_post_item(item: Dict) -> Dict:
    """Assign a new id and an empty broadcasts list to item, append it to
    the saved queue, and return the full stored item (including id)."""
    stored = {**item, "id": str(uuid.uuid4())[:8], "broadcasts": []}
    items = load_post_board()
    items.append(stored)
    save_post_board(items)
    return stored


def get_post_item(item_id: str) -> Optional[Dict]:
    """Look up a single pending item by id, or None if it's already been
    cleared (Done) or never existed."""
    return next((i for i in load_post_board() if i.get("id") == item_id), None)


def append_post_item_broadcast(item_id: str, chat_id: int, message_id: int) -> None:
    """Record one more (chat_id, message_id) delivery of item_id — called
    after every successful send, whether the initial push broadcast or a
    later View Queue catch-up, so a later Done can find and clear every
    copy of the item, not just the one that was tapped. A no-op if the
    item's already been cleared since the send (rare race, harmless to
    skip — there's nothing left to attach the delivery record to)."""
    items = load_post_board()
    for item in items:
        if item.get("id") == item_id:
            item.setdefault("broadcasts", []).append({"chat_id": chat_id, "message_id": message_id})
            save_post_board(items)
            return


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
# Rendering — shared by every delivery (initial send or Done-edit)
# ──────────────────────────────────────────────────────────

def _pb_poster_label(item: Dict) -> str:
    """"Jane Doe" (or the raw id if no cached name) for an item's poster."""
    return item.get("posted_by_name") or str(item.get("posted_by", "—"))


def _pb_render_text(item: Dict, cleared_by: Optional[str] = None) -> str:
    """Full display text for a text (ref number) item, built fresh from
    its stored fields every time — see the module docstring for why this
    is never built from a previously-sent Telegram message's retrieved
    text. cleared_by appends the Done line when set."""
    poster = md_escape(_pb_poster_label(item))
    when = item.get("posted_at", "—")
    text = f"🔖 *{md_escape(item.get('text', '—'))}*\nPosted by *{poster}* — `{when}`"
    if cleared_by:
        text += f"\n\n✅ Done — cleared by {md_escape(cleared_by)}"
    return text


def _pb_render_caption(item: Dict, cleared_by: Optional[str] = None) -> str:
    """Caption for a photo item, same "built fresh every time" rule as
    _pb_render_text."""
    poster = md_escape(_pb_poster_label(item))
    when = item.get("posted_at", "—")
    caption_line = f"\n{md_escape(item['caption'])}" if item.get("caption") else ""
    caption = f"📸 Posted by *{poster}* — `{when}`{caption_line}"
    if cleared_by:
        caption += f"\n\n✅ Done — cleared by {md_escape(cleared_by)}"
    return caption


def _pb_done_keyboard(item_id: str) -> InlineKeyboardMarkup:
    """The single ✅ Done button attached to one queue item's message."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done", callback_data=f"pb_done:{item_id}")]])


async def _pb_deliver_item(item: Dict, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Send one item to one chat as its own message with a ✅ Done button,
    and record the delivery so a later Done clears this copy too — the
    one code path used both by the on-post broadcast (every other
    authorized user) and View Queue (whoever's currently checking)."""
    if item["type"] == "photo":
        msg = await ctx.bot.send_photo(
            chat_id, photo=item["photo_file_id"], caption=_pb_render_caption(item),
            parse_mode="Markdown", reply_markup=_pb_done_keyboard(item["id"]),
        )
    else:
        msg = await ctx.bot.send_message(
            chat_id, _pb_render_text(item), parse_mode="Markdown",
            reply_markup=_pb_done_keyboard(item["id"]),
        )
    append_post_item_broadcast(item["id"], chat_id, msg.message_id)


async def _pb_broadcast_new_item(item: Dict, ctx: ContextTypes.DEFAULT_TYPE, poster_id: int) -> None:
    """Push a freshly-posted item to every user who currently has 📥 Post
    Board access, except the poster themselves (they already have their
    own "✅ Added" confirmation, no need to re-notify them about their
    own post). A delivery failure to one recipient (blocked the bot,
    etc.) is logged and skipped rather than aborting the rest."""
    recipients = [
        uid for uid in ALLOWED_IDS
        if uid != poster_id and category_allowed(uid, BTN_CAT_POST_BOARD)
    ]
    for chat_id in recipients:
        try:
            await _pb_deliver_item(item, chat_id, ctx)
        except Exception as e:
            logger.warning("Post Board: broadcast of item %s to %s failed: %s", item["id"], chat_id, e)


# ──────────────────────────────────────────────────────────
# Posting session
# ──────────────────────────────────────────────────────────

async def cmd_pb_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Entry point (/postboard or ➕ Post New Item(s)) — starts a posting
    # session; resets the running count from any previous session.
    if not allowed(update): return await deny(update)
    _get_pb_sess(ctx).posted_count = 0
    await update.message.reply_text(
        "➕ *Posting mode*\n\n"
        "Send reference numbers (one or more per message, one per line or "
        "comma-separated) and/or photos — as many messages as you like. "
        "Everyone else with 📥 Post Board access is notified as you post. "
        "Tap ✅ Done Posting when finished.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton(BTN_DONE_POSTING)]], resize_keyboard=True, is_persistent=True,
        ),
    )
    return PB.POSTING


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
    # comma-separated) — each becomes its own queue item and is broadcast
    # to every other authorized user immediately.
    if not allowed(update): return await deny(update)
    refs = _parse_list_input(update.message.text or "")
    if not refs:
        await update.message.reply_text("Send a reference number, or a photo.")
        return PB.POSTING

    base = _pb_new_item_base(update)
    for ref in refs:
        item = add_post_item({**base, "type": "text", "text": ref})
        await _pb_broadcast_new_item(item, ctx, update.effective_user.id)

    sess = _get_pb_sess(ctx)
    sess.posted_count += len(refs)
    await update.message.reply_text(
        f"✅ Added {len(refs)} item(s) — {sess.posted_count} this session.",
    )
    return PB.POSTING


async def recv_pb_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # A photo message — Telegram sends multiple resolutions, the last is
    # the largest. Its caption (if any) rides along with the queue item,
    # and it's broadcast to every other authorized user immediately.
    if not allowed(update): return await deny(update)
    base = _pb_new_item_base(update)
    item = add_post_item({
        **base,
        "type":          "photo",
        "photo_file_id": update.message.photo[-1].file_id,
        "caption":       update.message.caption or "",
    })
    await _pb_broadcast_new_item(item, ctx, update.effective_user.id)

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
# View Queue — a plain (non-conversation) catch-up action
# ──────────────────────────────────────────────────────────

async def cmd_pb_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Entry point (/postboardqueue or 📋 View Queue) — delivers every
    # still-pending item to whoever asked, the same way a new post is
    # delivered, recording this delivery too so a later Done clears it.
    if not allowed(update): return await deny(update)
    items = load_post_board()
    if not items:
        await update.message.reply_text("✅ No pending items.", reply_markup=_main_menu())
        return

    chat_id = update.effective_chat.id
    for item in items:
        await _pb_deliver_item(item, chat_id, ctx)
    await update.message.reply_text(f"— {len(items)} item(s) total —", reply_markup=_main_menu())


# ──────────────────────────────────────────────────────────
# Done — standalone, works outside any active conversation
# ──────────────────────────────────────────────────────────

async def recv_pb_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Clear one item from the queue and update every copy of it that was
    # ever delivered (the original broadcast, plus any View Queue catch-
    # up deliveries) — not just the one message that was tapped. Not tied
    # to any conversation state — a checker can tap Done on a queue
    # message sent long ago. Purely removes the item; it doesn't verify
    # the ref/photo against anything else.
    if not allowed(update): return await deny(update)
    query = update.callback_query
    await query.answer()
    item_id = query.data.split(":", 1)[1]

    item = get_post_item(item_id)
    if not item or not remove_post_item(item_id):
        await query.edit_message_reply_markup(reply_markup=None)
        return

    who = _pb_poster_label({
        "posted_by_name": load_user_names().get(str(query.from_user.id), ""),
        "posted_by": query.from_user.id,
    })
    logger.info("Post Board: item %s cleared by %s", item_id, who)

    for b in item.get("broadcasts", []):
        try:
            if item["type"] == "photo":
                await ctx.bot.edit_message_caption(
                    chat_id=b["chat_id"], message_id=b["message_id"],
                    caption=_pb_render_caption(item, cleared_by=who), parse_mode="Markdown",
                )
            else:
                await ctx.bot.edit_message_text(
                    chat_id=b["chat_id"], message_id=b["message_id"],
                    text=_pb_render_text(item, cleared_by=who), parse_mode="Markdown",
                )
        except Exception as e:
            logger.warning("Post Board: failed to update cleared copy %s: %s", b, e)


# ──────────────────────────────────────────────────────────
# Registration
# ──────────────────────────────────────────────────────────

def register(app: Application) -> None:
    """Wire the posting-session conversation, the plain View Queue
    handler, and the standalone ✅ Done handler into the given
    Application."""
    pb_conv = ConversationHandler(
        entry_points=[
            CommandHandler("postboard", cmd_pb_post),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_PB_POST)}$"), cmd_pb_post),
        ],
        states={
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
    app.add_handler(CommandHandler("postboardqueue", cmd_pb_view))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_PB_VIEW)}$"), cmd_pb_view))
    app.add_handler(CallbackQueryHandler(recv_pb_done, pattern=r"^pb_done:"))

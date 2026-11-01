import os
import re
import json
import logging
import asyncio
import urllib.parse
from datetime import datetime
import requests
from PIL import Image
from bs4 import BeautifulSoup
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from services import purge_all_storage_and_cookies


import config
from sso_engine import (
    SSOLoginSession,
    fetch_dashboard_status_records,
    render_application_pdf_playwright,
)
from wallet_engine import EmitraWalletRefillSession
from services import (
    load_cookies_from_disk,
    save_cookies_to_disk,
    parse_cookie_input,
    init_session,
    fetch_cascade_async,
    session_keep_alive,
    get_app_dir,
    combine_images_to_pdf,
    find_viewform_and_demand_details,
    execute_demand_payment,
)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

active_sso_sessions = {}
active_refill_sessions = {}

# Safe text filter that never swallows menu buttons or commands
SAFE_TEXT_FILTER = filters.TEXT & ~filters.COMMAND & ~filters.Regex(config.MENU_BUTTON_REGEX)

# --- Universal Cancel & Help Handlers --- #

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in active_refill_sessions:
        await active_refill_sessions[chat_id].cancel()
        del active_refill_sessions[chat_id]

    if chat_id in active_sso_sessions:
        await active_sso_sessions[chat_id].close()
        del active_sso_sessions[chat_id]

    context.user_data.clear()
    await update.effective_message.reply_text("🛑 Operation cancelled. Returned to main menu.", reply_markup=config.MAIN_KEYBOARD)
    return ConversationHandler.END

async def send_safe_options(update: Update, title: str, options: list, key_name: str = "Name"):
    if not options:
        await update.effective_message.reply_text(f"No options found for {title}. Please type manually:")
        return

    lines = []
    for idx, item in enumerate(options):
        label = item.get(key_name) or item.get("Text") or item.get("Value") or "Option"
        lines.append(f"{idx + 1}. {label}")

    header = f"**Select {title}** (Type the number):\n\n"
    current_chunk = header
    for line in lines:
        if len(current_chunk) + len(line) + 2 > 3500:
            await update.effective_message.reply_text(current_chunk)
            current_chunk = ""
        current_chunk += line + "\n"
    if current_chunk.strip():
        await update.effective_message.reply_text(current_chunk)


async def prompt_delete_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompts the user with an inline button to confirm full wipe."""
    kb = [
        [
            InlineKeyboardButton("⚠️ Yes, Delete Everything", callback_data="confirm_purge_all"),
            InlineKeyboardButton("❌ No, Keep Files", callback_data="cancel_purge_all")
        ]
    ]
    await update.effective_message.reply_text(
        "⚠️ **CRITICAL WARNING: Storage & Session Purge**\n\n"
        "This will permanently delete:\n"
        "• Stored login session (`cookies.json`)\n"
        "• All downloaded PDFs, site photos, receipts, and temporary form data in `HeadlessUploads`\n"
        "• Any active background browser tasks\n\n"
        "Are you sure you want to proceed?",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def confirm_purge_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_purge_all":
        await query.message.edit_text("✅ Operation cancelled. Your records and session are safe.")
        return

    if query.data == "confirm_purge_all":
        chat_id = update.effective_chat.id

        # 1. Terminate any active browser sessions
        if chat_id in active_sso_sessions:
            try:
                await active_sso_sessions[chat_id].close()
            except Exception:
                pass
            del active_sso_sessions[chat_id]

        if chat_id in active_refill_sessions:
            try:
                await active_refill_sessions[chat_id].cancel()
            except Exception:
                pass
            del active_refill_sessions[chat_id]

        context.user_data.clear()

        # 2. Run file system purge
        files_del, folders_del = purge_all_storage_and_cookies()

        await query.message.edit_text(
            f"🗑️ **Storage & Sessions Completely Purged!**\n\n"
            f"• **Session file:** Removed (`cookies.json`)\n"
            f"• **Application directories deleted:** {folders_del}\n"
            f"• **Files removed:** {files_del}\n\n"
            "Server storage is now completely clean. Tap **🔑 Login SSO** when ready to begin a new session.",
        )
# --- SSO & Refill Triggers --- #

async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in active_sso_sessions:
        await active_sso_sessions[chat_id].close()
        del active_sso_sessions[chat_id]

    status_msg = await update.effective_message.reply_text("⏳ Opening SSO portal and solving CAPTCHA automatically...")
    sso = SSOLoginSession(chat_id)
    success, msg = await sso.auto_login_and_request_totp()

    if success:
        active_sso_sessions[chat_id] = sso
        try:
            await status_msg.delete()
        except Exception:
            pass
        await update.effective_message.reply_text(
            "✅ **CAPTCHA verified automatically!**\n\n🔑 Enter your 6-digit Google Authenticator TOTP:",
            reply_markup=config.CANCEL_KEYBOARD
        )
    else:
        await status_msg.edit_text(f"❌ Login failed: {msg}\nTap '🔑 Login SSO' to retry.", reply_markup=config.MAIN_KEYBOARD)
        await sso.close()

async def refill_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in active_refill_sessions:
        await active_refill_sessions[chat_id].close()
        del active_refill_sessions[chat_id]

    amount = context.args[0].strip() if context.args else "500"
    status_msg = await update.effective_message.reply_text(f"⏳ Opening SSO to refill ₹{amount} in e-Mitra wallet...")

    session = EmitraWalletRefillSession(chat_id)
    success, msg = await session.auto_login_and_request_totp(amount)

    if success:
        active_refill_sessions[chat_id] = session
        try:
            await status_msg.delete()
        except Exception:
            pass
        await update.effective_message.reply_text(
            f"✅ **CAPTCHA verified automatically!**\n\n🔑 Refill ₹{amount}: Enter your 6-digit Google Authenticator code:",
            reply_markup=config.CANCEL_KEYBOARD
        )
    else:
        await status_msg.edit_text(f"⚠️ Error initiating refill: {msg}", reply_markup=config.MAIN_KEYBOARD)
        await session.close()

# --- Dispatcher for Menu Buttons, Active TOTP, & Lookups --- #

async def menu_text_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip() if update.message.text else ""

    if text in ["/cancel", "❌ Cancel Operation", "Cancel", "cancel"]:
        return await cancel_handler(update, context)

    # Main Menu Actions (Takes precedence over stale background states)
    if text == "🔑 Login SSO":
        return await login_command(update, context)
    elif text == "📝 New Form":
        return await new_form(update, context)
    elif text == "💳 Refill Wallet":
        await update.message.reply_text("Please type `/refill <AMOUNT>`\nExample: `/refill 500`")
        return
    elif text == "💳 Pay Application":
        await update.message.reply_text("Please type `/pay <APPLICATION_NO>`\nExample: `/pay PHED/2026/144821`")
        return
    elif text == "📊 Track Summary":
        return await track_summary_command(update, context)
    elif text == "💰 Pending Payment":
        return await handle_status_display("PAYMENT", update)
    elif text == "🔧 Pending Plumber":
        return await handle_status_display("PLUMBER", update)
    elif text == "🔍 Pending Verification":
        return await handle_status_display("VERIFICATION", update)
    elif text == "⚠️ Objections":
        return await handle_status_display("OBJECTION", update)
    elif text == "📁 List Saved Files":
        return await list_files_command(update, context)
    elif text == "🗑️ Clear Storage & Session":
        return await prompt_delete_all(update, context)

    # Active Wallet Refill TOTP Listener
    if chat_id in active_refill_sessions:
        refill_session = active_refill_sessions[chat_id]
        otp = text.replace(" ", "")
        if len(otp) != 6 or not otp.isdigit():
            await update.message.reply_text("⚠️ Please send exactly a 6-digit number.")
            return

        status_load = await update.message.reply_text("⏳ Navigating e-Mitra and generating PhonePe QR code...")
        success, qr_path, msg = await refill_session.submit_totp_and_get_qr(otp)

        if not success:
            await status_load.edit_text(f"❌ Failed: {msg}", reply_markup=config.MAIN_KEYBOARD)
            del active_refill_sessions[chat_id]
            return

        with open(qr_path, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption=f"📱 **PhonePe QR Code — Amount: ₹{refill_session.amount}**\n\n1. Scan with PhonePe.\n2. Complete payment.\n\n⏳ Waiting for transaction confirmation..."
            )
        if os.path.exists(qr_path):
            os.remove(qr_path)
        await status_load.delete()

        confirmed, balance = await refill_session.wait_for_payment_and_get_balance(timeout_seconds=180)
        if confirmed:
            await update.message.reply_text(f"🎉 **Payment Successful!**\n\n💰 **Current e-Mitra Wallet Balance:** `{balance}`", reply_markup=config.MAIN_KEYBOARD)
        else:
            await update.message.reply_text(f"⚠️ {balance}", reply_markup=config.MAIN_KEYBOARD)

        del active_refill_sessions[chat_id]
        return

    # Active SSO Login TOTP Listener
    if chat_id in active_sso_sessions:
        sso_session = active_sso_sessions[chat_id]
        otp = text.replace(" ", "")
        if len(otp) != 6 or not otp.isdigit():
            await update.message.reply_text("⚠️ Please send exactly a 6-digit number.")
            return

        status_load = await update.message.reply_text("⏳ Submitting TOTP and updating session cookies...")
        success, msg = await sso_session.complete_login_with_totp(otp)
        if success:
            await status_load.edit_text(f"🎉 {msg}", reply_markup=config.MAIN_KEYBOARD)
        else:
            await status_load.edit_text(f"❌ {msg}", reply_markup=config.MAIN_KEYBOARD)
        del active_sso_sessions[chat_id]
        return

    # Direct File Query by Application Number
    await get_files_handler(update, context)

# --- Standard Navigation, Tracking & File Delivery --- #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    inline_kb = [
        [InlineKeyboardButton("🔑 Login SSO", callback_data="btn_login"), InlineKeyboardButton("📝 New Form", callback_data="btn_newform")],
        [InlineKeyboardButton("📊 Track Summary", callback_data="btn_track"), InlineKeyboardButton("💰 Payment Files", callback_data="btn_status_payment")],
        [InlineKeyboardButton("🔧 Plumber Files", callback_data="btn_status_plumber"), InlineKeyboardButton("🔍 Verifications", callback_data="btn_status_verification")],
        [InlineKeyboardButton("⚠️ Objections", callback_data="btn_status_objection"), InlineKeyboardButton("💳 Pay Demand Note", callback_data="btn_pay")],
        [InlineKeyboardButton("📁 List Files", callback_data="btn_listfiles")],
    ]
    await update.message.reply_text("👋 **Jalmitra Automation & Tracker Bot**\n\nUse the buttons below to control the bot:", reply_markup=config.MAIN_KEYBOARD)
    await update.message.reply_text("⚡ **Quick Action Panel:**", reply_markup=InlineKeyboardMarkup(inline_kb))

async def track_summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cookies = load_cookies_from_disk()
    if not cookies:
        await update.effective_message.reply_text("⚠️ No active cookies found. Tap '🔑 Login SSO' first.")
        return

    status_msg = await update.effective_message.reply_text("⏳ Scanning Jalmitra live dashboard records across pages via Playwright...")
    data = await fetch_dashboard_status_records(cookies, max_pages=3)
    summary = (
        "📊 **Jalmitra Dashboard Live Tracking Summary:**\n\n"
        f"• 💰 **Demand Note / Payment Pending:** {len(data['PAYMENT'])} file(s)\n"
        f"• 🔧 **Pending with Plumber:** {len(data['PLUMBER'])} file(s)\n"
        f"• 🔍 **Under Inspection (JEn / AEn / EE):** {len(data['VERIFICATION'])} file(s)\n"
        f"• ⚠️ **Objections / Reverted to Citizen:** {len(data['OBJECTION'])} file(s)\n"
        f"• ✅ **Completed / Sanctioned:** {len(data['COMPLETED'])} file(s)\n"
        f"• 📁 **Other Categories:** {len(data['OTHER'])} file(s)"
    )
    inline_actions = [
        [InlineKeyboardButton("💰 View Payment Files", callback_data="btn_status_payment"), InlineKeyboardButton("🔧 View Plumber Files", callback_data="btn_status_plumber")],
        [InlineKeyboardButton("🔍 View Verifications", callback_data="btn_status_verification"), InlineKeyboardButton("⚠️ View Objections", callback_data="btn_status_objection")],
    ]
    await status_msg.edit_text(summary, reply_markup=InlineKeyboardMarkup(inline_actions))

async def handle_status_display(category_key: str, update: Update):
    valid_map = {"PAYMENT": "PAYMENT", "DEMAND": "PAYMENT", "PLUMBER": "PLUMBER", "VERIFICATION": "VERIFICATION", "OBJECTION": "OBJECTION", "COMPLETED": "COMPLETED"}
    target = valid_map.get(category_key.upper(), "OTHER")
    cookies = load_cookies_from_disk()
    if not cookies:
        await update.effective_message.reply_text("⚠️ No active cookies found. Tap '🔑 Login SSO'.")
        return

    status_msg = await update.effective_message.reply_text(f"⏳ Scanning dashboard records for **{target}** files...")
    data = await fetch_dashboard_status_records(cookies, max_pages=4)
    file_list = data.get(target, [])

    if not file_list:
        await status_msg.edit_text(f"ℹ️ No active files found under **{target}**.")
        return

    await status_msg.edit_text(f"✅ Found **{len(file_list)}** file(s) under **{target}**:")
    chunk_size = 20
    for i in range(0, len(file_list), chunk_size):
        chunk = file_list[i : i + chunk_size]
        part_num = (i // chunk_size) + 1
        total_parts = (len(file_list) + chunk_size - 1) // chunk_size
        msg = f"📋 **{target} Files (Part {part_num}/{total_parts}):**\n\n"
        for idx, item in enumerate(chunk, i + 1):
            msg += f"**{idx}. {item['app_no']}** — {item['name']}\n• **Status:** `{item['status']}`\n• **Date:** {item['status_date'] or item['date']}\n\n"
        await update.effective_message.reply_text(msg)

async def status_filter_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        kb = [
            [InlineKeyboardButton("💰 Payment", callback_data="btn_status_payment"), InlineKeyboardButton("🔧 Plumber", callback_data="btn_status_plumber")],
            [InlineKeyboardButton("🔍 Verification", callback_data="btn_status_verification"), InlineKeyboardButton("⚠️ Objection", callback_data="btn_status_objection")],
        ]
        await update.message.reply_text("Choose a category to view files:", reply_markup=InlineKeyboardMarkup(kb))
        return
    await handle_status_display(context.args[0], update)

async def list_files_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists(config.BASE_DOWNLOAD_DIR):
        await update.effective_message.reply_text("📂 No application files have been saved yet.")
        return
    folders = [f for f in os.listdir(config.BASE_DOWNLOAD_DIR) if os.path.isdir(os.path.join(config.BASE_DOWNLOAD_DIR, f))]
    if not folders:
        await update.effective_message.reply_text("📂 No applications found in storage.")
        return
    msg = "📁 **Saved Application Files Directory:**\n\n"
    for folder in sorted(folders, reverse=True)[:10]:
        fpath = os.path.join(config.BASE_DOWNLOAD_DIR, folder)
        files = os.listdir(fpath)
        pdf_count = len([x for x in files if x.endswith(".pdf")])
        msg += f"• **{folder}**\n  └ {pdf_count} PDF(s) saved ({', '.join(files[:4])})\n\n"
    msg += "To retrieve any file, simply send the Application Number (e.g. `168222`) or Folder Name."
    await update.effective_message.reply_text(msg)

async def inline_button_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "btn_login":
        await login_command(query, context)
    elif data == "btn_newform":
        await query.message.reply_text("To start a new form, tap /newform or use '📝 New Form'.")
    elif data == "btn_track":
        await track_summary_command(update, context)
    elif data == "btn_status_payment":
        await handle_status_display("PAYMENT", update)
    elif data == "btn_status_plumber":
        await handle_status_display("PLUMBER", update)
    elif data == "btn_status_verification":
        await handle_status_display("VERIFICATION", update)
    elif data == "btn_status_objection":
        await handle_status_display("OBJECTION", update)
    elif data == "btn_listfiles":
        await list_files_command(update, context)
    elif data == "btn_pay":
        await query.message.reply_text("Send /pay <Application_Number> or tap '💳 Pay Application' to proceed.")
    elif data in ["confirm_purge_all", "cancel_purge_all"]:
        await confirm_purge_callback(update, context)

async def get_files_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    if query.startswith("/") or not os.path.exists(config.BASE_DOWNLOAD_DIR):
        return

    clean_query = query.replace("/", "_").strip().lower()
    all_folders = [f for f in os.listdir(config.BASE_DOWNLOAD_DIR) if os.path.isdir(os.path.join(config.BASE_DOWNLOAD_DIR, f))]
    target_folder = None
    for folder in all_folders:
        if clean_query in folder.lower():
            target_folder = folder
            break

    if not target_folder:
        return

    folder_path = os.path.join(config.BASE_DOWNLOAD_DIR, target_folder)
    files = [f for f in os.listdir(folder_path) if os.path.isfile(os.path.join(folder_path, f))]
    if not files:
        await update.message.reply_text(f"⚠️ Folder `{target_folder}` is empty.")
        return

    status = await update.message.reply_text(f"⏳ Sending {len(files)} file(s) from `{target_folder}`...")
    for fname in sorted(files):
        fpath = os.path.join(folder_path, fname)
        try:
            with open(fpath, "rb") as f:
                await update.message.reply_document(document=f, filename=fname, caption=f"📁 `{target_folder}`\n📄 `{fname}`")
        except Exception as e:
            logger.error(f"Failed to send file {fname}: {e}")
    try:
        await status.delete()
    except Exception:
        pass

# --- Demand Note Payment Flow --- #

async def pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app_no = context.args[0].strip().upper() if context.args else None
    if not app_no:
        await update.message.reply_text("Please enter the **Application Number** to pay (e.g. `PHED/2026/144821`):", reply_markup=config.CANCEL_KEYBOARD)
        return config.PAY_APP_INPUT
    return await process_pay_app(app_no, update, context)

async def pay_app_input_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_pay_app(update.message.text.strip().upper(), update, context)

async def process_pay_app(app_no: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    cookies = load_cookies_from_disk()
    if not cookies:
        await update.effective_message.reply_text("⚠️ No active cookies found. Tap '🔑 Login SSO'.", reply_markup=config.MAIN_KEYBOARD)
        return ConversationHandler.END

    status_msg = await update.effective_message.reply_text(f"⏳ Searching dynamic records for `{app_no}`...", reply_markup=ReplyKeyboardRemove())
    view_url, demand_info = await find_viewform_and_demand_details(cookies, app_no)
    if not view_url:
        await status_msg.edit_text(f"❌ Application `{app_no}` not found.", reply_markup=config.MAIN_KEYBOARD)
        return ConversationHandler.END

    context.user_data["pay_app_no"] = app_no
    context.user_data["pay_view_url"] = view_url
    context.user_data["pay_doc_path"] = None

    await status_msg.edit_text(
        f"📋 **Application Found:** `{app_no}`\n\n💰 **Demand Note Details:**\n```\n{demand_info}\n```\n\n"
        "Please enter **Remarks** to confirm and proceed with payment (e.g. `Demand Deposited`):"
    )
    return config.PAY_REMARKS

async def pay_remarks_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pay_remarks"] = update.message.text.strip()
    kb = [["Skip Document Upload"], ["❌ Cancel Operation"]]
    await update.message.reply_text("Upload any **Supporting Document (PDF or Photo)** for payment, or tap **Skip**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return config.PAY_DOC

async def pay_doc_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app_no = context.user_data.get("pay_app_no", "APP")
    clean_app = app_no.replace("/", "_")
    app_dir = os.path.join(config.BASE_DOWNLOAD_DIR, f"PAYMENT_{clean_app}")
    os.makedirs(app_dir, exist_ok=True)

    text = update.message.text.strip() if update.message.text else ""
    doc_path = None
    if "Skip" not in text:
        if update.message.document:
            f = await update.message.document.get_file(read_timeout=60)
            doc_path = os.path.join(app_dir, "Payment_Doc.pdf")
            await f.download_to_drive(doc_path)
        elif update.message.photo:
            f = await update.message.photo[-1].get_file(read_timeout=60)
            temp_p = os.path.join(app_dir, "temp_pay.jpg")
            await f.download_to_drive(temp_p)
            doc_path = os.path.join(app_dir, "Payment_Doc.pdf")
            with Image.open(temp_p) as img:
                img.convert("RGB").save(doc_path, "PDF")
            if os.path.exists(temp_p):
                os.remove(temp_p)

    status_msg = await update.message.reply_text("⏳ Submitting payment payload...", reply_markup=ReplyKeyboardRemove())
    cookies = load_cookies_from_disk()
    success, msg, receipt_url = await execute_demand_payment(cookies, context.user_data["pay_view_url"], context.user_data["pay_remarks"], doc_path)

    if not success:
        await status_msg.edit_text(f"❌ Payment Failed:\n{msg}", reply_markup=config.MAIN_KEYBOARD)
        return ConversationHandler.END

    await status_msg.edit_text(f"🎉 **Payment Status:** {msg}\n\n⏳ Generating paid receipt / form copy...")
    receipt_pdf = os.path.join(app_dir, f"Receipt_{clean_app}.pdf")
    target_url = receipt_url if receipt_url else context.user_data["pay_view_url"]
    rendered = await render_application_pdf_playwright(cookies, app_no, target_url, receipt_pdf)

    if rendered and os.path.exists(receipt_pdf):
        with open(receipt_pdf, "rb") as f:
            await update.effective_message.reply_document(document=f, filename=f"Paid_Receipt_{clean_app}.pdf", caption=f"🧾 **Payment Receipt**\nApp No: `{app_no}`")
    await update.effective_message.reply_text("✅ Operation complete.", reply_markup=config.MAIN_KEYBOARD)
    return ConversationHandler.END

# --- Form Filling Multi-Step Handlers --- #

async def new_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["files"] = {}
    context.user_data["photo_buffers"] = {}

    saved = load_cookies_from_disk()
    if saved and (saved.get(".ASPXAUTH") or saved.get("ASP.NET_SessionId")):
        kb = [["Use Saved Cookies", "Paste New Cookies"], ["❌ Cancel Operation"]]
        await update.message.reply_text(
            "Found previously saved `cookies.json`.\nWould you like to use them or provide new ones?",
            reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True)
        )
        return config.CHECK_SAVED_COOKIES

    await update.message.reply_text("Paste your **Cookies** (or tap '🔑 Login SSO' to login automatically):", reply_markup=config.CANCEL_KEYBOARD)
    return config.COOKIES

async def check_saved_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if "Use Saved Cookies" in choice:
        c_dict = load_cookies_from_disk()
        if not c_dict:
            await update.effective_message.reply_text("⚠️ No saved cookies found. Please paste fresh cookies:")
            return config.COOKIES

        status = await update.effective_message.reply_text("⏳ Validating saved session...", reply_markup=ReplyKeyboardRemove())
        try:
            session, token = await asyncio.wait_for(asyncio.to_thread(init_session, c_dict), timeout=10.0)
            context.user_data["cookies"] = session.cookies.get_dict()
            context.user_data["token"] = token
            try:
                await status.delete()
            except Exception:
                pass

            await update.effective_message.reply_text("✅ Saved session verified!")
            kb = [["1. Individual (Indian Citizen)", "2. Government", "3. Commercial/Industrial"], ["❌ Cancel Operation"]]
            await update.effective_message.reply_text("Select **Citizen Type**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
            return config.CITIZEN_TYPE
        except Exception as e:
            try:
                await status.delete()
            except Exception:
                pass
            await update.effective_message.reply_text(
                "⚠️ **Saved session expired or invalid.**\n\nRun `/login` to refresh automatically, or paste fresh cookies below:",
                reply_markup=config.MAIN_KEYBOARD
            )
            return config.COOKIES

    await update.message.reply_text("Paste your fresh **Cookies** (or upload file):", reply_markup=config.CANCEL_KEYBOARD)
    return config.COOKIES

async def set_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_content = ""
    if update.message.document:
        f = await update.message.document.get_file(read_timeout=60)
        file_bytes = await f.download_as_bytearray()
        raw_content = file_bytes.decode("utf-8", errors="ignore")
    elif update.message.text:
        raw_content = update.message.text.strip()

    c_dict = parse_cookie_input(raw_content)
    if not c_dict.get(".ASPXAUTH") and not c_dict.get("ASP.NET_SessionId"):
        await update.message.reply_text("⚠️ Invalid cookies. Could not find `.ASPXAUTH` or `ASP.NET_SessionId`. Paste again:", reply_markup=config.CANCEL_KEYBOARD)
        return config.COOKIES

    status = await update.message.reply_text("⏳ Saving to `cookies.json` and verifying session...")
    try:
        session, token = await asyncio.to_thread(init_session, c_dict)
        final_cookies = session.cookies.get_dict()
        save_cookies_to_disk(final_cookies)
        context.user_data["cookies"] = final_cookies
        context.user_data["token"] = token
        await status.edit_text("✅ Saved to `cookies.json` & session active!")

        kb = [["1. Individual (Indian Citizen)", "2. Government", "3. Commercial/Industrial"], ["❌ Cancel Operation"]]
        await update.message.reply_text("Select **Citizen Type**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
        return config.CITIZEN_TYPE
    except Exception as e:
        await status.edit_text(f"❌ Session failed: {e}\nPlease log in again and paste fresh cookies.")
        return config.COOKIES

async def set_citizen_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text
    context.user_data["APPLICANT_CITIZENTYPE"] = "INDIAN CITIZEN" if "Individual" in val or "1" in val else ("GOVERNMENT" if "Government" in val else "COMMERCIAL")
    kb = [["Yes", "No"], ["❌ Cancel Operation"]]
    await update.message.reply_text("Do you have a **Jan Aadhaar**?", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return config.HAVE_JANAADHAR

async def set_have_janaadhar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["HAVE_JANAADHAR"] = "YES" if "yes" in update.message.text.strip().lower() else "NO"
    await update.message.reply_text("Enter **Applicant Full Name**:", reply_markup=config.CANCEL_KEYBOARD)
    return config.APPLICANT_NAME

async def set_applicant_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["APPLICANT_FULLNAME"] = update.message.text.strip().upper()
    await update.message.reply_text("Enter **Father's / Husband's Name**:", reply_markup=config.CANCEL_KEYBOARD)
    return config.FATHER_NAME

async def set_father_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["APPLICANT_FATHER_NAME"] = update.message.text.strip().upper()
    kb = [["MALE", "FEMALE", "TRANSGENDER"], ["❌ Cancel Operation"]]
    await update.message.reply_text("Select **Gender**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return config.GENDER

async def set_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip().upper()
    context.user_data["APPLICANT_GENDER"] = val if val in ["MALE", "FEMALE", "TRANSGENDER"] else "FEMALE"
    await update.message.reply_text("Enter **Date of Birth** (`DD/MM/YYYY`):", reply_markup=config.CANCEL_KEYBOARD)
    return config.DOB

async def set_dob(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["APPLICANT_DOBV"] = update.message.text.strip()
    await update.message.reply_text("Enter **10-Digit Mobile Number**:", reply_markup=config.CANCEL_KEYBOARD)
    return config.MOBILE_NO

async def set_mobile_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["APPLICANT_MOBILE"] = update.message.text.strip()
    await update.message.reply_text("Enter **Email ID** (or type `None`):", reply_markup=config.CANCEL_KEYBOARD)
    return config.EMAIL_ID

async def set_email_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    context.user_data["APPLICANT_EMAIL"] = "" if val.lower() == "none" else val.upper()
    kb = [["1. Non-BPL (General)", "2. BPL"], ["❌ Cancel Operation"]]
    await update.message.reply_text("Select **Consumer Category**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return config.CONSUMER_CATEGORY

async def set_consumer_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    context.user_data["APPLICANT_CATEGORY"] = "BPL" if "bpl" in val.lower() and "non" not in val.lower() else "NON BPL"
    kb = [["1. Identity Document", "2. Voter ID", "3. PAN Card", "4. Driving License"], ["❌ Cancel Operation"]]
    await update.message.reply_text("Select **Proof of Identity Type**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return config.IDENTITY_TYPE

async def set_identity_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    type_name = "AADHAAR CARD"
    if "voter" in val.lower():
        type_name = "VOTER ID"
    elif "pan" in val.lower():
        type_name = "PAN CARD"
    elif "driving" in val.lower():
        type_name = "DRIVING LICENSE"
    context.user_data["APPLICANT_IDENTITY_TYPE"] = type_name
    await update.message.reply_text("Enter the **Identity Document Number**:", reply_markup=config.CANCEL_KEYBOARD)
    return config.IDENTITY_NUMBER

async def set_identity_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["APPLICANT_IDENTITY_NUMBER"] = update.message.text.strip()
    dist_lines = [f"{k}. {v}" for k, v in sorted(config.DISTRICT_MAP.items(), key=lambda x: int(x[0]))]
    msg = "📍 **Select Permanent District** (Type Number or Name):\n\n" + "\n".join(dist_lines[:16])
    await update.message.reply_text(msg)
    await update.message.reply_text("\n".join(dist_lines[16:]))
    return config.PERM_DISTRICT

async def set_perm_district(update: Update, context: ContextTypes.DEFAULT_TYPE):
    inp = update.message.text.strip().upper()
    dist_id = "12"
    for k, v in config.DISTRICT_MAP.items():
        if inp == k or inp == v:
            dist_id = k
            break
    context.user_data["PERMANENT_DISTRICT_ID"] = dist_id
    kb = [["1. Urban (City/Town)", "2. Rural (Block/Village)"], ["❌ Cancel Operation"]]
    await update.message.reply_text(f"District set: **{config.DISTRICT_MAP.get(dist_id, dist_id)}**\nSelect **Area Type**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return config.PERM_AREA_TYPE

async def set_perm_area_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    inp = update.message.text
    area_type = "URBAN" if "Urban" in inp or "1" in inp else "RURAL"
    context.user_data["PERMANENT_AREA_TYPE"] = area_type

    status = await update.message.reply_text("⏳ Fetching Cities / Blocks from portal...", reply_markup=config.CANCEL_KEYBOARD)
    mst = "CITY" if area_type == "URBAN" else "BLOCK"
    dist_id = context.user_data.get("PERMANENT_DISTRICT_ID", "12")
    options = await fetch_cascade_async(context.user_data["cookies"], dist_id, mst)
    try:
        await status.delete()
    except Exception:
        pass

    if not options or not isinstance(options, list):
        fallback_name = "JAIPUR" if dist_id == "12" else "Main City"
        options = [{"Id": dist_id, "Name": fallback_name}]

    context.user_data["cached_blocks"] = options
    await send_safe_options(update, mst, options)
    return config.PERM_BLOCK_SELECT

async def set_perm_block_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    options = context.user_data.get("cached_blocks", [])
    selected_id = context.user_data.get("PERMANENT_DISTRICT_ID", "12")
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(options):
            selected_id = str(options[idx].get("Id", options[idx].get("Value", selected_id)))
    except Exception:
        for opt in options:
            if choice.lower() in opt.get("Name", "").lower():
                selected_id = str(opt.get("Id", selected_id))
                break

    context.user_data["PERMANENT_CITY_BLOCK_ID"] = selected_id
    status = await update.message.reply_text("⏳ Fetching Wards / Gram Panchayats...", reply_markup=config.CANCEL_KEYBOARD)
    wards = await fetch_cascade_async(context.user_data["cookies"], selected_id, "WARD")
    try:
        await status.delete()
    except Exception:
        pass

    if not wards or not isinstance(wards, list):
        wards = [{"Id": "66", "Name": "WARD NO. 66"}]

    context.user_data["cached_wards"] = wards
    await send_safe_options(update, "Ward / GP", wards)
    return config.PERM_WARD_SELECT

async def set_perm_ward_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    wards = context.user_data.get("cached_wards", [])
    selected_id = "66"
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(wards):
            selected_id = str(wards[idx].get("Id", wards[idx].get("Value", "66")))
    except Exception:
        pass
    context.user_data["PERMANENT_WARD_GP_ID"] = selected_id
    await update.message.reply_text("Enter **Permanent House Address / Street / Colony**:", reply_markup=config.CANCEL_KEYBOARD)
    return config.PERM_ADDRESS

async def set_perm_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["PERMANENT_ADDRESS"] = update.message.text.strip().upper()
    await update.message.reply_text("Enter **6-Digit Pincode**:", reply_markup=config.CANCEL_KEYBOARD)
    return config.PERM_PINCODE

async def set_perm_pincode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["PERMANENT_PINCODE"] = update.message.text.strip()
    kb = [["1. Proof of Identity", "2. Voter ID", "3. Electricity Bill", "4. Registry/Patta"], ["❌ Cancel Operation"]]
    await update.message.reply_text("Select **Proof of Address Type**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return config.PERM_PROOF_TYPE

async def set_perm_proof_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text
    type_name = "AADHAAR CARD"
    if "voter" in val.lower():
        type_name = "VOTER ID"
    elif "electricity" in val.lower():
        type_name = "ELECTRICITY BILL"
    elif "registry" in val.lower():
        type_name = "REGISTRY"
    context.user_data["PERMANENT_PROOF_TYPE"] = type_name
    await update.message.reply_text("Enter **Proof of Address Document Number** (or type `same`):", reply_markup=config.CANCEL_KEYBOARD)
    return config.PERM_PROOF_NUM

async def set_perm_proof_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    context.user_data["PERMANENT_PROOF_NUMBER"] = context.user_data["APPLICANT_IDENTITY_NUMBER"] if val.lower() == "same" else val
    kb = [["Yes", "No"], ["❌ Cancel Operation"]]
    await update.message.reply_text("Is the **Correspondence Address** same as Permanent Address?", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return config.CORR_SAMEAS

async def set_corr_sameas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["CORRESPONDENCE_SAMEAS"] = "YES" if "yes" in update.message.text.strip().lower() else "NO"
    kb = [["1. Permanent Address", "2. Correspondence Address"], ["❌ Cancel Operation"]]
    await update.message.reply_text("Where is the new water connection required?", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return config.CONN_LOCATION_CHOICE

async def set_conn_location_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text
    context.user_data["CONNECTION_SAMEAS"] = "PERMANENT ADDRESS" if "1" in val or "Permanent" in val else "CORRESPONDENCE ADDRESS"
    kb = [["1. New Connection", "2. Additional Connection"], ["❌ Cancel Operation"]]
    await update.message.reply_text("Select **Application Type**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return config.CONN_APP_TYPE

async def set_conn_app_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text
    context.user_data["CONNECTION_APPLICATION_TYPE"] = "NEW CONNECTION" if "New" in val or "1" in val else "ADDITIONAL CONNECTION"
    def_dist = context.user_data.get("PERMANENT_DISTRICT_ID", "12")
    kb = [[f"Use Same ({config.DISTRICT_MAP.get(def_dist, def_dist)})"], ["❌ Cancel Operation"]]
    await update.message.reply_text("Select **Connection District** (or type number):", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return config.CONN_DISTRICT

async def set_conn_district(update: Update, context: ContextTypes.DEFAULT_TYPE):
    inp = update.message.text.strip().upper()
    dist_id = context.user_data.get("PERMANENT_DISTRICT_ID", "12")
    for k, v in config.DISTRICT_MAP.items():
        if inp == k or inp == v:
            dist_id = k
            break
    context.user_data["CONNECTION_DISTRICT_ID"] = dist_id
    status = await update.message.reply_text(f"⏳ Fetching Division Offices for {config.DISTRICT_MAP.get(dist_id, dist_id)}...", reply_markup=config.CANCEL_KEYBOARD)
    divisions = await fetch_cascade_async(context.user_data["cookies"], dist_id, "DIVISION")
    try:
        await status.delete()
    except Exception:
        pass

    if not divisions or not isinstance(divisions, list):
        divisions = [{"Id": "3006082", "Name": "CITY DIVISION-III (SOUTH), MALVIYA NAGAR, JAIPUR"}]

    context.user_data["cached_divisions"] = divisions
    await send_safe_options(update, "Division Office", divisions)
    return config.CONN_DIVISION_SELECT

async def set_conn_division_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    divs = context.user_data.get("cached_divisions", [])
    div_id = "3006082"
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(divs):
            div_id = str(divs[idx].get("Id", divs[idx].get("Value", div_id)))
    except Exception:
        pass
    context.user_data["CONNECTION_DIVISION_OFFICE_ID"] = div_id

    status = await update.message.reply_text("⏳ Fetching Sub-Division Offices...", reply_markup=config.CANCEL_KEYBOARD)
    subdivisions = await fetch_cascade_async(context.user_data["cookies"], div_id, "SUBDIVISION")
    try:
        await status.delete()
    except Exception:
        pass

    if not subdivisions or not isinstance(subdivisions, list):
        subdivisions = [{"Id": "3006124", "Name": "SUB DIVISION CITY-VI (S) JAGATPURA, JAIPUR"}]

    context.user_data["cached_subdivisions"] = subdivisions
    await send_safe_options(update, "Sub-Division Office", subdivisions)
    return config.CONN_SUBDIVISION_SELECT

async def set_conn_subdivision_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    subs = context.user_data.get("cached_subdivisions", [])
    sub_id = "3006124"
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(subs):
            sub_id = str(subs[idx].get("Id", subs[idx].get("Value", sub_id)))
    except Exception:
        pass
    context.user_data["CONNECTION_SUB_DIVISION_OFFICE_ID"] = sub_id

    kb = [["1. Permanent", "2. Temporary"], ["❌ Cancel Operation"]]
    await update.message.reply_text("Select **Connection Type**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return config.CONN_TYPE

async def set_conn_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text
    context.user_data["CONNECTION_TYPE"] = "PERMANENT" if "Permanent" in val or "1" in val else "TEMPORARY"
    kb = [["1. Domestic", "2. Non-Domestic"], ["❌ Cancel Operation"]]
    await update.message.reply_text("Select **Connection Sub-Category**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return config.CONN_SUB_CAT

async def set_conn_sub_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["CONNECTION_SUB_CATEGORY"] = "NON BPL" if context.user_data.get("APPLICANT_CATEGORY") == "NON BPL" else "BPL"
    kb = [["1. Private", "2. Government", "3. Institutional"], ["❌ Cancel Operation"]]
    await update.message.reply_text("Select **Property Category**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return config.CONN_PROP_CAT

async def set_conn_prop_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text
    context.user_data["CONNECTION_PROPERTY_CATEGORY"] = "PRIVATE" if "Private" in val or "1" in val else ("GOVERNMENT" if "Government" in val else "INSTITUTIONAL")
    kb = [["1. Owner", "2. Tenant", "3. Leased"], ["❌ Cancel Operation"]]
    await update.message.reply_text("Select **Property Ownership Status**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return config.CONN_PROP_OWNERSHIP

async def set_conn_prop_ownership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text
    context.user_data["CONNECTION_PROPERTY_OWNERSHIP"] = "OWNER" if "Owner" in val or "1" in val else ("TENANT" if "Tenant" in val else "LEASED")
    kb = [["1. Residential House", "2. Commercial", "3. Industrial"], ["❌ Cancel Operation"]]
    await update.message.reply_text("Select **Connection Type Category**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return config.CONN_CATEGORY

async def set_conn_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text
    context.user_data["CONNECTION_CATEGORY"] = "RESIDENTIAL HOUSE" if "Residential" in val or "1" in val else "COMMERCIAL"
    await update.message.reply_text("Enter **Building Height (in Meters)** (e.g. `3.66`):", reply_markup=config.CANCEL_KEYBOARD)
    return config.CONN_BUILDING_HEIGHT

async def set_conn_building_height(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["CONNECTION_BUILDING_HEIGHT"] = update.message.text.strip()
    context.user_data["CONNECTION_BUILDING_HEIGHT_UNIT"] = "1"
    kb = [["No", "Yes"], ["❌ Cancel Operation"]]
    await update.message.reply_text("Is the building a **Multi-Story Building**?", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return config.CONN_IS_MULTISTORY

async def set_conn_is_multistory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["CONNECTION_IS_MULTISTORY"] = "YES" if "yes" in update.message.text.strip().lower() else "NO"
    await update.message.reply_text("Enter **Total Floors** in Building (e.g. `1` or `2`):", reply_markup=config.CANCEL_KEYBOARD)
    return config.CONN_TOTAL_FLOORS

async def set_conn_total_floors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["CONNECTION_BUILDING_TOTAL_FLOORS"] = update.message.text.strip()
    kb = [["15 mm (1/2 inch)", "20 mm (3/4 inch)", "25 mm (1 inch)"], ["❌ Cancel Operation"]]
    await update.message.reply_text("Select **Connection Pipe Size**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return config.CONN_PIPE_SIZE

async def set_conn_pipe_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text
    context.user_data["CONNECTION_PIPE_SIZE"] = "20" if "20" in val else ("25" if "25" in val else "15")
    await update.message.reply_text("Enter **Total Plot Area** and **Built-up Area** (Sq. Mtr.) separated by space (e.g. `211.33 211.33`):", reply_markup=config.CANCEL_KEYBOARD)
    return config.CONN_AREAS

async def set_conn_areas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().split()
    context.user_data["CONNECTION_ACTUAL_AREA"] = parts[0] if len(parts) > 0 else "211.33"
    context.user_data["CONNECTION_BUILTUP_AREA"] = parts[1] if len(parts) > 1 else "211.33"
    context.user_data["CONNECTION_CARPET_AREA"] = ""
    kb = [["No", "Yes"], ["❌ Cancel Operation"]]
    await update.message.reply_text("Is **Rainwater Harvesting Structure** constructed?", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return config.RAIN_HARVESTING

async def set_rain_harvesting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["IS_RAIN_WATER_HARVESTING"] = "YES" if "yes" in update.message.text.strip().lower() else "NO"
    await update.message.reply_text(
        "📁 **Document Uploads Begin**\n\nSend **1. Passport Size Photo of Applicant**:\n• Send **PDF** directly, or send **Photos** and tap 'Done'.",
        reply_markup=config.CANCEL_KEYBOARD
    )
    return config.DOC_PHOTO

# --- Universal Document Handling Engine --- #

async def handle_document_entry(update: Update, context: ContextTypes.DEFAULT_TYPE, field_name: str, collect_state: int, next_prompt: str, next_state: int):
    app_dir = get_app_dir(context)
    buffers = context.user_data.setdefault("photo_buffers", {})

    if update.message.document:
        doc = update.message.document
        if doc.file_name.lower().endswith(".pdf"):
            pdf_path = os.path.join(app_dir, f"{field_name}.pdf")
            f = await doc.get_file(read_timeout=120)
            await f.download_to_drive(pdf_path)
            context.user_data["files"][field_name] = pdf_path
            await update.message.reply_text(f"✅ PDF saved for **{field_name}**.\n\n{next_prompt}")
            return next_state

    file_obj = None
    if update.message.photo:
        file_obj = await update.message.photo[-1].get_file(read_timeout=120)
    elif update.message.document:
        file_obj = await update.message.document.get_file(read_timeout=120)

    if file_obj:
        temp_img_name = f"temp_{field_name}_{len(buffers.get(field_name, [])) + 1}.jpg"
        temp_img_path = os.path.join(app_dir, temp_img_name)
        await file_obj.download_to_drive(temp_img_path)
        buffers.setdefault(field_name, []).append(temp_img_path)
        kb = [["➕ Add More Photos", "✅ Done (Convert to PDF)"], ["❌ Cancel Operation"]]
        await update.message.reply_text(
            f"📸 Received photo #{len(buffers[field_name])} for **{field_name}**.\nUpload another photo or tap **Done**:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
        )
        return collect_state

    await update.message.reply_text("⚠️ Unsupported format. Please send a Photo or PDF file:")
    return None

async def handle_document_collection(update: Update, context: ContextTypes.DEFAULT_TYPE, field_name: str, collect_state: int, next_prompt: str, next_state: int):
    app_dir = get_app_dir(context)
    buffers = context.user_data.get("photo_buffers", {})
    text = update.message.text.strip() if update.message.text else ""

    if "Done" in text or "✅" in text:
        img_list = buffers.get(field_name, [])
        if not img_list:
            await update.message.reply_text("⚠️ No photos received yet. Please send a photo first:")
            return collect_state

        pdf_path = os.path.join(app_dir, f"{field_name}.pdf")
        status = await update.message.reply_text("⏳ Merging images into PDF...", reply_markup=ReplyKeyboardRemove())
        final_pdf = combine_images_to_pdf(img_list, pdf_path)
        buffers[field_name] = []

        if final_pdf and os.path.exists(final_pdf):
            context.user_data["files"][field_name] = final_pdf
            await status.delete()
            with open(final_pdf, "rb") as f:
                await update.message.reply_document(document=f, caption=f"📄 Combined {field_name} PDF")
            await update.message.reply_text(f"✅ Saved!\n\n{next_prompt}")
            return next_state
        else:
            await update.message.reply_text("❌ Failed to create PDF. Please upload photo/PDF again:")
            return collect_state

    file_obj = None
    if update.message.photo:
        file_obj = await update.message.photo[-1].get_file(read_timeout=120)
    elif update.message.document:
        file_obj = await update.message.document.get_file(read_timeout=120)

    if file_obj:
        temp_img_name = f"temp_{field_name}_{len(buffers.get(field_name, [])) + 1}.jpg"
        temp_img_path = os.path.join(app_dir, temp_img_name)
        await file_obj.download_to_drive(temp_img_path)
        buffers.setdefault(field_name, []).append(temp_img_path)
        await update.message.reply_text(f"📸 Added photo #{len(buffers[field_name])}. Send more or tap 'Done':")
        return collect_state

    return collect_state

async def doc_photo_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nxt = "Send **2. Proof of Identity** (Identity Document / Voter ID):"
    res = await handle_document_entry(update, context, "APPLICANT_PHOTO", config.DOC_PHOTO_COLLECT, nxt, config.DOC_ID_PROOF)
    return res if res is not None else config.DOC_PHOTO

async def doc_photo_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nxt = "Send **2. Proof of Identity** (Identity Document / Voter ID):"
    return await handle_document_collection(update, context, "APPLICANT_PHOTO", config.DOC_PHOTO_COLLECT, nxt, config.DOC_ID_PROOF)

async def doc_id_proof_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nxt = "Send **3. Proof of Address** (Identity Document / Voter / Electricity Bill / Registry):"
    res = await handle_document_entry(update, context, "APPLICANT_IDENTITY", config.DOC_ID_PROOF_COLLECT, nxt, config.DOC_ADDR_PROOF)
    return res if res is not None else config.DOC_ID_PROOF

async def doc_id_proof_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nxt = "Send **3. Proof of Address** (Identity Document / Voter / Electricity Bill / Registry):"
    return await handle_document_collection(update, context, "APPLICANT_IDENTITY", config.DOC_ID_PROOF_COLLECT, nxt, config.DOC_ADDR_PROOF)

async def doc_addr_proof_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text and "same" in update.message.text.lower():
        context.user_data["files"]["ADDRESS_PROOF"] = context.user_data["files"].get("APPLICANT_IDENTITY")
        await update.message.reply_text("✅ Address proof set as Identity document.\n\nSend **4. Property Ownership Proof** (Registry / Patta):")
        return config.DOC_OWNERSHIP
    nxt = "Send **4. Property Ownership Proof** (Registry / Patta):"
    res = await handle_document_entry(update, context, "ADDRESS_PROOF", config.DOC_ADDR_PROOF_COLLECT, nxt, config.DOC_OWNERSHIP)
    return res if res is not None else config.DOC_ADDR_PROOF

async def doc_addr_proof_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nxt = "Send **4. Property Ownership Proof** (Registry / Patta):"
    return await handle_document_collection(update, context, "ADDRESS_PROOF", config.DOC_ADDR_PROOF_COLLECT, nxt, config.DOC_OWNERSHIP)

async def doc_ownership_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nxt = "Send **5. Site Plan / Map**:"
    res = await handle_document_entry(update, context, "OWNERSHIP_PROOF", config.DOC_OWNERSHIP_COLLECT, nxt, config.DOC_SITE_PLAN)
    return res if res is not None else config.DOC_OWNERSHIP

async def doc_ownership_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nxt = "Send **5. Site Plan / Map**:"
    return await handle_document_collection(update, context, "OWNERSHIP_PROOF", config.DOC_OWNERSHIP_COLLECT, nxt, config.DOC_SITE_PLAN)

async def doc_site_plan_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nxt = "Send **6. Photograph of Property / Front Elevation**:"
    res = await handle_document_entry(update, context, "SITE_PLAN", config.DOC_SITE_PLAN_COLLECT, nxt, config.DOC_PROP_PHOTO)
    return res if res is not None else config.DOC_SITE_PLAN

async def doc_site_plan_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nxt = "Send **6. Photograph of Property / Front Elevation**:"
    return await handle_document_collection(update, context, "SITE_PLAN", config.DOC_SITE_PLAN_COLLECT, nxt, config.DOC_PROP_PHOTO)

async def doc_prop_photo_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["Done - Submit Form"], ["Neighbor Water Bill", "Electricity Bill"], ["Rain Harvesting Certificate", "Other Document"], ["❌ Cancel Operation"]]
    nxt = "Do you want to upload any **Optional Supporting Documents**?"
    res = await handle_document_entry(update, context, "PROPERTY_PHOTO", config.DOC_PROP_PHOTO_COLLECT, nxt, config.DOC_SUPPORTING_CHOICE)
    if res == config.DOC_SUPPORTING_CHOICE:
        await update.message.reply_text(nxt, reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return res if res is not None else config.DOC_PROP_PHOTO

async def doc_prop_photo_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["Done - Submit Form"], ["Neighbor Water Bill", "Electricity Bill"], ["Rain Harvesting Certificate", "Other Document"], ["❌ Cancel Operation"]]
    nxt = "Do you want to upload any **Optional Supporting Documents**?"
    res = await handle_document_collection(update, context, "PROPERTY_PHOTO", config.DOC_PROP_PHOTO_COLLECT, nxt, config.DOC_SUPPORTING_CHOICE)
    if res == config.DOC_SUPPORTING_CHOICE:
        await update.message.reply_text(nxt, reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return res

async def doc_supporting_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if "Done - Submit Form" in choice:
        return await finalize_and_submit(update, context)
    mapping = {
        "Neighbor Water Bill": "NEIGHBOUR_WATER_BILL",
        "Electricity Bill": "ELECTRICITY_BILL",
        "Rain Harvesting Certificate": "RAIN_HARVESTING_CERTIFICATE",
        "Other Document": "OTHER_DOC",
    }
    field_key = mapping.get(choice, "OTHER_DOC")
    context.user_data["current_supporting_field"] = field_key
    await update.message.reply_text(f"Upload file for **{choice}** (PDF or Photos):", reply_markup=config.CANCEL_KEYBOARD)
    return config.DOC_SUPPORTING_UPLOAD

async def doc_supporting_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get("current_supporting_field", "OTHER_DOC")
    res = await handle_document_entry(update, context, field, config.DOC_SUPPORTING_COLLECT, "Document recorded.", config.DOC_SUPPORTING_CHOICE)
    if res == config.DOC_SUPPORTING_CHOICE:
        kb = [["Done - Submit Form"], ["Neighbor Water Bill", "Electricity Bill"], ["Rain Harvesting Certificate", "Other Document"], ["❌ Cancel Operation"]]
        await update.message.reply_text("Add another supporting document or finish:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return res if res is not None else config.DOC_SUPPORTING_UPLOAD

async def doc_supporting_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get("current_supporting_field", "OTHER_DOC")
    res = await handle_document_collection(update, context, field, config.DOC_SUPPORTING_COLLECT, "Document recorded.", config.DOC_SUPPORTING_CHOICE)
    if res == config.DOC_SUPPORTING_CHOICE:
        kb = [["Done - Submit Form"], ["Neighbor Water Bill", "Electricity Bill"], ["Rain Harvesting Certificate", "Other Document"], ["❌ Cancel Operation"]]
        await update.message.reply_text("Add another supporting document or finish:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return res

# --- Form Submission Execution --- #

async def finalize_and_submit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = await update.message.reply_text("⏳ Assembling payload and connecting to Rajasthan PHED...", reply_markup=ReplyKeyboardRemove())

    d = context.user_data
    dist_id = d.get("CONNECTION_DISTRICT_ID", "12")
    div_id = d.get("CONNECTION_DIVISION_OFFICE_ID", "3006082")
    subdiv_id = d.get("CONNECTION_SUB_DIVISION_OFFICE_ID", "3006124")
    city_val = d.get("PERMANENT_CITY_BLOCK_ID") or "JAIPUR"
    ward_val = d.get("PERMANENT_WARD_GP_ID") or "WARD NO. 66"
    address_str = d.get("PERMANENT_ADDRESS", "").upper()
    pincode_str = d.get("PERMANENT_PINCODE", "302017")
    id_num = d.get("APPLICANT_IDENTITY_NUMBER", "")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"{config.BASE_URL}/Phed/WC_Form",
        "Origin": "https://ssoapps.rajasthan.gov.in",
    })
    session.cookies.update(d.get("cookies", {}))

    fresh_token = d.get("token", "")
    try:
        def _get_token():
            r = session.get(f"{config.BASE_URL}/Phed/WC_Form", timeout=20)
            tag = BeautifulSoup(r.text, "html.parser").find("input", {"name": "__RequestVerificationToken"})
            return tag.get("value", "") if tag else ""
        extracted = await asyncio.to_thread(_get_token)
        if extracted:
            fresh_token = extracted
    except Exception:
        pass

    form_payload = {
        "__RequestVerificationToken": fresh_token,
        "APPLICANT_CITIZENTYPE": d.get("APPLICANT_CITIZENTYPE", "INDIAN CITIZEN"),
        "HAVE_JANAADHAR": d.get("HAVE_JANAADHAR", "NO"),
        "JANAADHAAR_ENR_ID_FAMILY_ID": "",
        "APPLICANT_FULLNAME": d.get("APPLICANT_FULLNAME", "").upper(),
        "APPLICANT_FATHER_NAME": d.get("APPLICANT_FATHER_NAME", "").upper(),
        "APPLICANT_GENDER": d.get("APPLICANT_GENDER", "FEMALE"),
        "APPLICANT_DOBV": d.get("APPLICANT_DOBV", "01/01/1990"),
        "APPLICANT_MOBILE": d.get("APPLICANT_MOBILE", ""),
        "APPLICANT_EMAIL": d.get("APPLICANT_EMAIL", "").upper(),
        "APPLICANT_CATEGORY": d.get("APPLICANT_CATEGORY", "NON BPL"),
        "APPLICANT_BPL_NUMBER": "",
        "APPLICANT_IDENTITY_TYPE": d.get("APPLICANT_IDENTITY_TYPE", "AADHAAR CARD"),
        "APPLICANT_IDENTITY_NUMBER": id_num,
        "PERMANENT_DISTRICT_ID": dist_id,
        "PERMANENT_AREA_TYPE": "URBAN",
        "PERMANENT_CITY_BLOCK_ID": city_val,
        "PERMANENT_WARD_GP_ID": ward_val,
        "PERMANENT_VILLAGE_ID": "",
        "PERMANENT_ADDRESS": address_str,
        "PERMANENT_PINCODE": pincode_str,
        "PERMANENT_PROOF_TYPE": d.get("PERMANENT_PROOF_TYPE", "AADHAAR CARD"),
        "PERMANENT_PROOF_NUMBER": id_num,
        "CORRESPONDENCE_SAMEAS": d.get("CORRESPONDENCE_SAMEAS", "YES"),
        "CORRESPONDENCE_DISTRICT_ID": dist_id,
        "CORRESPONDENCE_AREA_TYPE": "URBAN",
        "CORRESPONDENCE_CITY_BLOCK_ID": city_val,
        "CORRESPONDENCE_WARD_GP_ID": ward_val,
        "CORRESPONDENCE_VILLAGE_ID": "",
        "CORRESPONDENCE_ADDRESS": address_str,
        "CORRESPONDENCE_PINCODE": pincode_str,
        "CONNECTION_SAMEAS": d.get("CONNECTION_SAMEAS", "PERMANENT ADDRESS"),
        "CONNECTION_APPLICATION_TYPE": d.get("CONNECTION_APPLICATION_TYPE", "NEW CONNECTION"),
        "CIN_EXISTING_CONNECTION": "",
        "NEIGHBOUR_CIN": "",
        "CONNECTION_DISTRICT_ID": dist_id,
        "CONNECTION_DIVISION_OFFICE_ID": div_id,
        "CONNECTION_SUB_DIVISION_OFFICE_ID": subdiv_id,
        "CONNECTION_AREA_TYPE": "URBAN",
        "CONNECTION_CITY_BLOCK_ID": city_val,
        "CONNECTION_WARD_GP_ID": ward_val,
        "CONNECTION_VILLAGE_ID": "",
        "CONNECTION_ADDRESS": address_str,
        "CONNECTION_PINCODE": pincode_str,
        "CONNECTION_LAT": "", "CONNECTION_LONG": "",
        "CONNECTION_TYPE": d.get("CONNECTION_TYPE", "PERMANENT"),
        "CONNECTION_TYPE_TEMPORARY_REASON": "",
        "CONNECTION_SUB_CATEGORY": d.get("CONNECTION_SUB_CATEGORY", "NON BPL"),
        "CONNECTION_PROPERTY_CATEGORY": d.get("CONNECTION_PROPERTY_CATEGORY", "PRIVATE"),
        "CONNECTION_PROPERTY_OWNERSHIP": d.get("CONNECTION_PROPERTY_OWNERSHIP", "OWNER"),
        "CONNECTION_CATEGORY": d.get("CONNECTION_CATEGORY", "RESIDENTIAL HOUSE"),
        "CONNECTION_BUILDING_HEIGHT": d.get("CONNECTION_BUILDING_HEIGHT", "3.66"),
        "CONNECTION_BUILDING_HEIGHT_UNIT": "1",
        "CONNECTION_IS_MULTISTORY": d.get("CONNECTION_IS_MULTISTORY", "NO"),
        "CONNECTION_BUILDING_TOTAL_FLOORS": d.get("CONNECTION_BUILDING_TOTAL_FLOORS", "1"),
        "CONNECTION_PIPE_SIZE": d.get("CONNECTION_PIPE_SIZE", "15"),
        "CONNECTION_ACTUAL_AREA": d.get("CONNECTION_ACTUAL_AREA", "211.33"),
        "CONNECTION_BUILTUP_AREA": d.get("CONNECTION_BUILTUP_AREA", "211.33"),
        "CONNECTION_CARPET_AREA": "",
        "IS_RAIN_WATER_HARVESTING": d.get("IS_RAIN_WATER_HARVESTING", "NO"),
        "K_NUMBER": "",
        "btnsubmit": "Submit"
    }

    opened_files, files_payload = [], {}
    try:
        for field, fpath in d.get("files", {}).items():
            if fpath and os.path.exists(fpath):
                fp = open(fpath, "rb")
                opened_files.append(fp)
                files_payload[field] = (os.path.basename(fpath), fp, "application/pdf")

        resp = await asyncio.to_thread(lambda: session.post(f"{config.BASE_URL}/Phed/WC_Form", data=form_payload, files=files_payload, timeout=120, allow_redirects=True))
        app_dir = get_app_dir(context)
        with open(os.path.join(app_dir, "last_response.html"), "w", encoding="utf-8") as f:
            f.write(resp.text)

        submitted_app_no = None
        alertbox_match = re.search(r"alertBox\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]", resp.text)
        if alertbox_match:
            title, msg_text = alertbox_match.groups()
            if any(err in msg_text.lower() for err in ["error", "fail", "try again", "invalid", "rejected"]):
                await status_msg.edit_text(f"⚠️ **Submission Rejected:**\n• {title}: {msg_text}")
                await update.effective_message.reply_text("Menu:", reply_markup=config.MAIN_KEYBOARD)
                return ConversationHandler.END
            app_no_match = re.search(r"(?:application\s*no\.?\s*is\s*)([A-Za-z0-9\/\-_]+)", msg_text, re.IGNORECASE)
            if app_no_match:
                submitted_app_no = app_no_match.group(1).rstrip(".")

        if not submitted_app_no:
            match = re.search(r"\b(PHED\/\d{4}\/\d+)\b", resp.text)
            if match:
                submitted_app_no = match.group(1)

        if submitted_app_no:
            await status_msg.edit_text(f"🎉 **Application Registered!**\n• App No: `{submitted_app_no}`\n⏳ Rendering PDF...")
            final_app_dir = get_app_dir(context, submitted_app_no)
            if app_dir != final_app_dir and os.path.exists(app_dir):
                for item in os.listdir(app_dir):
                    os.rename(os.path.join(app_dir, item), os.path.join(final_app_dir, item))
                os.rmdir(app_dir)

            clean_name = submitted_app_no.replace("/", "_")
            out_pdf = os.path.join(final_app_dir, f"Application_{clean_name}.pdf")
            view_url = f"{config.BASE_URL}/Phed/ViewForm?ID={submitted_app_no}"
            rendered = await render_application_pdf_playwright(d.get("cookies", {}), submitted_app_no, view_url, out_pdf)

            if rendered and os.path.exists(out_pdf):
                with open(out_pdf, "rb") as pdf_file:
                    await update.effective_message.reply_document(document=pdf_file, filename=f"Application_{clean_name}.pdf", caption=f"📄 App: `{submitted_app_no}`")
            await update.effective_message.reply_text("✅ Operation complete.", reply_markup=config.MAIN_KEYBOARD)
            return ConversationHandler.END

        await status_msg.edit_text("⚠️ Portal submission returned without issuing Application Number. Check `last_response.html`.")
    except Exception as err:
        logger.error(f"Execution error: {err}")
        await status_msg.edit_text(f"❌ Network/Submission Error: {err}")
    finally:
        for fp in opened_files:
            fp.close()

    await update.effective_message.reply_text("Menu:", reply_markup=config.MAIN_KEYBOARD)
    return ConversationHandler.END

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception in update handler:", exc_info=context.error)

# --- Main Entry Point --- #

def main():
    req = HTTPXRequest(connect_timeout=30.0, read_timeout=120.0, write_timeout=60.0)
    app = ApplicationBuilder().token(config.BOT_TOKEN).request(req).build()
    app.add_handler(CommandHandler("delete_all", prompt_delete_all))
    app.add_handler(CommandHandler("purge", prompt_delete_all))
    
    if app.job_queue:
        app.job_queue.run_repeating(session_keep_alive, interval=120, first=10)

    app.add_error_handler(global_error_handler)

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("newform", new_form),
            MessageHandler(filters.Regex("^📝 New Form$"), new_form),
        ],
        states={
            config.CHECK_SAVED_COOKIES: [MessageHandler(SAFE_TEXT_FILTER, check_saved_cookies)],
            config.COOKIES: [MessageHandler(SAFE_TEXT_FILTER | filters.Document.ALL, set_cookies)],
            config.CITIZEN_TYPE: [MessageHandler(SAFE_TEXT_FILTER, set_citizen_type)],
            config.HAVE_JANAADHAR: [MessageHandler(SAFE_TEXT_FILTER, set_have_janaadhar)],
            config.APPLICANT_NAME: [MessageHandler(SAFE_TEXT_FILTER, set_applicant_name)],
            config.FATHER_NAME: [MessageHandler(SAFE_TEXT_FILTER, set_father_name)],
            config.GENDER: [MessageHandler(SAFE_TEXT_FILTER, set_gender)],
            config.DOB: [MessageHandler(SAFE_TEXT_FILTER, set_dob)],
            config.MOBILE_NO: [MessageHandler(SAFE_TEXT_FILTER, set_mobile_no)],
            config.EMAIL_ID: [MessageHandler(SAFE_TEXT_FILTER, set_email_id)],
            config.CONSUMER_CATEGORY: [MessageHandler(SAFE_TEXT_FILTER, set_consumer_category)],
            config.IDENTITY_TYPE: [MessageHandler(SAFE_TEXT_FILTER, set_identity_type)],
            config.IDENTITY_NUMBER: [MessageHandler(SAFE_TEXT_FILTER, set_identity_number)],
            config.PERM_DISTRICT: [MessageHandler(SAFE_TEXT_FILTER, set_perm_district)],
            config.PERM_AREA_TYPE: [MessageHandler(SAFE_TEXT_FILTER, set_perm_area_type)],
            config.PERM_BLOCK_SELECT: [MessageHandler(SAFE_TEXT_FILTER, set_perm_block_select)],
            config.PERM_WARD_SELECT: [MessageHandler(SAFE_TEXT_FILTER, set_perm_ward_select)],
            config.PERM_ADDRESS: [MessageHandler(SAFE_TEXT_FILTER, set_perm_address)],
            config.PERM_PINCODE: [MessageHandler(SAFE_TEXT_FILTER, set_perm_pincode)],
            config.PERM_PROOF_TYPE: [MessageHandler(SAFE_TEXT_FILTER, set_perm_proof_type)],
            config.PERM_PROOF_NUM: [MessageHandler(SAFE_TEXT_FILTER, set_perm_proof_num)],
            config.CORR_SAMEAS: [MessageHandler(SAFE_TEXT_FILTER, set_corr_sameas)],
            config.CONN_LOCATION_CHOICE: [MessageHandler(SAFE_TEXT_FILTER, set_conn_location_choice)],
            config.CONN_APP_TYPE: [MessageHandler(SAFE_TEXT_FILTER, set_conn_app_type)],
            config.CONN_DISTRICT: [MessageHandler(SAFE_TEXT_FILTER, set_conn_district)],
            config.CONN_DIVISION_SELECT: [MessageHandler(SAFE_TEXT_FILTER, set_conn_division_select)],
            config.CONN_SUBDIVISION_SELECT: [MessageHandler(SAFE_TEXT_FILTER, set_conn_subdivision_select)],
            config.CONN_TYPE: [MessageHandler(SAFE_TEXT_FILTER, set_conn_type)],
            config.CONN_SUB_CAT: [MessageHandler(SAFE_TEXT_FILTER, set_conn_sub_cat)],
            config.CONN_PROP_CAT: [MessageHandler(SAFE_TEXT_FILTER, set_conn_prop_cat)],
            config.CONN_PROP_OWNERSHIP: [MessageHandler(SAFE_TEXT_FILTER, set_conn_prop_ownership)],
            config.CONN_CATEGORY: [MessageHandler(SAFE_TEXT_FILTER, set_conn_category)],
            config.CONN_BUILDING_HEIGHT: [MessageHandler(SAFE_TEXT_FILTER, set_conn_building_height)],
            config.CONN_IS_MULTISTORY: [MessageHandler(SAFE_TEXT_FILTER, set_conn_is_multistory)],
            config.CONN_TOTAL_FLOORS: [MessageHandler(SAFE_TEXT_FILTER, set_conn_total_floors)],
            config.CONN_PIPE_SIZE: [MessageHandler(SAFE_TEXT_FILTER, set_conn_pipe_size)],
            config.CONN_AREAS: [MessageHandler(SAFE_TEXT_FILTER, set_conn_areas)],
            config.RAIN_HARVESTING: [MessageHandler(SAFE_TEXT_FILTER, set_rain_harvesting)],
            config.DOC_PHOTO: [MessageHandler(filters.Document.ALL | filters.PHOTO, doc_photo_step)],
            config.DOC_PHOTO_COLLECT: [MessageHandler(filters.Document.ALL | filters.PHOTO | SAFE_TEXT_FILTER, doc_photo_collect)],
            config.DOC_ID_PROOF: [MessageHandler(filters.Document.ALL | filters.PHOTO, doc_id_proof_step)],
            config.DOC_ID_PROOF_COLLECT: [MessageHandler(filters.Document.ALL | filters.PHOTO | SAFE_TEXT_FILTER, doc_id_proof_collect)],
            config.DOC_ADDR_PROOF: [MessageHandler(filters.Document.ALL | filters.PHOTO | SAFE_TEXT_FILTER, doc_addr_proof_step)],
            config.DOC_ADDR_PROOF_COLLECT: [MessageHandler(filters.Document.ALL | filters.PHOTO | SAFE_TEXT_FILTER, doc_addr_proof_collect)],
            config.DOC_OWNERSHIP: [MessageHandler(filters.Document.ALL | filters.PHOTO, doc_ownership_step)],
            config.DOC_OWNERSHIP_COLLECT: [MessageHandler(filters.Document.ALL | filters.PHOTO | SAFE_TEXT_FILTER, doc_ownership_collect)],
            config.DOC_SITE_PLAN: [MessageHandler(filters.Document.ALL | filters.PHOTO, doc_site_plan_step)],
            config.DOC_SITE_PLAN_COLLECT: [MessageHandler(filters.Document.ALL | filters.PHOTO | SAFE_TEXT_FILTER, doc_site_plan_collect)],
            config.DOC_PROP_PHOTO: [MessageHandler(filters.Document.ALL | filters.PHOTO, doc_prop_photo_step)],
            config.DOC_PROP_PHOTO_COLLECT: [MessageHandler(filters.Document.ALL | filters.PHOTO | SAFE_TEXT_FILTER, doc_prop_photo_collect)],
            config.DOC_SUPPORTING_CHOICE: [MessageHandler(SAFE_TEXT_FILTER, doc_supporting_choice)],
            config.DOC_SUPPORTING_UPLOAD: [MessageHandler(filters.Document.ALL | filters.PHOTO, doc_supporting_upload)],
            config.DOC_SUPPORTING_COLLECT: [MessageHandler(filters.Document.ALL | filters.PHOTO | SAFE_TEXT_FILTER, doc_supporting_collect)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_handler),
            MessageHandler(filters.Regex(config.MENU_BUTTON_REGEX), cancel_handler),
        ],
    )

    pay_conv = ConversationHandler(
        entry_points=[
            CommandHandler("pay", pay_command),
            MessageHandler(filters.Regex("^💳 Pay Application$"), pay_command),
        ],
        states={
            config.PAY_APP_INPUT: [MessageHandler(SAFE_TEXT_FILTER, pay_app_input_step)],
            config.PAY_REMARKS: [MessageHandler(SAFE_TEXT_FILTER, pay_remarks_step)],
            config.PAY_DOC: [MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.Regex(config.MENU_BUTTON_REGEX), pay_doc_step)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_handler),
            MessageHandler(filters.Regex(config.MENU_BUTTON_REGEX), cancel_handler),
        ],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("login", login_command))
    app.add_handler(CommandHandler("refill", refill_command))
    app.add_handler(CommandHandler("track", track_summary_command))
    app.add_handler(CommandHandler("status", status_filter_command))
    app.add_handler(CommandHandler("listfiles", list_files_command))
    app.add_handler(CommandHandler("cancel", cancel_handler))

    app.add_handler(CallbackQueryHandler(inline_button_router))
    app.add_handler(pay_conv)
    app.add_handler(conv)

    # Master text listener for interactive TOTP, buttons, and direct file queries
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_text_dispatcher))

    print("🤖 Jalmitra Bot running in modular deployment mode...")
    app.run_polling()

if __name__ == "__main__":
    main()
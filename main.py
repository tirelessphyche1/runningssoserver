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

MENU_BUTTON_REGEX = r"^(🔑 Login SSO|📝 New Form|💳 Refill Wallet|💳 Pay Application|📊 Track Summary|💰 Pending Payment|🔧 Pending Plumber|🔍 Pending Verification|⚠️ Objections|📁 List Saved Files|❌ Cancel Operation)$"
SAFE_TEXT_FILTER = filters.TEXT & ~filters.COMMAND & ~filters.Regex(MENU_BUTTON_REGEX)

#from wallet_engine import EmitraWalletEngine
from wallet_engine import EmitraWalletRefillSession

active_refill_sessions = {}
active_refill_sessions = {}

from config import (
    BOT_TOKEN,
    BASE_URL,
    BASE_DOWNLOAD_DIR,
    COOKIE_FILE_PATH,
    DISTRICT_MAP,
)
from sso_engine import (
    SSOLoginSession,
    fetch_dashboard_status_records,
    render_application_pdf_playwright,
)

CANCEL_KEYBOARD = ReplyKeyboardMarkup(
    [["❌ Cancel Operation"]],
    resize_keyboard=True
)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Main Navigation Keyboard with quick SSO Login included
# Main Navigation Keyboard
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🔑 Login SSO", "📝 New Form"],
        ["💳 Refill Wallet", "💳 Pay Application"],
        ["📊 Track Summary", "💰 Pending Payment"],
        ["🔧 Pending Plumber", "🔍 Pending Verification"],
        ["⚠️ Objections", "📁 List Saved Files"],
    ],
    resize_keyboard=True,
)

# Active live sessions for SSO login
active_sso_sessions = {}

# Conversation States for Form Filling
(
    CHECK_SAVED_COOKIES, COOKIES, CITIZEN_TYPE, HAVE_JANAADHAR, APPLICANT_NAME,
    FATHER_NAME, GENDER, DOB, MOBILE_NO, EMAIL_ID, CONSUMER_CATEGORY,
    IDENTITY_TYPE, IDENTITY_NUMBER, PERM_DISTRICT, PERM_AREA_TYPE,
    PERM_BLOCK_SELECT, PERM_WARD_SELECT, PERM_ADDRESS, PERM_PINCODE,
    PERM_PROOF_TYPE, PERM_PROOF_NUM, CORR_SAMEAS, CONN_LOCATION_CHOICE,
    CONN_APP_TYPE, CONN_DISTRICT, CONN_DIVISION_SELECT, CONN_SUBDIVISION_SELECT,
    CONN_TYPE, CONN_SUB_CAT, CONN_PROP_CAT, CONN_PROP_OWNERSHIP, CONN_CATEGORY,
    CONN_BUILDING_HEIGHT, CONN_IS_MULTISTORY, CONN_TOTAL_FLOORS, CONN_PIPE_SIZE,
    CONN_AREAS, RAIN_HARVESTING, DOC_PHOTO, DOC_PHOTO_COLLECT, DOC_ID_PROOF,
    DOC_ID_PROOF_COLLECT, DOC_ADDR_PROOF, DOC_ADDR_PROOF_COLLECT, DOC_OWNERSHIP,
    DOC_OWNERSHIP_COLLECT, DOC_SITE_PLAN, DOC_SITE_PLAN_COLLECT, DOC_PROP_PHOTO,
    DOC_PROP_PHOTO_COLLECT, DOC_SUPPORTING_CHOICE, DOC_SUPPORTING_UPLOAD,
    DOC_SUPPORTING_COLLECT, PAY_APP_INPUT, PAY_REMARKS, PAY_DOC
) = range(56)


# --- Cookie Persistence Helpers --- #

def load_cookies_from_disk() -> dict:
    if os.path.exists(COOKIE_FILE_PATH):
        try:
            with open(COOKIE_FILE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Could not read cookies: {e}")
    return {}

def save_cookies_to_disk(cookie_dict: dict):
    try:
        with open(COOKIE_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(cookie_dict, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving cookies: {e}")

def parse_cookie_input(raw_input: str) -> dict:
    raw = raw_input.strip()
    cookies = {}
    if raw.startswith("[") and raw.endswith("]"):
        try:
            items = json.loads(raw)
            for it in items:
                n, v = it.get("name"), it.get("value")
                if n and v:
                    cookies[n] = v
            return cookies
        except Exception:
            pass
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies

def get_session(cookie_dict: dict) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Referer": f"{BASE_URL}/Phed/WC_Form",
        "Origin": "https://ssoapps.rajasthan.gov.in",
    })
    s.cookies.update(cookie_dict)
    return s

def init_session(cookie_dict: dict):
    session = get_session(cookie_dict)
    
    # 1. Short timeout and disallow redirects to login/SSO
    try:
        resp = session.get(
            f"{BASE_URL}/Phed/WC_Form",
            timeout=8,
            allow_redirects=False
        )
    except Exception as e:
        raise RuntimeError(f"Connection error: {e}")

    # If redirected (301, 302, 303, 307), the session expired and redirected to SSO
    if resp.status_code in [301, 302, 303, 307]:
        raise RuntimeError("Session expired (server redirected to login).")

    if resp.status_code != 200:
        raise RuntimeError(f"Server returned HTTP {resp.status_code}.")

    # 2. Check for soft redirects or missing verification tokens
    if "signin" in resp.url.lower() or ("login" in resp.text.lower() and "__RequestVerificationToken" not in resp.text):
        raise RuntimeError("Session expired or invalid.")

    soup = BeautifulSoup(resp.text, "html.parser")
    token_elem = soup.find("input", {"name": "__RequestVerificationToken"})
    if not token_elem:
        raise RuntimeError("Verification token missing. Session expired.")

    token = token_elem.get("value", "")
    token_cookie = "__RequestVerificationToken_L0pBTE1JVFJB0"
    if token_cookie not in session.cookies:
        for k in cookie_dict:
            if "__RequestVerificationToken" in k:
                session.cookies.set(token_cookie, cookie_dict[k])

    return session, token

async def fetch_cascade_async(cookie_dict: dict, parent_id: str, master_type: str):
    def _do():
        try:
            url = f"{BASE_URL}/Home/GetCascad"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{BASE_URL}/Phed/WC_Form",
            }
            resp = requests.get(url, params={"Id": parent_id, "mst": master_type}, headers=headers, cookies=cookie_dict, timeout=10)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return []
    return await asyncio.to_thread(_do)


async def refill_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    # Extract amount if given (e.g. /refill 500), default to 500
    amount = "500"
    if context.args:
        amount = context.args[0].strip()

    status_msg = await update.effective_message.reply_text(
        f"⏳ Opening e-Mitra and generating PhonePe QR for ₹{amount}...",
        reply_markup=ReplyKeyboardRemove()
    )

    engine = EmitraWalletEngine(chat_id)
    active_refill_sessions[chat_id] = engine

    success, qr_path, msg = await engine.generate_phonepe_qr(amount)

    if not success:
        try:
            await status_msg.delete()
        except Exception:
            pass
        await update.effective_message.reply_text(
            f"❌ Failed to generate QR:\n{msg}",
            reply_markup=MAIN_KEYBOARD
        )
        if chat_id in active_refill_sessions:
            del active_refill_sessions[chat_id]
        return

    # Send the PhonePe QR code photo directly to Telegram
    with open(qr_path, "rb") as photo:
        await update.effective_message.reply_photo(
            photo=photo,
            caption=(
                f"📱 **PhonePe QR Code — Amount: ₹{amount}**\n\n"
                "1. Open PhonePe on your phone.\n"
                "2. Scan this QR code and complete payment.\n\n"
                "⏳ Bot is waiting for payment confirmation..."
            )
        )
    if os.path.exists(qr_path):
        os.remove(qr_path)
    await status_msg.delete()

    # Wait for completion -> click Done -> Home -> Dashboard -> scrape balance
    confirmed, result = await engine.wait_for_payment_and_get_balance(timeout_seconds=180)

    if confirmed:
        await update.effective_message.reply_text(
            f"🎉 **Payment Successful!**\n\n"
            f"💰 **Current e-Mitra Wallet Balance:** `{result}`\n\n"
            "Your wallet is recharged and ready for application fees.",
            reply_markup=MAIN_KEYBOARD
        )
    else:
        await update.effective_message.reply_text(
            f"⚠️ {result}",
            reply_markup=MAIN_KEYBOARD
        )

    if chat_id in active_refill_sessions:
        del active_refill_sessions[chat_id]

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cancelled_something = False

    # 1. Abort Wallet Refill if running
    if chat_id in active_refill_sessions:
        session = active_refill_sessions[chat_id]
        await session.cancel()
        del active_refill_sessions[chat_id]
        cancelled_something = True

    # 2. Abort SSO Login if running
    if chat_id in active_sso_sessions:
        sso = active_sso_sessions[chat_id]
        await sso.close()
        del active_sso_sessions[chat_id]
        cancelled_something = True

    # 3. Clear user form data if inside a conversation
    context.user_data.clear()

    await update.effective_message.reply_text(
        "🛑 **Operation cancelled.**\nReturned to the main menu.",
        reply_markup=MAIN_KEYBOARD
    )
    return ConversationHandler.END

async def refill_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in active_refill_sessions:
        await active_refill_sessions[chat_id].close()
        del active_refill_sessions[chat_id]

    amount = "500"
    if context.args:
        amount = context.args[0].strip()

    status_msg = await update.effective_message.reply_text(
        f"⏳ Opening SSO to refill ₹{amount} in e-Mitra wallet...",
        reply_markup=ReplyKeyboardRemove()
    )

    session = EmitraWalletRefillSession(chat_id)
    try:
        captcha_path = await session.start_sso_for_refill(amount)
        active_refill_sessions[chat_id] = session

        with open(captcha_path, "rb") as f:
            await update.effective_message.reply_photo(
                photo=f,
                caption=f"🔍 Refill ₹{amount}: Enter the 6-digit CAPTCHA code above:"
            )
        if os.path.exists(captcha_path):
            os.remove(captcha_path)
        await status_msg.delete()
    except Exception as e:
        await update.effective_message.reply_text(f"⚠️ Error starting refill: {e}", reply_markup=MAIN_KEYBOARD)
        await session.close()

        
async def session_keep_alive(context: ContextTypes.DEFAULT_TYPE):
    cookies = load_cookies_from_disk()
    if not cookies or (not cookies.get(".ASPXAUTH") and not cookies.get("ASP.NET_SessionId")):
        return

    def _ping():
        url = f"{BASE_URL}/Phed/WC_Form"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Referer": f"{BASE_URL}/Phed/WC_Form"}
        try:
            resp = requests.get(url, headers=headers, cookies=cookies, timeout=15)
            if resp.cookies:
                updated = False
                for k, v in resp.cookies.get_dict().items():
                    if cookies.get(k) != v:
                        cookies[k] = v
                        updated = True
                if updated:
                    save_cookies_to_disk(cookies)
        except Exception:
            pass
    await asyncio.to_thread(_ping)

def get_app_dir(context: ContextTypes.DEFAULT_TYPE, app_no: str = None) -> str:
    applicant_name = context.user_data.get("APPLICANT_FULLNAME", "NEW_APP").replace(" ", "_")
    applicant_name = re.sub(r"[^A-Za-z0-9_]", "", applicant_name)

    if app_no:
        clean_app_no = app_no.replace("/", "_")
        target_dir = os.path.join(BASE_DOWNLOAD_DIR, f"{clean_app_no}_{applicant_name}")
    else:
        temp_id = context.user_data.get("temp_folder_id")
        if not temp_id:
            temp_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            context.user_data["temp_folder_id"] = temp_id
        target_dir = os.path.join(BASE_DOWNLOAD_DIR, f"TEMP_{temp_id}_{applicant_name}")

    os.makedirs(target_dir, exist_ok=True)
    return target_dir

def combine_images_to_pdf(image_paths: list, output_pdf_path: str) -> str:
    if not image_paths:
        return ""
    pil_images = []
    for p in image_paths:
        try:
            pil_images.append(Image.open(p).convert("RGB"))
        except Exception:
            pass

    if not pil_images:
        return ""

    first_image = pil_images[0]
    first_image.save(output_pdf_path, "PDF", resolution=100.0, save_all=True, append_images=pil_images[1:] if len(pil_images) > 1 else [])
    for p in image_paths:
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
    return output_pdf_path

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


# --- TELEGRAM SSO LOGIN HANDLERS --- #

async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in active_sso_sessions:
        await active_sso_sessions[chat_id].close()
        del active_sso_sessions[chat_id]

    await update.message.reply_text("⏳ Opening SSO Portal and generating session...", reply_markup=ReplyKeyboardRemove())
    sso = SSOLoginSession(chat_id)
    try:
        captcha_img = await sso.open_portal_and_get_captcha()
        active_sso_sessions[chat_id] = sso
        with open(captcha_img, "rb") as f:
            await update.message.reply_photo(photo=f, caption="🔍 Enter the 6-digit CAPTCHA code shown above:")
        if os.path.exists(captcha_img):
            os.remove(captcha_img)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error launching SSO login: {e}", reply_markup=MAIN_KEYBOARD)

async def handle_sso_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in active_sso_sessions:
        return False  # Not handled by SSO

    sso = active_sso_sessions[chat_id]
    text = update.message.text.strip()

    if sso.step == "CAPTCHA":
        await update.message.reply_text(f"Submitting CAPTCHA `{text}`...")
        success, next_step, new_img = await sso.submit_captcha(text)
        if success:
            await update.message.reply_text("🔑 CAPTCHA accepted! Now send your 6-digit Google Authenticator TOTP:")
        else:
            with open(new_img, "rb") as f:
                await update.message.reply_photo(photo=f, caption=f"⚠️ {next_step}")
            if os.path.exists(new_img):
                os.remove(new_img)
        return True

    elif sso.step == "TOTP":
        otp = text.replace(" ", "")
        if len(otp) != 6 or not otp.isdigit():
            await update.message.reply_text("⚠️ Please send exactly a 6-digit number.")
            return True

        await update.message.reply_text(f"Submitting TOTP `{otp}` and navigating e-Mitra to Jalmitra...")
        success, msg = await sso.complete_login_with_totp(otp)
        if success:
            await update.message.reply_text(f"🎉 {msg}", reply_markup=MAIN_KEYBOARD)
        else:
            await update.message.reply_text(f"❌ {msg}", reply_markup=MAIN_KEYBOARD)
        if chat_id in active_sso_sessions:
            del active_sso_sessions[chat_id]
        return True

    return False


# --- CORE TELEGRAM COMMANDS & NAVIGATION --- #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    inline_kb = [
        [InlineKeyboardButton("🔑 Login SSO", callback_data="btn_login"), InlineKeyboardButton("📝 New Form", callback_data="btn_newform")],
        [InlineKeyboardButton("📊 Track Summary", callback_data="btn_track"), InlineKeyboardButton("💰 Payment Files", callback_data="btn_status_payment")],
        [InlineKeyboardButton("🔧 Plumber Files", callback_data="btn_status_plumber"), InlineKeyboardButton("🔍 Verifications", callback_data="btn_status_verification")],
        [InlineKeyboardButton("⚠️ Objections", callback_data="btn_status_objection"), InlineKeyboardButton("💳 Pay Demand Note", callback_data="btn_pay")],
        [InlineKeyboardButton("📁 List Files", callback_data="btn_listfiles")],
    ]
    await update.message.reply_text(
        "👋 **Jalmitra Headless Automation & Tracker Bot**\n\nUse the buttons below to control the bot:",
        reply_markup=MAIN_KEYBOARD,
    )
    await update.message.reply_text("⚡ **Quick Action Panel:**", reply_markup=InlineKeyboardMarkup(inline_kb))

async def track_summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cookies = load_cookies_from_disk()
    if not cookies:
        await update.effective_message.reply_text("⚠️ No active cookies found. Please tap '🔑 Login SSO' first.")
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
        await update.effective_message.reply_text("⚠️ No active cookies found. Please tap '🔑 Login SSO'.")
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
    if not os.path.exists(BASE_DOWNLOAD_DIR):
        await update.effective_message.reply_text("📂 No application files have been saved yet.")
        return
    folders = [f for f in os.listdir(BASE_DOWNLOAD_DIR) if os.path.isdir(os.path.join(BASE_DOWNLOAD_DIR, f))]
    if not folders:
        await update.effective_message.reply_text("📂 No applications found in storage.")
        return
    msg = "📁 **Saved Application Files Directory:**\n\n"
    for folder in sorted(folders, reverse=True)[:10]:
        fpath = os.path.join(BASE_DOWNLOAD_DIR, folder)
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

async def menu_text_dispatcher(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
  chat_id = update.effective_chat.id
  text = update.message.text.strip() if update.message.text else ""

  # 1. Global Cancel Button
  if text in ["/cancel", "❌ Cancel Operation", "Cancel", "cancel"]:
    return await cancel_handler(update, context)

  # 2. Main Menu Actions (Forces priority over any lingering sessions)
  if text == "🔑 Login SSO":
    if chat_id in active_refill_sessions:
      await active_refill_sessions[chat_id].close()
      del active_refill_sessions[chat_id]
    context.user_data.clear()
    return await login_command(update, context)

  elif text == "📝 New Form":
    context.user_data.clear()
    return await new_form(update, context)

  elif text == "💳 Refill Wallet":
    context.user_data.clear()
    await update.message.reply_text(
        "Please type `/refill <AMOUNT>`\nExample: `/refill 500`"
    )
    return

  elif text == "💳 Pay Application":
    context.user_data.clear()
    await update.message.reply_text(
        "Please type `/pay <APPLICATION_NO>`\nExample: `/pay PHED/2026/144821`"
    )
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

  # 3. Active Refill Session (CAPTCHA / TOTP processing)
  if chat_id in active_refill_sessions:
    session = active_refill_sessions[chat_id]
    if session.step == "CAPTCHA":
      await update.message.reply_text(f"Submitting CAPTCHA `{text}`...")
      success, msg, new_img = await session.submit_captcha(text)
      if success:
        await update.message.reply_text(
            "🔑 CAPTCHA accepted! Now send your 6-digit Google Authenticator"
            " TOTP:"
        )
      else:
        if os.path.exists(new_img):
          with open(new_img, "rb") as f:
            await update.message.reply_photo(photo=f, caption=f"⚠️ {msg}")
          os.remove(new_img)
      return

    elif session.step == "TOTP":
      otp = text.replace(" ", "")
      if len(otp) != 6 or not otp.isdigit():
        await update.message.reply_text("⚠️ Please send exactly a 6-digit number.")
        return

      status_load = await update.message.reply_text(
          "⏳ Entering e-Mitra and generating PhonePe QR code..."
      )
      success, qr_path, msg = await session.submit_totp_and_get_qr(otp)

      if not success:
        await status_load.edit_text(
            f"❌ Failed: {msg}", reply_markup=MAIN_KEYBOARD
        )
        del active_refill_sessions[chat_id]
        return

      with open(qr_path, "rb") as f:
        await update.message.reply_photo(
            photo=f,
            caption=(
                f"📱 **PhonePe QR Code — Amount: ₹{session.amount}**\n\n1. Scan"
                " with PhonePe.\n2. Complete payment.\n\n⏳ Waiting for"
                " transaction confirmation..."
            ),
        )
      if os.path.exists(qr_path):
        os.remove(qr_path)
      await status_load.delete()

      confirmed, balance = await session.wait_for_payment_and_get_balance(
          timeout_seconds=180
      )
      if confirmed:
        await update.message.reply_text(
            f"🎉 **Payment Successful!**\n\n💰 **Current e-Mitra Wallet"
            f" Balance:** `{balance}`",
            reply_markup=MAIN_KEYBOARD,
        )
      else:
        await update.message.reply_text(f"⚠️ {balance}", reply_markup=MAIN_KEYBOARD)

      del active_refill_sessions[chat_id]
      return

  # 4. Active SSO Login Session (Jalmitra)
  if await handle_sso_text_messages(update, context):
    return

  # 5. File Retrieval by text query (e.g. application numbers)
  await get_files_handler(update, context)

async def get_files_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    if query.startswith("/"):
        return
    if not os.path.exists(BASE_DOWNLOAD_DIR):
        await update.message.reply_text("📂 No files found.")
        return

    clean_query = query.replace("/", "_").strip().lower()
    all_folders = [f for f in os.listdir(BASE_DOWNLOAD_DIR) if os.path.isdir(os.path.join(BASE_DOWNLOAD_DIR, f))]
    target_folder = None
    for folder in all_folders:
        if clean_query in folder.lower():
            target_folder = folder
            break

    if not target_folder:
        await update.message.reply_text(f"❌ Could not find application matching: `{query}`")
        return

    folder_path = os.path.join(BASE_DOWNLOAD_DIR, target_folder)
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


# --- PAYMENT HANDLERS --- #

async def find_viewform_and_demand_details(cookies_dict: dict, app_no: str) -> tuple[str, str]:
    from sso_engine import async_playwright
    view_url = ""
    demand_details = "Fee details not detected in active table."
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            cookie_items = [{"name": k, "value": v, "domain": "ssoapps.rajasthan.gov.in", "path": "/"} for k, v in cookies_dict.items()]
            await context.add_cookies(cookie_items)
            page = await context.new_page()
            await page.goto(f"{BASE_URL}/Home/Dashboard", wait_until="networkidle", timeout=30000)

            search_input = await page.query_selector("#dtsearch_input")
            if search_input:
                await search_input.fill(app_no)
                await page.click("#datatable_serach")
                await page.wait_for_timeout(2500)

            view_link = await page.query_selector(f"table#data_table tbody tr:has-text('{app_no}') a.viewtask_eye")
            if not view_link:
                view_link = await page.query_selector("table#data_table tbody tr a.viewtask_eye")
            if view_link:
                href = await view_link.get_attribute("href")
                if href:
                    view_url = urllib.parse.urljoin(f"{BASE_URL}/Phed/", href)

            if not view_url:
                await browser.close()
                return "", "Application row not found on portal."

            await page.goto(view_url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(1500)
            html = await page.content()
            soup = BeautifulSoup(html, "html.parser")
            demand_rows = []
            for table in soup.find_all("table"):
                if any(k in table.get_text().lower() for k in ["demand", "amount", "fee", "total", "rupees", "charge"]):
                    for r in table.find_all("tr"):
                        cols = [c.get_text(strip=True) for c in r.find_all(["td", "th"])]
                        if len(cols) >= 2:
                            demand_rows.append(" : ".join(cols))

            total_match = re.search(r"(?:Total\s*(?:Payable)?\s*Amount|Demand\s*Amount)[^\d]*([\d,]+(?:\.\d{2})?)", html, re.IGNORECASE)
            if demand_rows:
                demand_details = "\n".join(demand_rows[:12])
            elif total_match:
                demand_details = f"Total Payable Amount: ₹ {total_match.group(1)}"
            await browser.close()
    except Exception as e:
        return view_url, f"Could not parse amount: {e}"
    return view_url, demand_details

async def execute_demand_payment(cookies_dict: dict, view_url: str, remarks: str, doc_path: str = None) -> tuple[bool, str, str]:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": view_url,
        "Origin": "https://ssoapps.rajasthan.gov.in",
    })
    session.cookies.update(cookies_dict)
    resp = await asyncio.to_thread(session.get, view_url, timeout=25)
    if resp.status_code != 200:
        return False, f"Failed to open form: HTTP {resp.status_code}", ""

    soup = BeautifulSoup(resp.text, "html.parser")
    form = soup.find("form", {"id": "entryform"}) or soup.find("form", action=re.compile(r"UpdateStatus|PaymenRequest", re.IGNORECASE))
    if not form:
        return False, "Could not find payment entry form on this application page.", ""

    payload = {inp.get("name"): inp.get("value", "") for inp in form.find_all("input") if inp.get("name")}
    payload["REMARKS"] = remarks
    payload["btnsubmit8"] = "Pay Now"

    action_attr = form.get("action", "")
    btn_submit = form.find("input", {"id": "btnsubmit8"})
    if btn_submit and btn_submit.get("formaction"):
        action_attr = btn_submit.get("formaction")

    post_url = urllib.parse.urljoin(f"{BASE_URL}/Phed/", action_attr)
    files_payload, fp = {}, None
    if doc_path and os.path.exists(doc_path):
        fp = open(doc_path, "rb")
        files_payload["OTHER_DOC"] = (os.path.basename(doc_path), fp, "application/pdf")

    try:
        post_resp = await asyncio.to_thread(lambda: session.post(post_url, data=payload, files=files_payload, timeout=60, allow_redirects=True))
        alertbox_match = re.search(r"alertBox\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]", post_resp.text)
        if alertbox_match:
            title, msg_text = alertbox_match.groups()
            if any(err_kw in msg_text.lower() for err_kw in ["error", "fail", "try again", "invalid", "rejected"]):
                return False, f"{title}: {msg_text}", post_resp.url
            return True, f"{title}: {msg_text}", post_resp.url

        if post_resp.status_code == 200 and ("Receipt" in post_resp.url or "ViewForm" in post_resp.url or "Success" in post_resp.text):
            return True, "Payment request submitted successfully.", post_resp.url
        return True, "Payment request sent to department.", post_resp.url
    except Exception as e:
        return False, f"Payment error: {e}", ""
    finally:
        if fp:
            fp.close()

async def pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app_no = context.args[0].strip().upper() if context.args else None
    if not app_no:
        await update.message.reply_text("Please enter the **Application Number** to pay (e.g. `PHED/2026/144821`):", reply_markup=ReplyKeyboardRemove())
        return PAY_APP_INPUT
    return await process_pay_app(app_no, update, context)

async def pay_app_input_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_pay_app(update.message.text.strip().upper(), update, context)

async def process_pay_app(app_no: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    cookies = load_cookies_from_disk()
    if not cookies:
        await update.effective_message.reply_text("⚠️ No active cookies found. Tap '🔑 Login SSO'.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    status_msg = await update.effective_message.reply_text(f"⏳ Searching dynamic records for `{app_no}`...", reply_markup=ReplyKeyboardRemove())
    view_url, demand_info = await find_viewform_and_demand_details(cookies, app_no)
    if not view_url:
        await status_msg.edit_text(f"❌ Application `{app_no}` not found.", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    context.user_data["pay_app_no"] = app_no
    context.user_data["pay_view_url"] = view_url
    context.user_data["pay_doc_path"] = None

    await status_msg.edit_text(
        f"📋 **Application Found:** `{app_no}`\n\n💰 **Demand Note Details:**\n```\n{demand_info}\n```\n\n"
        "Please enter **Remarks** to confirm and proceed with payment (e.g. `Demand Deposited`):"
    )
    return PAY_REMARKS

async def pay_remarks_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["pay_remarks"] = update.message.text.strip()
    kb = [["Skip Document Upload"]]
    await update.message.reply_text("Upload any **Supporting Document (PDF or Photo)** for payment, or tap **Skip**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return PAY_DOC

async def pay_doc_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app_no = context.user_data.get("pay_app_no", "APP")
    clean_app = app_no.replace("/", "_")
    app_dir = os.path.join(BASE_DOWNLOAD_DIR, f"PAYMENT_{clean_app}")
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
        await status_msg.edit_text(f"❌ Payment Failed:\n{msg}", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    await status_msg.edit_text(f"🎉 **Payment Status:** {msg}\n\n⏳ Generating paid receipt / form copy...")
    receipt_pdf = os.path.join(app_dir, f"Receipt_{clean_app}.pdf")
    target_url = receipt_url if receipt_url else context.user_data["pay_view_url"]
    rendered = await render_application_pdf_playwright(cookies, app_no, target_url, receipt_pdf)

    if rendered and os.path.exists(receipt_pdf):
        with open(receipt_pdf, "rb") as f:
            await update.effective_message.reply_document(document=f, filename=f"Paid_Receipt_{clean_app}.pdf", caption=f"🧾 **Payment Receipt**\nApp No: `{app_no}`")
    await update.effective_message.reply_text("✅ Operation complete.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


# --- REGISTRATION / FORM FILL CONVERSATION --- #

async def new_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["files"] = {}
    context.user_data["photo_buffers"] = {}

    saved = load_cookies_from_disk()
    if saved and (saved.get(".ASPXAUTH") or saved.get("ASP.NET_SessionId")):
        kb = [["Use Saved Cookies", "Paste New Cookies"]]
        await update.message.reply_text(
            "Found previously saved `cookies.json`.\nWould you like to use them or provide new ones?",
            reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True)
        )
        return CHECK_SAVED_COOKIES

    await update.message.reply_text("Paste your **Cookies** (or tap '🔑 Login SSO' to log in automatically):", reply_markup=ReplyKeyboardRemove())
    return COOKIES

async def check_saved_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()
    if "Use Saved Cookies" in choice:
        c_dict = load_cookies_from_disk()
        if not c_dict:
            await update.effective_message.reply_text(
                "⚠️ No saved cookies found. Please paste fresh cookies:"
            )
            return COOKIES

        status = await update.effective_message.reply_text(
            "⏳ Validating saved session...",
            reply_markup=ReplyKeyboardRemove()
        )

        try:
            # Enforce strict 10-second ceiling on the validation thread
            session, token = await asyncio.wait_for(
                asyncio.to_thread(init_session, c_dict),
                timeout=10.0
            )
            context.user_data["cookies"] = session.cookies.get_dict()
            context.user_data["token"] = token
            
            try:
                await status.delete()
            except Exception:
                pass

            await update.effective_message.reply_text("✅ Saved session verified!")
            
            kb = [["1. Individual (Indian Citizen)", "2. Government", "3. Commercial/Industrial"]]
            await update.effective_message.reply_text(
                "Select **Citizen Type**:",
                reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True)
            )
            return CITIZEN_TYPE

        except Exception as e:
            logger.warning(f"Saved session check failed: {e}")
            try:
                await status.delete()
            except Exception:
                pass

            await update.effective_message.reply_text(
                "⚠️ **Saved session expired or invalid.**\n\n"
                "Please run `/login` to refresh your session automatically, or paste fresh cookies below:",
                reply_markup=MAIN_KEYBOARD
            )
            return COOKIES

    await update.message.reply_text(
        "Paste your fresh **Cookies** (or upload file):",
        reply_markup=ReplyKeyboardRemove()
    )
    return COOKIES

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
        await update.message.reply_text("⚠️ Invalid cookies. Could not find `.ASPXAUTH` or `ASP.NET_SessionId`. Please paste again:")
        return COOKIES

    status = await update.message.reply_text("⏳ Saving to `cookies.json` and verifying session...")
    try:
        session, token = await asyncio.to_thread(init_session, c_dict)
        final_cookies = session.cookies.get_dict()
        save_cookies_to_disk(final_cookies)
        context.user_data["cookies"] = final_cookies
        context.user_data["token"] = token
        await status.edit_text("✅ Saved to `cookies.json` & session active!")

        kb = [["1. Individual (Indian Citizen)", "2. Government", "3. Commercial/Industrial"]]
        await update.message.reply_text("Select **Citizen Type**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
        return CITIZEN_TYPE
    except Exception as e:
        await status.edit_text(f"❌ Session failed: {e}\nPlease log in again and paste fresh cookies.")
        return COOKIES

async def set_citizen_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text
    context.user_data["APPLICANT_CITIZENTYPE"] = "INDIAN CITIZEN" if "Individual" in val or "1" in val else ("GOVERNMENT" if "Government" in val else "COMMERCIAL")
    kb = [["Yes", "No"]]
    await update.message.reply_text("Do you have a **Jan Aadhaar**?", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return HAVE_JANAADHAR

async def set_have_janaadhar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["HAVE_JANAADHAR"] = "YES" if "yes" in update.message.text.strip().lower() else "NO"
    await update.message.reply_text("Enter **Applicant Full Name**:", reply_markup=ReplyKeyboardRemove())
    return APPLICANT_NAME

async def set_applicant_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["APPLICANT_FULLNAME"] = update.message.text.strip().upper()
    await update.message.reply_text("Enter **Father's / Husband's Name**:")
    return FATHER_NAME

async def set_father_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["APPLICANT_FATHER_NAME"] = update.message.text.strip().upper()
    kb = [["MALE", "FEMALE", "TRANSGENDER"]]
    await update.message.reply_text("Select **Gender**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return GENDER

async def set_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip().upper()
    context.user_data["APPLICANT_GENDER"] = val if val in ["MALE", "FEMALE", "TRANSGENDER"] else "FEMALE"
    await update.message.reply_text("Enter **Date of Birth** (`DD/MM/YYYY`):", reply_markup=ReplyKeyboardRemove())
    return DOB

async def set_dob(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["APPLICANT_DOBV"] = update.message.text.strip()
    await update.message.reply_text("Enter **10-Digit Mobile Number**:")
    return MOBILE_NO

async def set_mobile_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["APPLICANT_MOBILE"] = update.message.text.strip()
    await update.message.reply_text("Enter **Email ID** (or type `None`):")
    return EMAIL_ID

async def set_email_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    context.user_data["APPLICANT_EMAIL"] = "" if val.lower() == "none" else val.upper()
    kb = [["1. Non-BPL (General)", "2. BPL"]]
    await update.message.reply_text("Select **Consumer Category**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return CONSUMER_CATEGORY

async def set_consumer_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    context.user_data["APPLICANT_CATEGORY"] = "BPL" if "bpl" in val.lower() and "non" not in val.lower() else "NON BPL"
    kb = [["1. Identity Document", "2. Voter ID", "3. PAN Card", "4. Driving License"]]
    await update.message.reply_text("Select **Proof of Identity Type**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return IDENTITY_TYPE

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
    await update.message.reply_text("Enter the **Identity Document Number**:", reply_markup=ReplyKeyboardRemove())
    return IDENTITY_NUMBER

async def set_identity_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["APPLICANT_IDENTITY_NUMBER"] = update.message.text.strip()
    dist_lines = [f"{k}. {v}" for k, v in sorted(DISTRICT_MAP.items(), key=lambda x: int(x[0]))]
    msg = "📍 **Select Permanent District** (Type Number or Name):\n\n" + "\n".join(dist_lines[:16])
    await update.message.reply_text(msg)
    await update.message.reply_text("\n".join(dist_lines[16:]))
    return PERM_DISTRICT

async def set_perm_district(update: Update, context: ContextTypes.DEFAULT_TYPE):
    inp = update.message.text.strip().upper()
    dist_id = "12"
    for k, v in DISTRICT_MAP.items():
        if inp == k or inp == v:
            dist_id = k
            break
    context.user_data["PERMANENT_DISTRICT_ID"] = dist_id
    kb = [["1. Urban (City/Town)", "2. Rural (Block/Village)"]]
    await update.message.reply_text(
        f"District set: **{DISTRICT_MAP.get(dist_id, dist_id)}**\nSelect **Area Type**:",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True)
    )
    return PERM_AREA_TYPE

async def set_perm_area_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    inp = update.message.text
    area_type = "URBAN" if "Urban" in inp or "1" in inp else "RURAL"
    context.user_data["PERMANENT_AREA_TYPE"] = area_type

    status = await update.message.reply_text("⏳ Fetching Cities / Blocks from portal...", reply_markup=ReplyKeyboardRemove())
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
    return PERM_BLOCK_SELECT

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
    status = await update.message.reply_text("⏳ Fetching Wards / Gram Panchayats...")
    wards = await fetch_cascade_async(context.user_data["cookies"], selected_id, "WARD")
    try:
        await status.delete()
    except Exception:
        pass

    if not wards or not isinstance(wards, list):
        wards = [{"Id": "66", "Name": "WARD NO. 66"}]

    context.user_data["cached_wards"] = wards
    await send_safe_options(update, "Ward / GP", wards)
    return PERM_WARD_SELECT

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
    await update.message.reply_text("Enter **Permanent House Address / Street / Colony**:")
    return PERM_ADDRESS

async def set_perm_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["PERMANENT_ADDRESS"] = update.message.text.strip().upper()
    await update.message.reply_text("Enter **6-Digit Pincode**:")
    return PERM_PINCODE

async def set_perm_pincode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["PERMANENT_PINCODE"] = update.message.text.strip()
    kb = [["1. Proof of Identity", "2. Voter ID", "3. Electricity Bill", "4. Registry/Patta"]]
    await update.message.reply_text("Select **Proof of Address Type**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return PERM_PROOF_TYPE

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
    await update.message.reply_text("Enter **Proof of Address Document Number** (or type `same`):", reply_markup=ReplyKeyboardRemove())
    return PERM_PROOF_NUM

async def set_perm_proof_num(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    context.user_data["PERMANENT_PROOF_NUMBER"] = context.user_data["APPLICANT_IDENTITY_NUMBER"] if val.lower() == "same" else val
    kb = [["Yes", "No"]]
    await update.message.reply_text("Is the **Correspondence Address** same as Permanent Address?", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return CORR_SAMEAS

async def set_corr_sameas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["CORRESPONDENCE_SAMEAS"] = "YES" if "yes" in update.message.text.strip().lower() else "NO"
    kb = [["1. Permanent Address", "2. Correspondence Address"]]
    await update.message.reply_text("Where is the new water connection required?", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return CONN_LOCATION_CHOICE

async def set_conn_location_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text
    context.user_data["CONNECTION_SAMEAS"] = "PERMANENT ADDRESS" if "1" in val or "Permanent" in val else "CORRESPONDENCE ADDRESS"
    kb = [["1. New Connection", "2. Additional Connection"]]
    await update.message.reply_text("Select **Application Type**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return CONN_APP_TYPE

async def set_conn_app_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text
    context.user_data["CONNECTION_APPLICATION_TYPE"] = "NEW CONNECTION" if "New" in val or "1" in val else "ADDITIONAL CONNECTION"
    def_dist = context.user_data.get("PERMANENT_DISTRICT_ID", "12")
    kb = [[f"Use Same ({DISTRICT_MAP.get(def_dist, def_dist)})"]]
    await update.message.reply_text("Select **Connection District** (or type number):", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return CONN_DISTRICT

async def set_conn_district(update: Update, context: ContextTypes.DEFAULT_TYPE):
    inp = update.message.text.strip().upper()
    dist_id = context.user_data.get("PERMANENT_DISTRICT_ID", "12")
    for k, v in DISTRICT_MAP.items():
        if inp == k or inp == v:
            dist_id = k
            break
    context.user_data["CONNECTION_DISTRICT_ID"] = dist_id
    status = await update.message.reply_text(f"⏳ Fetching Division Offices for {DISTRICT_MAP.get(dist_id, dist_id)}...", reply_markup=ReplyKeyboardRemove())
    divisions = await fetch_cascade_async(context.user_data["cookies"], dist_id, "DIVISION")
    try:
        await status.delete()
    except Exception:
        pass

    if not divisions or not isinstance(divisions, list):
        divisions = [{"Id": "3006082", "Name": "CITY DIVISION-III (SOUTH), MALVIYA NAGAR, JAIPUR"}]

    context.user_data["cached_divisions"] = divisions
    await send_safe_options(update, "Division Office", divisions)
    return CONN_DIVISION_SELECT

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

    status = await update.message.reply_text("⏳ Fetching Sub-Division Offices...")
    subdivisions = await fetch_cascade_async(context.user_data["cookies"], div_id, "SUBDIVISION")
    try:
        await status.delete()
    except Exception:
        pass

    if not subdivisions or not isinstance(subdivisions, list):
        subdivisions = [{"Id": "3006124", "Name": "SUB DIVISION CITY-VI (S) JAGATPURA, JAIPUR"}]

    context.user_data["cached_subdivisions"] = subdivisions
    await send_safe_options(update, "Sub-Division Office", subdivisions)
    return CONN_SUBDIVISION_SELECT

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

    kb = [["1. Permanent", "2. Temporary"]]
    await update.message.reply_text("Select **Connection Type**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return CONN_TYPE

async def set_conn_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text
    context.user_data["CONNECTION_TYPE"] = "PERMANENT" if "Permanent" in val or "1" in val else "TEMPORARY"
    kb = [["1. Domestic", "2. Non-Domestic"]]
    await update.message.reply_text("Select **Connection Sub-Category**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return CONN_SUB_CAT

async def set_conn_sub_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["CONNECTION_SUB_CATEGORY"] = "NON BPL" if context.user_data.get("APPLICANT_CATEGORY") == "NON BPL" else "BPL"
    kb = [["1. Private", "2. Government", "3. Institutional"]]
    await update.message.reply_text("Select **Property Category**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return CONN_PROP_CAT

async def set_conn_prop_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text
    context.user_data["CONNECTION_PROPERTY_CATEGORY"] = "PRIVATE" if "Private" in val or "1" in val else ("GOVERNMENT" if "Government" in val else "INSTITUTIONAL")
    kb = [["1. Owner", "2. Tenant", "3. Leased"]]
    await update.message.reply_text("Select **Property Ownership Status**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return CONN_PROP_OWNERSHIP

async def set_conn_prop_ownership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text
    context.user_data["CONNECTION_PROPERTY_OWNERSHIP"] = "OWNER" if "Owner" in val or "1" in val else ("TENANT" if "Tenant" in val else "LEASED")
    kb = [["1. Residential House", "2. Commercial", "3. Industrial"]]
    await update.message.reply_text("Select **Connection Type Category**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return CONN_CATEGORY

async def set_conn_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text
    context.user_data["CONNECTION_CATEGORY"] = "RESIDENTIAL HOUSE" if "Residential" in val or "1" in val else "COMMERCIAL"
    await update.message.reply_text("Enter **Building Height (in Meters)** (e.g. `3.66`):", reply_markup=ReplyKeyboardRemove())
    return CONN_BUILDING_HEIGHT

async def set_conn_building_height(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["CONNECTION_BUILDING_HEIGHT"] = update.message.text.strip()
    context.user_data["CONNECTION_BUILDING_HEIGHT_UNIT"] = "1"
    kb = [["No", "Yes"]]
    await update.message.reply_text("Is the building a **Multi-Story Building**?", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return CONN_IS_MULTISTORY

async def set_conn_is_multistory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["CONNECTION_IS_MULTISTORY"] = "YES" if "yes" in update.message.text.strip().lower() else "NO"
    await update.message.reply_text("Enter **Total Floors** in Building (e.g. `1` or `2`):", reply_markup=ReplyKeyboardRemove())
    return CONN_TOTAL_FLOORS

async def set_conn_total_floors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["CONNECTION_BUILDING_TOTAL_FLOORS"] = update.message.text.strip()
    kb = [["15 mm (1/2 inch)", "20 mm (3/4 inch)", "25 mm (1 inch)"]]
    await update.message.reply_text("Select **Connection Pipe Size**:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return CONN_PIPE_SIZE

async def set_conn_pipe_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text
    context.user_data["CONNECTION_PIPE_SIZE"] = "20" if "20" in val else ("25" if "25" in val else "15")
    await update.message.reply_text("Enter **Total Plot Area** and **Built-up Area** (Sq. Mtr.) separated by space (e.g. `211.33 211.33`):", reply_markup=ReplyKeyboardRemove())
    return CONN_AREAS

async def set_conn_areas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().split()
    context.user_data["CONNECTION_ACTUAL_AREA"] = parts[0] if len(parts) > 0 else "211.33"
    context.user_data["CONNECTION_BUILTUP_AREA"] = parts[1] if len(parts) > 1 else "211.33"
    context.user_data["CONNECTION_CARPET_AREA"] = ""
    kb = [["No", "Yes"]]
    await update.message.reply_text("Is **Rainwater Harvesting Structure** constructed?", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
    return RAIN_HARVESTING

async def set_rain_harvesting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["IS_RAIN_WATER_HARVESTING"] = "YES" if "yes" in update.message.text.strip().lower() else "NO"
    await update.message.reply_text(
        "📁 **Document Uploads Begin**\n\nSend **1. Passport Size Photo of Applicant**:\n• Send **PDF** directly, or send **Photos** and tap 'Done'.",
        reply_markup=ReplyKeyboardRemove()
    )
    return DOC_PHOTO


# --- UNIVERSAL DOCUMENT HANDLING ENGINE --- #

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
        kb = [["➕ Add More Photos", "✅ Done (Convert to PDF)"]]
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
    res = await handle_document_entry(update, context, "APPLICANT_PHOTO", DOC_PHOTO_COLLECT, nxt, DOC_ID_PROOF)
    return res if res is not None else DOC_PHOTO

async def doc_photo_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nxt = "Send **2. Proof of Identity** (Identity Document / Voter ID):"
    return await handle_document_collection(update, context, "APPLICANT_PHOTO", DOC_PHOTO_COLLECT, nxt, DOC_ID_PROOF)

async def doc_id_proof_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nxt = "Send **3. Proof of Address** (Identity Document / Voter / Electricity Bill / Registry):"
    res = await handle_document_entry(update, context, "APPLICANT_IDENTITY", DOC_ID_PROOF_COLLECT, nxt, DOC_ADDR_PROOF)
    return res if res is not None else DOC_ID_PROOF

async def doc_id_proof_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nxt = "Send **3. Proof of Address** (Identity Document / Voter / Electricity Bill / Registry):"
    return await handle_document_collection(update, context, "APPLICANT_IDENTITY", DOC_ID_PROOF_COLLECT, nxt, DOC_ADDR_PROOF)

async def doc_addr_proof_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text and "same" in update.message.text.lower():
        context.user_data["files"]["ADDRESS_PROOF"] = context.user_data["files"].get("APPLICANT_IDENTITY")
        await update.message.reply_text("✅ Address proof set as Identity document.\n\nSend **4. Property Ownership Proof** (Registry / Patta):")
        return DOC_OWNERSHIP
    nxt = "Send **4. Property Ownership Proof** (Registry / Patta):"
    res = await handle_document_entry(update, context, "ADDRESS_PROOF", DOC_ADDR_PROOF_COLLECT, nxt, DOC_OWNERSHIP)
    return res if res is not None else DOC_ADDR_PROOF

async def doc_addr_proof_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nxt = "Send **4. Property Ownership Proof** (Registry / Patta):"
    return await handle_document_collection(update, context, "ADDRESS_PROOF", DOC_ADDR_PROOF_COLLECT, nxt, DOC_OWNERSHIP)

async def doc_ownership_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nxt = "Send **5. Site Plan / Map**:"
    res = await handle_document_entry(update, context, "OWNERSHIP_PROOF", DOC_OWNERSHIP_COLLECT, nxt, DOC_SITE_PLAN)
    return res if res is not None else DOC_OWNERSHIP

async def doc_ownership_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nxt = "Send **5. Site Plan / Map**:"
    return await handle_document_collection(update, context, "OWNERSHIP_PROOF", DOC_OWNERSHIP_COLLECT, nxt, DOC_SITE_PLAN)

async def doc_site_plan_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nxt = "Send **6. Photograph of Property / Front Elevation**:"
    res = await handle_document_entry(update, context, "SITE_PLAN", DOC_SITE_PLAN_COLLECT, nxt, DOC_PROP_PHOTO)
    return res if res is not None else DOC_SITE_PLAN

async def doc_site_plan_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nxt = "Send **6. Photograph of Property / Front Elevation**:"
    return await handle_document_collection(update, context, "SITE_PLAN", DOC_SITE_PLAN_COLLECT, nxt, DOC_PROP_PHOTO)

async def doc_prop_photo_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["Done - Submit Form"], ["Neighbor Water Bill", "Electricity Bill"], ["Rain Harvesting Certificate", "Other Document"]]
    nxt = "Do you want to upload any **Optional Supporting Documents**?"
    res = await handle_document_entry(update, context, "PROPERTY_PHOTO", DOC_PROP_PHOTO_COLLECT, nxt, DOC_SUPPORTING_CHOICE)
    if res == DOC_SUPPORTING_CHOICE:
        await update.message.reply_text(nxt, reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return res if res is not None else DOC_PROP_PHOTO

async def doc_prop_photo_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["Done - Submit Form"], ["Neighbor Water Bill", "Electricity Bill"], ["Rain Harvesting Certificate", "Other Document"]]
    nxt = "Do you want to upload any **Optional Supporting Documents**?"
    res = await handle_document_collection(update, context, "PROPERTY_PHOTO", DOC_PROP_PHOTO_COLLECT, nxt, DOC_SUPPORTING_CHOICE)
    if res == DOC_SUPPORTING_CHOICE:
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
    await update.message.reply_text(f"Upload file for **{choice}** (PDF or Photos):", reply_markup=ReplyKeyboardRemove())
    return DOC_SUPPORTING_UPLOAD

async def doc_supporting_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get("current_supporting_field", "OTHER_DOC")
    res = await handle_document_entry(update, context, field, DOC_SUPPORTING_COLLECT, "Document recorded.", DOC_SUPPORTING_CHOICE)
    if res == DOC_SUPPORTING_CHOICE:
        kb = [["Done - Submit Form"], ["Neighbor Water Bill", "Electricity Bill"], ["Rain Harvesting Certificate", "Other Document"]]
        await update.message.reply_text("Add another supporting document or finish:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return res if res is not None else DOC_SUPPORTING_UPLOAD

async def doc_supporting_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get("current_supporting_field", "OTHER_DOC")
    res = await handle_document_collection(update, context, field, DOC_SUPPORTING_COLLECT, "Document recorded.", DOC_SUPPORTING_CHOICE)
    if res == DOC_SUPPORTING_CHOICE:
        kb = [["Done - Submit Form"], ["Neighbor Water Bill", "Electricity Bill"], ["Rain Harvesting Certificate", "Other Document"]]
        await update.message.reply_text("Add another supporting document or finish:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return res


# --- FORM SUBMISSION ENGINE --- #

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
        "Referer": f"{BASE_URL}/Phed/WC_Form",
        "Origin": "https://ssoapps.rajasthan.gov.in",
    })
    session.cookies.update(d.get("cookies", {}))

    fresh_token = d.get("token", "")
    try:
        def _get_token():
            r = session.get(f"{BASE_URL}/Phed/WC_Form", timeout=20)
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

        resp = await asyncio.to_thread(lambda: session.post(f"{BASE_URL}/Phed/WC_Form", data=form_payload, files=files_payload, timeout=120, allow_redirects=True))
        app_dir = get_app_dir(context)
        with open(os.path.join(app_dir, "last_response.html"), "w", encoding="utf-8") as f:
            f.write(resp.text)

        soup = BeautifulSoup(resp.text, "html.parser")
        submitted_app_no = None

        alertbox_match = re.search(r"alertBox\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]", resp.text)
        if alertbox_match:
            title, msg_text = alertbox_match.groups()
            if any(err in msg_text.lower() for err in ["error", "fail", "try again", "invalid", "rejected"]):
                await status_msg.edit_text(f"⚠️ **Submission Rejected:**\n• {title}: {msg_text}")
                await update.effective_message.reply_text("Menu:", reply_markup=MAIN_KEYBOARD)
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
            view_url = f"{BASE_URL}/Phed/ViewForm?ID={submitted_app_no}"
            rendered = await render_application_pdf_playwright(d.get("cookies", {}), submitted_app_no, view_url, out_pdf)

            if rendered and os.path.exists(out_pdf):
                with open(out_pdf, "rb") as pdf_file:
                    await update.effective_message.reply_document(document=pdf_file, filename=f"Application_{clean_name}.pdf", caption=f"📄 App: `{submitted_app_no}`")
            await update.effective_message.reply_text("✅ Operation complete.", reply_markup=MAIN_KEYBOARD)
            return ConversationHandler.END

        await status_msg.edit_text("⚠️ Portal submission returned without issuing Application Number. Check `last_response.html`.")
    except Exception as err:
        logger.error(f"Execution error: {err}")
        await status_msg.edit_text(f"❌ Network/Submission Error: {err}")
    finally:
        for fp in opened_files:
            fp.close()

    await update.effective_message.reply_text("Menu:", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Operation cancelled.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception in update handler:", exc_info=context.error)


# --- MAIN RUNNER --- #

def main():
    req = HTTPXRequest(connect_timeout=30.0, read_timeout=120.0, write_timeout=60.0)
    app = ApplicationBuilder().token(BOT_TOKEN).request(req).build()

    if app.job_queue:
        app.job_queue.run_repeating(session_keep_alive, interval=120, first=10)

    app.add_error_handler(global_error_handler)

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("newform", new_form),
            MessageHandler(filters.Regex("^📝 New Form$"), new_form),
        ],
        states={
            CHECK_SAVED_COOKIES: [MessageHandler(SAFE_TEXT_FILTER, check_saved_cookies)],
            COOKIES: [MessageHandler(SAFE_TEXT_FILTER | filters.Document.ALL, set_cookies)],
            CITIZEN_TYPE: [MessageHandler(SAFE_TEXT_FILTER, set_citizen_type)],
            HAVE_JANAADHAR: [MessageHandler(SAFE_TEXT_FILTER, set_have_janaadhar)],
            APPLICANT_NAME: [MessageHandler(SAFE_TEXT_FILTER, set_applicant_name)],
            FATHER_NAME: [MessageHandler(SAFE_TEXT_FILTER, set_father_name)],
            GENDER: [MessageHandler(SAFE_TEXT_FILTER, set_gender)],
            DOB: [MessageHandler(SAFE_TEXT_FILTER, set_dob)],
            MOBILE_NO: [MessageHandler(SAFE_TEXT_FILTER, set_mobile_no)],
            EMAIL_ID: [MessageHandler(SAFE_TEXT_FILTER, set_email_id)],
            CONSUMER_CATEGORY: [MessageHandler(SAFE_TEXT_FILTER, set_consumer_category)],
            IDENTITY_TYPE: [MessageHandler(SAFE_TEXT_FILTER, set_identity_type)],
            IDENTITY_NUMBER: [MessageHandler(SAFE_TEXT_FILTER, set_identity_number)],
            PERM_DISTRICT: [MessageHandler(SAFE_TEXT_FILTER, set_perm_district)],
            PERM_AREA_TYPE: [MessageHandler(SAFE_TEXT_FILTER, set_perm_area_type)],
            PERM_BLOCK_SELECT: [MessageHandler(SAFE_TEXT_FILTER, set_perm_block_select)],
            PERM_WARD_SELECT: [MessageHandler(SAFE_TEXT_FILTER, set_perm_ward_select)],
            PERM_ADDRESS: [MessageHandler(SAFE_TEXT_FILTER, set_perm_address)],
            PERM_PINCODE: [MessageHandler(SAFE_TEXT_FILTER, set_perm_pincode)],
            PERM_PROOF_TYPE: [MessageHandler(SAFE_TEXT_FILTER, set_perm_proof_type)],
            PERM_PROOF_NUM: [MessageHandler(SAFE_TEXT_FILTER, set_perm_proof_num)],
            CORR_SAMEAS: [MessageHandler(SAFE_TEXT_FILTER, set_corr_sameas)],
            CONN_LOCATION_CHOICE: [MessageHandler(SAFE_TEXT_FILTER, set_conn_location_choice)],
            CONN_APP_TYPE: [MessageHandler(SAFE_TEXT_FILTER, set_conn_app_type)],
            CONN_DISTRICT: [MessageHandler(SAFE_TEXT_FILTER, set_conn_district)],
            CONN_DIVISION_SELECT: [MessageHandler(SAFE_TEXT_FILTER, set_conn_division_select)],
            CONN_SUBDIVISION_SELECT: [MessageHandler(SAFE_TEXT_FILTER, set_conn_subdivision_select)],
            CONN_TYPE: [MessageHandler(SAFE_TEXT_FILTER, set_conn_type)],
            CONN_SUB_CAT: [MessageHandler(SAFE_TEXT_FILTER, set_conn_sub_cat)],
            CONN_PROP_CAT: [MessageHandler(SAFE_TEXT_FILTER, set_conn_prop_cat)],
            CONN_PROP_OWNERSHIP: [MessageHandler(SAFE_TEXT_FILTER, set_conn_prop_ownership)],
            CONN_CATEGORY: [MessageHandler(SAFE_TEXT_FILTER, set_conn_category)],
            CONN_BUILDING_HEIGHT: [MessageHandler(SAFE_TEXT_FILTER, set_conn_building_height)],
            CONN_IS_MULTISTORY: [MessageHandler(SAFE_TEXT_FILTER, set_conn_is_multistory)],
            CONN_TOTAL_FLOORS: [MessageHandler(SAFE_TEXT_FILTER, set_conn_total_floors)],
            CONN_PIPE_SIZE: [MessageHandler(SAFE_TEXT_FILTER, set_conn_pipe_size)],
            CONN_AREAS: [MessageHandler(SAFE_TEXT_FILTER, set_conn_areas)],
            RAIN_HARVESTING: [MessageHandler(SAFE_TEXT_FILTER, set_rain_harvesting)],
            # Document collection steps remain unchanged
            DOC_PHOTO: [MessageHandler(filters.Document.ALL | filters.PHOTO, doc_photo_step)],
            DOC_PHOTO_COLLECT: [MessageHandler(filters.Document.ALL | filters.PHOTO | SAFE_TEXT_FILTER, doc_photo_collect)],
            DOC_ID_PROOF: [MessageHandler(filters.Document.ALL | filters.PHOTO, doc_id_proof_step)],
            DOC_ID_PROOF_COLLECT: [MessageHandler(filters.Document.ALL | filters.PHOTO | SAFE_TEXT_FILTER, doc_id_proof_collect)],
            DOC_ADDR_PROOF: [MessageHandler(filters.Document.ALL | filters.PHOTO | SAFE_TEXT_FILTER, doc_addr_proof_step)],
            DOC_ADDR_PROOF_COLLECT: [MessageHandler(filters.Document.ALL | filters.PHOTO | SAFE_TEXT_FILTER, doc_addr_proof_collect)],
            DOC_OWNERSHIP: [MessageHandler(filters.Document.ALL | filters.PHOTO, doc_ownership_step)],
            DOC_OWNERSHIP_COLLECT: [MessageHandler(filters.Document.ALL | filters.PHOTO | SAFE_TEXT_FILTER, doc_ownership_collect)],
            DOC_SITE_PLAN: [MessageHandler(filters.Document.ALL | filters.PHOTO, doc_site_plan_step)],
            DOC_SITE_PLAN_COLLECT: [MessageHandler(filters.Document.ALL | filters.PHOTO | SAFE_TEXT_FILTER, doc_site_plan_collect)],
            DOC_PROP_PHOTO: [MessageHandler(filters.Document.ALL | filters.PHOTO, doc_prop_photo_step)],
            DOC_PROP_PHOTO_COLLECT: [MessageHandler(filters.Document.ALL | filters.PHOTO | SAFE_TEXT_FILTER, doc_prop_photo_collect)],
            DOC_SUPPORTING_CHOICE: [MessageHandler(SAFE_TEXT_FILTER, doc_supporting_choice)],
            DOC_SUPPORTING_UPLOAD: [MessageHandler(filters.Document.ALL | filters.PHOTO, doc_supporting_upload)],
            DOC_SUPPORTING_COLLECT: [MessageHandler(filters.Document.ALL | filters.PHOTO | SAFE_TEXT_FILTER, doc_supporting_collect)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_handler),
            MessageHandler(filters.Regex(MENU_BUTTON_REGEX), cancel_handler)
        ],
    )

    pay_conv = ConversationHandler(
        entry_points=[
            CommandHandler("pay", pay_command),
            MessageHandler(filters.Regex("^💳 Pay Application$"), pay_command),
        ],
        states={
            PAY_APP_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, pay_app_input_step)],
            PAY_REMARKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, pay_remarks_step)],
            PAY_DOC: [MessageHandler(filters.ALL & ~filters.COMMAND, pay_doc_step)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("login", login_command))
    app.add_handler(CommandHandler("track", track_summary_command))
    app.add_handler(CommandHandler("status", status_filter_command))
    app.add_handler(CommandHandler("listfiles", list_files_command))
    app.add_handler(CallbackQueryHandler(inline_button_router))
    app.add_handler(pay_conv)
    app.add_handler(conv)
    app.add_handler(CommandHandler("refill", refill_command))
    # General text dispatcher handles menu buttons, SSO replies, and file lookups
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_text_dispatcher))

    print("🤖 Jalmitra Bot is online with SSO Authentication & Clean Modular Architecture.")
    app.run_polling()

if __name__ == "__main__":
    main()
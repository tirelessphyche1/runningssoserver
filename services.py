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
from telegram.ext import ContextTypes
import config
import shutil

logger = logging.getLogger(__name__)

def load_cookies_from_disk() -> dict:
    if os.path.exists(config.COOKIE_FILE_PATH):
        try:
            with open(config.COOKIE_FILE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Could not read cookies: {e}")
    return {}

def save_cookies_to_disk(cookie_dict: dict):
    try:
        with open(config.COOKIE_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(cookie_dict, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving cookies: {e}")

def purge_all_storage_and_cookies() -> tuple[int, int]:
    """
    Deletes cookies.json and recursively clears all files/folders inside HeadlessUploads.
    Returns: (files_deleted_count, folders_deleted_count)
    """
    files_deleted = 0
    folders_deleted = 0

    # 1. Delete cookies file
    if os.path.exists(config.COOKIE_FILE_PATH):
        try:
            os.remove(config.COOKIE_FILE_PATH)
            files_deleted += 1
        except Exception as e:
            logger.error(f"Error removing {config.COOKIE_FILE_PATH}: {e}")

    # 2. Delete contents inside HeadlessUploads directory
    if os.path.exists(config.BASE_DOWNLOAD_DIR):
        for item in os.listdir(config.BASE_DOWNLOAD_DIR):
            item_path = os.path.join(config.BASE_DOWNLOAD_DIR, item)
            try:
                if os.path.isfile(item_path) or os.path.islink(item_path):
                    os.remove(item_path)
                    files_deleted += 1
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                    folders_deleted += 1
            except Exception as e:
                logger.error(f"Failed to delete {item_path}: {e}")

    # Re-create empty storage folder just in case
    os.makedirs(config.BASE_DOWNLOAD_DIR, exist_ok=True)

    return files_deleted, folders_deleted

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
        "Referer": f"{config.BASE_URL}/Phed/WC_Form",
        "Origin": "https://ssoapps.rajasthan.gov.in",
    })
    s.cookies.update(cookie_dict)
    return s

def init_session(cookie_dict: dict):
    session = get_session(cookie_dict)
    try:
        resp = session.get(f"{config.BASE_URL}/Phed/WC_Form", timeout=8, allow_redirects=False)
    except Exception as e:
        raise RuntimeError(f"Connection error: {e}")

    if resp.status_code in [301, 302, 303, 307]:
        raise RuntimeError("Session expired (server redirected to login).")
    if resp.status_code != 200:
        raise RuntimeError(f"Server returned HTTP {resp.status_code}.")

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
            url = f"{config.BASE_URL}/Home/GetCascad"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{config.BASE_URL}/Phed/WC_Form",
            }
            resp = requests.get(url, params={"Id": parent_id, "mst": master_type}, headers=headers, cookies=cookie_dict, timeout=10)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return []
    return await asyncio.to_thread(_do)

async def session_keep_alive(context: ContextTypes.DEFAULT_TYPE):
    cookies = load_cookies_from_disk()
    if not cookies or (not cookies.get(".ASPXAUTH") and not cookies.get("ASP.NET_SessionId")):
        return

    def _ping():
        url = f"{config.BASE_URL}/Phed/WC_Form"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Referer": f"{config.BASE_URL}/Phed/WC_Form"}
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
        target_dir = os.path.join(config.BASE_DOWNLOAD_DIR, f"{clean_app_no}_{applicant_name}")
    else:
        temp_id = context.user_data.get("temp_folder_id")
        if not temp_id:
            temp_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            context.user_data["temp_folder_id"] = temp_id
        target_dir = os.path.join(config.BASE_DOWNLOAD_DIR, f"TEMP_{temp_id}_{applicant_name}")

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

async def find_viewform_and_demand_details(cookies_dict: dict, app_no: str) -> tuple[str, str]:
    from playwright.async_api import async_playwright
    CHROMIUM_ARGS = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
    view_url = ""
    demand_details = "Fee details not detected in active table."
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
            context = await browser.new_context()
            cookie_items = [{"name": k, "value": v, "domain": "ssoapps.rajasthan.gov.in", "path": "/"} for k, v in cookies_dict.items()]
            await context.add_cookies(cookie_items)
            page = await context.new_page()
            await page.goto(f"{config.BASE_URL}/Home/Dashboard", wait_until="domcontentloaded", timeout=30000)

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
                    view_url = urllib.parse.urljoin(f"{config.BASE_URL}/Phed/", href)

            if not view_url:
                await browser.close()
                return "", "Application row not found on portal."

            await page.goto(view_url, wait_until="domcontentloaded", timeout=30000)
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

    post_url = urllib.parse.urljoin(f"{config.BASE_URL}/Phed/", action_attr)
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
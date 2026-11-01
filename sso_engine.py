import os
import re
import json
import logging
import urllib.parse
from playwright.async_api import async_playwright
import ddddocr
from config import (
    SSO_USER,
    SSO_PASS,
    SSO_LOGIN_URL,
    BASE_URL,
    COOKIE_FILE_PATH,
    BASE_DOWNLOAD_DIR,
)

logger = logging.getLogger(__name__)
ocr = ddddocr.DdddOcr(show_ad=False)

CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]

class SSOLoginSession:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.p = None
        self.browser = None
        self.context = None
        self.page = None
        self.step = "TOTP"

    async def auto_login_and_request_totp(self) -> tuple[bool, str]:
        """Automatically fills credentials, solves CAPTCHA with OCR, and prompts for TOTP."""
        try:
            self.p = await async_playwright().start()
            self.browser = await self.p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
            self.context = await self.browser.new_context(
                viewport={"width": 1366, "height": 768},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            self.page = await self.context.new_page()
            await self.page.goto(SSO_LOGIN_URL, wait_until="networkidle")

            await self.page.get_by_role("textbox", name="Digital Identity (SSOID/").fill(SSO_USER)
            await self.page.get_by_role("textbox", name="Password").fill(SSO_PASS)

            max_attempts = 4
            for attempt in range(1, max_attempts + 1):
                logger.info(f"Auto-solving CAPTCHA (attempt {attempt}/{max_attempts})...")
                captcha_loc = self.page.locator("#imgCaptcha")
                if await captcha_loc.count() == 0:
                    captcha_loc = self.page.locator('img[id*="Captcha"]')

                await captcha_loc.wait_for(state="visible", timeout=10000)
                img_bytes = await captcha_loc.screenshot()

                captcha_code = ocr.classification(img_bytes).strip()
                captcha_code = re.sub(r"[^A-Za-z0-9]", "", captcha_code)
                logger.info(f"Predicted CAPTCHA: {captcha_code}")

                captcha_box = self.page.get_by_role("textbox", name="Enter 6-digit captcha code")
                await captcha_box.fill("")
                await captcha_box.fill(captcha_code)
                await self.page.get_by_role("button", name="Login").click()
                await self.page.wait_for_timeout(2500)

                try:
                    already_logged = self.page.get_by_role("checkbox", name="You are already logged-in (IP")
                    if await already_logged.is_visible():
                        await already_logged.check()
                        pass_box = self.page.get_by_role("textbox", name="Password")
                        if await pass_box.count() > 0:
                            await pass_box.fill(SSO_PASS)
                        await self.page.get_by_role("button", name="Login").click()
                        await self.page.wait_for_timeout(2500)
                except Exception:
                    pass

                try:
                    first_spin = self.page.get_by_role("spinbutton").first
                    await first_spin.wait_for(state="visible", timeout=4000)
                    logger.info("CAPTCHA accepted. Ready for TOTP.")
                    return True, "Ready for TOTP"
                except Exception:
                    logger.warning("CAPTCHA failed or refreshed; retrying...")
                    await self.page.wait_for_timeout(1000)

            return False, "Failed to auto-solve CAPTCHA after 4 attempts."

        except Exception as e:
            logger.error(f"Error in auto_login_and_request_totp: {e}", exc_info=True)
            await self.close()
            return False, str(e)

    async def complete_login_with_totp(self, totp_code: str) -> tuple[bool, str]:
        """Submits TOTP, traverses e-Mitra to PHED, and dumps all session cookies."""
        try:
            spins = self.page.get_by_role("spinbutton")
            for idx, digit in enumerate(totp_code):
                await spins.nth(idx).fill(digit)

            await self.page.get_by_role("button", name="टीओटीपी मान्य करें").click()
            await self.page.wait_for_timeout(4000)

            error_modal = self.page.locator(".swal2-modal, .alert, #lblTOTPMessage, :text('अमान्य')")
            if await error_modal.count() > 0 and await error_modal.first.is_visible():
                ok_btn = self.page.get_by_role("button", name="OK")
                if await ok_btn.count() > 0:
                    await ok_btn.click()
                return False, "Google Authenticator code expired or invalid."

            if await self.page.get_by_role("spinbutton").count() > 0:
                return False, "TOTP rejected by portal. Send the latest 6-digit code."

            try:
                async with self.page.expect_popup(timeout=8000) as popup_info:
                    await self.page.get_by_role("link", name="e-Mitra").click()
                emitra_page = await popup_info.value
            except Exception:
                emitra_page = self.page

            await emitra_page.wait_for_load_state("domcontentloaded")
            await emitra_page.wait_for_timeout(3000)

            close_icon = emitra_page.get_by_role("button", name="")
            if await close_icon.count() > 0:
                try:
                    await close_icon.click(force=True, timeout=3000)
                except Exception:
                    await close_icon.dispatch_event("click")
                await emitra_page.wait_for_timeout(1000)

            ack_btn = emitra_page.get_by_role("button", name="I have read and acknowledge")
            if await ack_btn.count() > 0:
                try:
                    await ack_btn.click(force=True, timeout=5000)
                except Exception:
                    await ack_btn.dispatch_event("click")
                await emitra_page.wait_for_timeout(1500)

            await emitra_page.locator("a").filter(has_text="Services").click(force=True)
            await emitra_page.locator("a").filter(has_text="Avail Service").click(force=True)
            await emitra_page.locator("a").filter(has_text="Utility").click(force=True)
            await emitra_page.wait_for_timeout(2000)

            await emitra_page.get_by_text("Select Service").click(force=True)
            search_box = emitra_page.get_by_role("textbox")
            await search_box.fill("phed")
            await emitra_page.wait_for_timeout(1000)
            await emitra_page.get_by_text("PHED - APPLY FOR NEW DOMESTIC").click(force=True)

            await emitra_page.get_by_role("button", name="Search").click(force=True)
            await emitra_page.wait_for_timeout(1500)

            yes_btn = emitra_page.get_by_role("button", name="Yes")
            if await yes_btn.count() > 0:
                await yes_btn.click(force=True)
                await emitra_page.wait_for_timeout(2000)

            await emitra_page.get_by_role("link", name="New Application (Domestic)").click(force=True)
            await emitra_page.wait_for_load_state("domcontentloaded")
            await emitra_page.wait_for_timeout(3000)

            all_cookies = await self.context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in all_cookies}
            with open(COOKIE_FILE_PATH, "w", encoding="utf-8") as f:
                json.dump(cookie_dict, f, indent=4)

            return True, "Login successful! Session cookies saved to cookies.json."

        except Exception as e:
            logger.error(f"Error during SSO navigation: {e}", exc_info=True)
            return False, f"Navigation Error: {str(e)}"
        finally:
            await self.close()

    async def close(self):
        try:
            if self.browser:
                await self.browser.close()
            if self.p:
                await self.p.stop()
        except Exception:
            pass

async def fetch_dashboard_status_records(cookies_dict: dict, max_pages: int = 5) -> dict:
    categorized = {
        "PLUMBER": [], "PAYMENT": [], "VERIFICATION": [], "OBJECTION": [], "COMPLETED": [], "OTHER": []
    }
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
            context = await browser.new_context()
            cookie_items = [
                {"name": k, "value": v, "domain": "ssoapps.rajasthan.gov.in", "path": "/"}
                for k, v in cookies_dict.items()
            ]
            await context.add_cookies(cookie_items)
            page = await context.new_page()
            await page.goto(f"{BASE_URL}/Home/Dashboard", wait_until="domcontentloaded", timeout=30000)

            pagesize_dropdown = await page.query_selector("#selectpagesize")
            if pagesize_dropdown:
                await pagesize_dropdown.select_option("100")
                await page.wait_for_timeout(2000)

            current_page = 1
            seen_app_nos = set()

            while current_page <= max_pages:
                rows = await page.query_selector_all("table#data_table tbody tr")
                for row in rows:
                    cols = await row.query_selector_all("td")
                    if len(cols) < 10:
                        continue

                    app_cell_text = await cols[2].inner_text()
                    parts = app_cell_text.strip().split("\n")
                    app_no = parts[0].strip()
                    app_date = parts[1].strip() if len(parts) > 1 else ""

                    if not app_no or app_no in seen_app_nos:
                        continue
                    seen_app_nos.add(app_no)

                    name = (await cols[8].inner_text()).strip()
                    status_raw = (await cols[9].inner_text()).strip()
                    status_upper = status_raw.upper()
                    status_date = (await cols[10].inner_text()).strip() if len(cols) > 10 else ""

                    link_tag = await row.query_selector("a.viewtask_eye")
                    href = await link_tag.get_attribute("href") if link_tag else ""
                    view_url = urllib.parse.urljoin(f"{BASE_URL}/Phed/", href) if href else ""

                    rec = {
                        "app_no": app_no, "date": app_date, "name": name,
                        "status": status_raw, "status_date": status_date, "url": view_url
                    }

                    if "PLUMBER" in status_upper:
                        categorized["PLUMBER"].append(rec)
                    elif "DEMAND" in status_upper or "PAYMENT" in status_upper:
                        categorized["PAYMENT"].append(rec)
                    elif any(k in status_upper for k in ["JEN", "AEN", "EE", "VERIFICATION", "SCRUTINY", "INSPECTION"]):
                        categorized["VERIFICATION"].append(rec)
                    elif any(k in status_upper for k in ["OBJECT", "REVERT", "REJECT", "DEFECT", "CORRECTION"]):
                        categorized["OBJECTION"].append(rec)
                    elif any(k in status_upper for k in ["CERTIFICATE", "CLOSED", "SANCTION", "COMPLETE", "ISSUED"]):
                        categorized["COMPLETED"].append(rec)
                    else:
                        categorized["OTHER"].append(rec)

                next_page_idx = current_page + 1
                has_next = await page.query_selector(f"a[onclick='getdataforpage({next_page_idx})']")
                if not has_next or current_page >= max_pages:
                    break
                await page.evaluate(f"getdataforpage({next_page_idx})")
                await page.wait_for_timeout(2500)
                current_page += 1

            await browser.close()
    except Exception as e:
        logger.error(f"Error fetching dashboard records: {e}")
    return categorized

async def render_application_pdf_playwright(cookies_dict: dict, app_no: str, view_url: str, output_path: str) -> bool:
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
            context = await browser.new_context()
            cookie_items = [
                {"name": k, "value": v, "domain": "ssoapps.rajasthan.gov.in", "path": "/"}
                for k, v in cookies_dict.items()
            ]
            await context.add_cookies(cookie_items)
            page = await context.new_page()

            target_url = view_url
            if not target_url:
                await page.goto(f"{BASE_URL}/Home/Dashboard", wait_until="domcontentloaded", timeout=30000)
                search_input = await page.query_selector("#dtsearch_input")
                if search_input:
                    await search_input.fill(app_no)
                    await page.click("#datatable_serach")
                    await page.wait_for_timeout(2000)
                view_link = await page.query_selector(f"tr:has-text('{app_no}') a.viewtask_eye")
                if not view_link:
                    view_link = await page.query_selector("table#data_table tbody tr a.viewtask_eye")
                if view_link:
                    href = await view_link.get_attribute("href")
                    if href:
                        target_url = urllib.parse.urljoin(f"{BASE_URL}/Phed/", href)

            if not target_url:
                await browser.close()
                return False

            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1500)
            await page.pdf(
                path=output_path, format="A4", print_background=True,
                margin={"top": "15mm", "bottom": "15mm", "left": "10mm", "right": "10mm"}
            )
            await browser.close()
            return True
    except Exception as e:
        logger.error(f"PDF rendering failed: {e}")
        return False
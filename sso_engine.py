import os
import re
import json
import logging
import urllib.parse
import ddddocr
from playwright.async_api import async_playwright
from config import (
    SSO_USER,
    SSO_PASS,
    SSO_LOGIN_URL,
    BASE_URL,
    COOKIE_FILE_PATH,
    BASE_DOWNLOAD_DIR,
)

logger = logging.getLogger(__name__)

class SSOLoginSession:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.p = None
        self.browser = None
        self.context = None
        self.page = None
        self.step = "CAPTCHA"

    async def open_portal_and_get_captcha(self) -> str:
        self.p = await async_playwright().start()
        self.browser = await self.p.chromium.launch(headless=True)
        self.context = await self.browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        self.page = await self.context.new_page()
        await self.page.goto(SSO_LOGIN_URL, wait_until="networkidle")

        # Fill credentials
        await self.page.get_by_role("textbox", name="Digital Identity (SSOID/").fill(SSO_USER)
        await self.page.get_by_role("textbox", name="Password").fill(SSO_PASS)

        # Grab CAPTCHA
        # Grab CAPTCHA
        captcha_loc = self.page.locator("#imgCaptcha")
        if await captcha_loc.count() == 0:
            captcha_loc = self.page.locator('img[id*="Captcha"]')

        captcha_path = os.path.join(BASE_DOWNLOAD_DIR, f"captcha_{self.chat_id}.png")
        await captcha_loc.screenshot(path=captcha_path)
        return captcha_path

    async def submit_captcha(self, captcha_text: str) -> tuple[bool, str, str]:
        """Returns (success, next_state_or_error_msg, new_captcha_path_if_retry)"""
        try:
            captcha_box = self.page.get_by_role("textbox", name="Enter 6-digit captcha code")
            await captcha_box.fill(captcha_text)
            await self.page.get_by_role("button", name="Login").click()

            # Handle already logged in session dialog if it appears
            try:
                already_logged = self.page.get_by_role("checkbox", name="You are already logged-in (IP")
                await already_logged.wait_for(state="visible", timeout=3000)
                if await already_logged.is_visible():
                    await already_logged.check()
                    pass_box = self.page.get_by_role("textbox", name="Password")
                    if await pass_box.count() > 0:
                        await pass_box.fill(SSO_PASS)
                    await self.page.get_by_role("button", name="Login").click()
            except Exception:
                pass  # No concurrent session dialog appeared

            # Proactively wait for TOTP spinbuttons to render (up to 12 seconds)
            totp_ready = False
            try:
                first_spin = self.page.get_by_role("spinbutton").first
                await first_spin.wait_for(state="visible", timeout=12000)
                totp_ready = True
            except Exception:
                totp_ready = False

            if not totp_ready:
                # CAPTCHA failed, expired, or credentials were wrong -> capture new image
                captcha_loc = self.page.locator("#imgCaptcha")
                if await captcha_loc.count() == 0:
                    captcha_loc = self.page.locator('img[id*="Captcha"]')

                new_captcha_path = os.path.join(BASE_DOWNLOAD_DIR, f"captcha_{self.chat_id}.png")
                await captcha_loc.screenshot(path=new_captcha_path)
                return False, "CAPTCHA invalid or expired. Please enter the new one above:", new_captcha_path

            self.step = "TOTP"
            return True, "TOTP", ""

        except Exception as e:
            logger.error(f"Error in submit_captcha: {e}")
            captcha_loc = self.page.locator("#imgCaptcha")
            new_captcha_path = os.path.join(BASE_DOWNLOAD_DIR, f"captcha_{self.chat_id}.png")
            try:
                await captcha_loc.screenshot(path=new_captcha_path)
            except Exception:
                pass
            return False, f"Error: {e}. Please retry CAPTCHA:", new_captcha_path

    async def complete_login_with_totp(self, totp_code: str) -> tuple[bool, str]:
        try:
            # 1. Fill 6 split digit boxes
            spins = self.page.get_by_role("spinbutton")
            for idx, digit in enumerate(totp_code):
                await spins.nth(idx).fill(digit)

            await self.page.get_by_role("button", name="टीओटीपी मान्य करें").click()
            await self.page.wait_for_timeout(4000)

            # Error validation
            error_modal = self.page.locator(".swal2-modal, .alert, #lblTOTPMessage, :text('अमान्य')")
            if await error_modal.count() > 0 and await error_modal.first.is_visible():
                ok_btn = self.page.get_by_role("button", name="OK")
                if await ok_btn.count() > 0:
                    await ok_btn.click()
                return False, "Google Authenticator code expired or invalid. Please check app and enter fresh TOTP."

            if await self.page.get_by_role("spinbutton").count() > 0:
                return False, "TOTP rejected by portal. Please enter the latest 6-digit code."

            # 2. Click e-Mitra
            try:
                async with self.page.expect_popup(timeout=8000) as popup_info:
                    await self.page.get_by_role("link", name="e-Mitra").click()
                emitra_page = await popup_info.value
            except Exception:
                emitra_page = self.page

            await emitra_page.wait_for_load_state("networkidle")
            await emitra_page.wait_for_timeout(3000)

            # 3. Dismiss Modals & Overlays
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

            # 4. Navigate Services -> Avail Service -> Utility
            await emitra_page.locator("a").filter(has_text="Services").click(force=True)
            await emitra_page.locator("a").filter(has_text="Avail Service").click(force=True)
            await emitra_page.locator("a").filter(has_text="Utility").click(force=True)
            await emitra_page.wait_for_timeout(2000)

            # 5. Search Service "phed"
            await emitra_page.get_by_text("Select Service").click(force=True)
            search_box = emitra_page.get_by_role("textbox")
            await search_box.fill("phed")
            await emitra_page.wait_for_timeout(1000)
            await emitra_page.get_by_text("PHED - APPLY FOR NEW DOMESTIC").click(force=True)

            # 6. Confirmation popups
            search_btn = emitra_page.get_by_role("button", name="Search")
            await search_btn.click(force=True)
            await emitra_page.wait_for_timeout(1500)

            yes_btn = emitra_page.get_by_role("button", name="Yes")
            if await yes_btn.count() > 0:
                await yes_btn.click(force=True)
                await emitra_page.wait_for_timeout(2000)

            # 7. Final link to the application
            await emitra_page.get_by_role("link", name="New Application (Domestic)").click(force=True)
            await emitra_page.wait_for_load_state("networkidle")
            await emitra_page.wait_for_timeout(3000)

            # 8. Dump all context cookies to cookies.json
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


# --- Scraper & Dashboard Extraction Engine --- #

async def fetch_dashboard_status_records(cookies_dict: dict, max_pages: int = 5) -> dict:
    categorized = {
        "PLUMBER": [], "PAYMENT": [], "VERIFICATION": [], "OBJECTION": [], "COMPLETED": [], "OTHER": []
    }
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            cookie_items = [
                {"name": k, "value": v, "domain": "ssoapps.rajasthan.gov.in", "path": "/"}
                for k, v in cookies_dict.items()
            ]
            await context.add_cookies(cookie_items)
            page = await context.new_page()
            await page.goto(f"{BASE_URL}/Home/Dashboard", wait_until="networkidle", timeout=30000)

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
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            cookie_items = [
                {"name": k, "value": v, "domain": "ssoapps.rajasthan.gov.in", "path": "/"}
                for k, v in cookies_dict.items()
            ]
            await context.add_cookies(cookie_items)
            page = await context.new_page()

            target_url = view_url
            if not target_url:
                await page.goto(f"{BASE_URL}/Home/Dashboard", wait_until="networkidle", timeout=30000)
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

            await page.goto(target_url, wait_until="networkidle", timeout=30000)
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
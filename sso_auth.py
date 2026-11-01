import os
import json
import logging
from playwright.async_api import async_playwright
import config

logger = logging.getLogger(__name__)

class SSOAuthenticator:
    """Manages asynchronous browser login automation through SSO, TOTP, and e-Mitra."""

    def __init__(self):
        self.p = None
        self.browser = None
        self.context = None
        self.page = None

    async def initialize_and_get_captcha(self, chat_id: int) -> str:
        """Launches Chromium, enters credentials, and captures the CAPTCHA image."""
        self.p = await async_playwright().start()
        self.browser = await self.p.chromium.launch(headless=True)
        self.context = await self.browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        self.page = await self.context.new_page()
        await self.page.goto(config.SSO_LOGIN_URL, wait_until="networkidle")

        await self.page.get_by_role("textbox", name="Digital Identity (SSOID/").fill(config.SSO_USER)
        await self.page.get_by_role("textbox", name="Password").fill(config.SSO_PASS)

        captcha_loc = self.page.locator("#imgCaptcha")
        if await captcha_loc.count() == 0:
            captcha_loc = self.page.locator('img[id*="Captcha"]')

        captcha_path = os.path.join(config.BASE_DOWNLOAD_DIR, f"captcha_{chat_id}.png")
        await captcha_loc.screenshot(path=captcha_path)
        return captcha_path

    async def submit_captcha(self, captcha_text: str, chat_id: int) -> tuple[bool, str]:
        """Submits the CAPTCHA and checks for already-logged-in prompts or invalidation."""
        captcha_box = self.page.get_by_role("textbox", name="Enter 6-digit captcha code")
        await captcha_box.fill(captcha_text)
        await self.page.get_by_role("button", name="Login").click()
        await self.page.wait_for_timeout(3000)

        # Handle multiple/concurrent login prompt if present
        already_logged = self.page.get_by_role("checkbox", name="You are already logged-in (IP")
        if await already_logged.count() > 0:
            await already_logged.check()
            pass_box = self.page.get_by_role("textbox", name="Password")
            if await pass_box.count() > 0:
                await pass_box.fill(config.SSO_PASS)
            await self.page.get_by_role("button", name="Login").click()
            await self.page.wait_for_timeout(3000)

        spin_buttons = self.page.get_by_role("spinbutton")
        if await spin_buttons.count() > 0:
            return True, ""

        # Capture refreshed CAPTCHA on failure
        captcha_loc = self.page.locator("#imgCaptcha")
        if await captcha_loc.count() == 0:
            captcha_loc = self.page.locator('img[id*="Captcha"]')

        captcha_path = os.path.join(config.BASE_DOWNLOAD_DIR, f"captcha_{chat_id}.png")
        await captcha_loc.screenshot(path=captcha_path)
        return False, captcha_path

    async def complete_login_and_save_cookies(self, totp_code: str) -> tuple[bool, str]:
        """Enters split TOTP digits, navigates e-Mitra to PHED, and dumps all session cookies."""
        try:
            spins = self.page.get_by_role("spinbutton")
            for idx, digit in enumerate(totp_code):
                await spins.nth(idx).fill(digit)

            await self.page.get_by_role("button", name="टीओटीपी मान्य करें").click()
            await self.page.wait_for_timeout(4000)

            # Check for invalid or expired TOTP
            error_modal = self.page.locator(".swal2-modal, .alert, #lblTOTPMessage, :text('अमान्य')")
            if await error_modal.count() > 0 and await error_modal.first.is_visible():
                ok_btn = self.page.get_by_role("button", name="OK")
                if await ok_btn.count() > 0:
                    await ok_btn.click()
                return False, "Google Authenticator code expired or invalid."

            if await self.page.get_by_role("spinbutton").count() > 0:
                return False, "TOTP was rejected by the server."

            # Open e-Mitra application
            try:
                async with self.page.expect_popup(timeout=8000) as popup_info:
                    await self.page.get_by_role("link", name="e-Mitra").click()
                emitra_page = await popup_info.value
            except Exception:
                emitra_page = self.page

            await emitra_page.wait_for_load_state("networkidle")
            await emitra_page.wait_for_timeout(3000)

            # Dismiss PrimeNG modal dialogs and overlays cleanly using force=True
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

            # Navigate: Services -> Avail Service -> Utility
            await emitra_page.locator("a").filter(has_text="Services").click(force=True)
            await emitra_page.locator("a").filter(has_text="Avail Service").click(force=True)
            await emitra_page.locator("a").filter(has_text="Utility").click(force=True)
            await emitra_page.wait_for_timeout(2000)

            # Search Service: "phed"
            await emitra_page.get_by_text("Select Service").click(force=True)
            search_box = emitra_page.get_by_role("textbox")
            await search_box.fill("phed")
            await emitra_page.wait_for_timeout(1000)
            await emitra_page.get_by_text("PHED - APPLY FOR NEW DOMESTIC").click(force=True)

            # Confirm Selection
            await emitra_page.get_by_role("button", name="Search").click(force=True)
            await emitra_page.wait_for_timeout(1500)
            yes_btn = emitra_page.get_by_role("button", name="Yes")
            if await yes_btn.count() > 0:
                await yes_btn.click(force=True)
                await emitra_page.wait_for_timeout(2000)

            # Final Application Entry
            await emitra_page.get_by_role("link", name="New Application (Domestic)").click(force=True)
            await emitra_page.wait_for_load_state("networkidle")
            await emitra_page.wait_for_timeout(3000)

            # Dump All Cookies
            all_cookies = await self.context.cookies()
            cookie_dict = {c["name"]: c["value"] for c in all_cookies}

            with open(config.COOKIE_FILE_PATH, "w", encoding="utf-8") as f:
                json.dump(cookie_dict, f, indent=4)

            return True, "Success"
        except Exception as e:
            logger.error(f"Error during SSO navigation: {e}", exc_info=True)
            return False, str(e)
        finally:
            await self.close()

    async def close(self):
        """Cleanly releases browser engine resources."""
        try:
            if self.browser:
                await self.browser.close()
            if self.p:
                await self.p.stop()
        except Exception:
            pass
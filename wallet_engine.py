import os
import re
import json
import asyncio
import logging
from PIL import Image
from playwright.async_api import async_playwright
import ddddocr
import config

logger = logging.getLogger(__name__)
ocr = ddddocr.DdddOcr(show_ad=False)

CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]

class EmitraWalletRefillSession:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.p = None
        self.browser = None
        self.context = None
        self.page = None
        self.emitra_page = None
        self.step = "TOTP"
        self.amount = "500"

    async def auto_login_and_request_totp(self, amount: str) -> tuple[bool, str]:
        """Opens SSO, automatically solves CAPTCHA, and advances to TOTP."""
        self.amount = str(amount)
        try:
            self.p = await async_playwright().start()
            self.browser = await self.p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
            self.context = await self.browser.new_context(
                viewport={"width": 1366, "height": 768},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            self.page = await self.context.new_page()
            await self.page.goto(config.SSO_LOGIN_URL, wait_until="networkidle")

            await self.page.get_by_role("textbox", name="Digital Identity (SSOID/").fill(config.SSO_USER)
            await self.page.get_by_role("textbox", name="Password").fill(config.SSO_PASS)

            for attempt in range(1, 5):
                logger.info(f"Refill login CAPTCHA attempt {attempt}...")
                captcha_loc = self.page.locator("#imgCaptcha")
                if await captcha_loc.count() == 0:
                    captcha_loc = self.page.locator('img[id*="Captcha"]')

                await captcha_loc.wait_for(state="visible", timeout=10000)
                img_bytes = await captcha_loc.screenshot()

                captcha_code = ocr.classification(img_bytes).strip()
                captcha_code = re.sub(r"[^A-Za-z0-9]", "", captcha_code)

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
                            await pass_box.fill(config.SSO_PASS)
                        await self.page.get_by_role("button", name="Login").click()
                        await self.page.wait_for_timeout(2500)
                except Exception:
                    pass

                try:
                    first_spin = self.page.get_by_role("spinbutton").first
                    await first_spin.wait_for(state="visible", timeout=4000)
                    self.step = "TOTP"
                    return True, "Ready for TOTP"
                except Exception:
                    await self.page.wait_for_timeout(1000)

            return False, "Failed to auto-solve CAPTCHA."

        except Exception as e:
            logger.error(f"Error in auto_login_and_request_totp: {e}")
            await self.close()
            return False, str(e)

    async def submit_totp_and_get_qr(self, totp_code: str) -> tuple[bool, str, str]:
        """Submits TOTP, opens e-Mitra directly, selects PhonePe, and returns cropped QR image."""
        try:
            spins = self.page.get_by_role("spinbutton")
            for idx, digit in enumerate(totp_code):
                await spins.nth(idx).fill(digit)

            await self.page.get_by_role("button", name="टीओटीपी मान्य करें").click()
            await self.page.wait_for_timeout(4000)

            if await self.page.get_by_role("spinbutton").count() > 0:
                return False, "", "TOTP expired or rejected."

            try:
                async with self.page.expect_popup(timeout=8000) as popup_info:
                    await self.page.get_by_role("link", name="e-Mitra").click()
                self.emitra_page = await popup_info.value
            except Exception:
                self.emitra_page = self.page

            await self.emitra_page.wait_for_load_state("domcontentloaded")
            await self.emitra_page.wait_for_timeout(3000)

            for sel in [".p-dialog-header-close", "button:has-text('')", "button:has-text('I have read and acknowledge')"]:
                try:
                    btn = self.emitra_page.locator(sel)
                    if await btn.count() > 0 and await btn.first.is_visible():
                        await btn.first.click(force=True, timeout=2000)
                        await self.emitra_page.wait_for_timeout(500)
                except Exception:
                    pass

            refill_btn = self.emitra_page.locator("a, button, span, div").filter(has_text=re.compile(r"^Refill\s*Wallet$", re.IGNORECASE))
            if await refill_btn.count() == 0:
                refill_btn = self.emitra_page.get_by_text("Refill Wallet")

            await refill_btn.first.wait_for(state="visible", timeout=12000)
            await refill_btn.first.click(force=True)
            await self.emitra_page.wait_for_timeout(2000)

            amount_field = self.emitra_page.locator("#withoutgrouping")
            if await amount_field.count() == 0 or not await amount_field.is_visible():
                amount_field = self.emitra_page.locator("input[placeholder*='Amount'], input[type='number']")

            await amount_field.first.wait_for(state="visible", timeout=12000)
            await amount_field.first.click(force=True)
            await amount_field.first.fill(str(self.amount))
            await self.emitra_page.wait_for_timeout(500)

            calc_btn = self.emitra_page.get_by_role("button", name="Refill Wallet")
            if await calc_btn.count() > 0:
                await calc_btn.first.click(force=True)
            await self.emitra_page.wait_for_timeout(1500)

            qr_opt = self.emitra_page.get_by_text("QR CODE PAYMENT")
            await qr_opt.wait_for(state="visible", timeout=10000)
            await qr_opt.click(force=True)
            await self.emitra_page.wait_for_timeout(1000)

            phonepe_opt = self.emitra_page.get_by_text("PHONEPE")
            await phonepe_opt.first.click(force=True)
            await self.emitra_page.wait_for_timeout(1000)

            dropdown_item = self.emitra_page.get_by_role("option", name="PHONEPE")
            if await dropdown_item.count() > 0:
                await dropdown_item.click(force=True)

            pay_now_btn = self.emitra_page.get_by_role("button", name="Pay Now")
            await pay_now_btn.click(force=True)
            await self.emitra_page.wait_for_timeout(1000)

            ok_btn = self.emitra_page.get_by_role("button", name="OK")
            if await ok_btn.count() > 0:
                await ok_btn.click(force=True)

            target_page = self.emitra_page
            try:
                new_tab = await self.context.wait_for_event("page", timeout=6000)
                if new_tab:
                    target_page = new_tab
            except Exception:
                target_page = self.emitra_page

            await target_page.wait_for_load_state("domcontentloaded")
            await target_page.wait_for_timeout(3000)

            qr_locator = target_page.locator("img[alt*='QR'], img[src*='qr'], canvas, .qr-image, div:has-text('Scan via any UPI app')")
            await qr_locator.first.wait_for(state="visible", timeout=25000)

            container = target_page.locator("div:has-text('Scan via any UPI app')").locator("..")
            raw_path = os.path.join(config.BASE_DOWNLOAD_DIR, f"raw_qr_{self.chat_id}.png")
            qr_path = os.path.join(config.BASE_DOWNLOAD_DIR, f"phonepe_qr_{self.chat_id}.png")

            if await container.count() > 0:
                await container.first.screenshot(path=raw_path)
                with Image.open(raw_path) as img:
                    w, h = img.size
                    left = int(w * 0.15)
                    top = int(h * 0.20)
                    right = int(w * 0.85)
                    bottom = int(h * 0.85)
                    cropped = img.crop((left, top, right, bottom))
                    cropped.save(qr_path)
                if os.path.exists(raw_path):
                    os.remove(raw_path)
            else:
                await qr_locator.first.screenshot(path=qr_path)

            self.emitra_page = target_page
            return True, qr_path, "QR Code Ready"

        except Exception as e:
            logger.error(f"Error in submit_totp_and_get_qr: {e}", exc_info=True)
            await self.close()
            return False, "", str(e)

    async def wait_for_payment_and_get_balance(self, timeout_seconds: int = 180) -> tuple[bool, str]:
        """Polls for transaction success and scrapes the updated wallet balance."""
        try:
            returned = False
            for _ in range(timeout_seconds // 3):
                await asyncio.sleep(3)
                curr_url = self.emitra_page.url.lower()
                if "emitra" in curr_url and ("status=success" in curr_url or "refillform" in curr_url or "successfull" in curr_url):
                    returned = True
                    break

            if not returned:
                return False, "Payment timed out or cancelled on PhonePe."

            await self.emitra_page.wait_for_load_state("domcontentloaded")
            await self.emitra_page.wait_for_timeout(2000)

            done_btn = self.emitra_page.get_by_role("button", name="Done")
            if await done_btn.count() > 0:
                await done_btn.click(force=True)
                await self.emitra_page.wait_for_timeout(2000)

            home_link = self.emitra_page.locator("a").filter(has_text=re.compile(r"^Home$"))
            if await home_link.count() > 0:
                await home_link.click(force=True)
                await self.emitra_page.wait_for_timeout(2000)

            dashboard_link = self.emitra_page.locator("a").filter(has_text="Dashboard")
            if await dashboard_link.count() > 0:
                await dashboard_link.click(force=True)
                await self.emitra_page.wait_for_timeout(3000)

            content = await self.emitra_page.content()
            match = re.search(r"₹\s*([0-9,]+\.[0-9]{2})", content)
            balance_text = f"₹ {match.group(1)}" if match else "Updated"
            return True, balance_text

        except Exception as e:
            logger.error(f"Error reading updated balance: {e}")
            return False, f"Error: {e}"
        finally:
            await self.close()

    async def cancel(self):
        """Cleanly releases resources on user cancellation."""
        await self.close()
        for fname in [f"raw_qr_{self.chat_id}.png", f"phonepe_qr_{self.chat_id}.png"]:
            p = os.path.join(config.BASE_DOWNLOAD_DIR, fname)
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    async def close(self):
        try:
            if self.browser:
                await self.browser.close()
            if self.p:
                await self.p.stop()
        except Exception:
            pass
"""Playwright-driven Chrome session.

Douyin's web API is protected by an environment-bound signature (`a_bogus`) plus a
risk-control cookie set.  Empirically the signing only produces usable values in a
*headed* Chrome driven through a persistent profile, so that is what we do.  No
account is required for public profiles — only a real, headed browser.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Callable, Iterator

from .util import DyxError, LoginRequired, log

POST_API = "/aweme/v1/web/aweme/post/"
DETAIL_API = "/aweme/v1/web/aweme/detail/"
PROFILE_API = "/aweme/v1/web/user/profile/other/"
LOGIN_COOKIES = ("sessionid", "sessionid_ss", "sid_tt", "sid_guard", "uid_tt")


def sec_uid_from(text: str) -> str | None:
    if not text:
        return None
    text = text.strip()
    if re.fullmatch(r"MS4wLjABAAAA[\w-]{20,}", text):
        return text
    m = re.search(r"/(?:share/)?user/(MS4wLjABAAAA[\w-]+)", text)
    return m.group(1) if m else None


def aweme_id_from(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"(\d{15,25})", text.strip())
    return m.group(1) if m else None


_SHOW_JS = None


def force_show_chrome(show: bool = True) -> str:
    """Restore the Chrome window, or park it off-screen when show=False."""
    if os.name != "nt":
        return "skipped"
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)

    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    def _work_area():
        r = RECT()
        if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):  # SPI_GETWORKAREA
            return r
        return RECT(0, 0, 1400, 900)

    hits = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value != "Chrome_WidgetWin_1":
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        t = title.value.strip()
        if not t or "DeepSeek Harness" in t or "deepseek" in t.lower():
            return True  # the DSH app itself is also a Chrome_WidgetWin_1
        hits.append((hwnd, t))
        return True

    user32.EnumWindows(cb, 0)
    if not hits:
        return "no chrome window found"

    hwnd, title = hits[0]
    wa = _work_area()
    pad = 24
    w = min(1400, max(900, wa.right - wa.left - pad * 2))
    h = min(900, max(600, wa.bottom - wa.top - pad * 2))
    user32.ShowWindow(hwnd, 9)          # SW_RESTORE
    if show:
        user32.MoveWindow(hwnd, wa.left + pad, wa.top + pad, w, h, True)
        user32.SetForegroundWindow(hwnd)
        return f"{title} @ {hwnd} {w}x{h} (visible)"
    user32.MoveWindow(hwnd, -30000, -30000, w, h, False)
    return f"{title} @ {hwnd} (parked off-screen)"


def force_show_retry(attempts: int = 12, delay: float = 1.0, show: bool = True) -> str:
    """Retry force_show_chrome until a real Chrome window is found."""
    import time as _t
    last = "no chrome window found"
    for _ in range(max(1, attempts)):
        try:
            last = force_show_chrome(show=show)
        except Exception as e:  # noqa: BLE001
            last = f"error: {e}"
        if "no chrome window found" not in last and "error" not in last:
            return last
        _t.sleep(delay)
    return last


class BrowserSession:
    """One persistent, headed Chrome. Use as a context manager."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.b = cfg.data["browser"]
        self._pw = None
        self.ctx = None
        self.page = None
        self._bucket: list[dict] = []
        self.stop_reason = "unknown"

    # ------------------------------------------------------------- lifecycle
    def __enter__(self) -> "BrowserSession":
        from playwright.sync_api import sync_playwright

        os.makedirs(self.b["profileDir"], exist_ok=True)
        self._pw = sync_playwright().start()
        channels = [self.b.get("channel") or "chrome", "msedge", None]
        last: Exception | None = None
        for ch in channels:
            kwargs = dict(
                user_data_dir=self.b["profileDir"],
                headless=bool(self.b.get("headless")),
                args=[
                    "--disable-blink-features=AutomationControlled", "--start-maximized",
                    "--lang=zh-CN",
                    "--mute-audio",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-features=Translate,OptimizationHints",
                ],
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1160, "height": 640},
                accept_downloads=False,
            )
            if ch:
                kwargs["channel"] = ch
            try:
                self.ctx = self._pw.chromium.launch_persistent_context(**kwargs)
                self.channel = ch or "chromium"
                break
            except Exception as e:  # noqa: BLE001
                last = e
                log(f"launch channel={ch} failed: {str(e)[:160]}")
        if self.ctx is None:
            self._pw.stop()
            raise DyxError(
                f"无法启动 Chrome: {last}",
                code="browser_launch_failed",
                hint="确认已安装 Google Chrome 或 Edge；或在 dyx_settings 里设置 browser.channel。",
            )

        self.ctx.set_default_timeout(int(self.b.get("navTimeoutMs") or 60000))
        self.ctx.on("response", self._on_response)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.ctx:
                self.ctx.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass

    # --------------------------------------------------------------- plumbing
    def _on_response(self, resp) -> None:
        url = resp.url
        if "douyin.com" not in url:
            return
        if not any(k in url for k in (POST_API, DETAIL_API, PROFILE_API)):
            return
        item = {"url": url, "status": resp.status, "text": "", "json": None}
        try:
            item["text"] = resp.text()
            item["json"] = json.loads(item["text"]) if item["text"] else None
        except Exception:  # noqa: BLE001
            pass
        self._bucket.append(item)

    def drain(self) -> list[dict]:
        items, self._bucket = self._bucket, []
        return items

    def _wait_for(self, predicate: Callable[[list[dict]], bool], timeout_ms: int) -> list[dict]:
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            if predicate(self._bucket):
                return self._bucket
            self.page.wait_for_timeout(300)
        return self._bucket

    # ------------------------------------------------------------------ warmup
    def is_logged_in(self) -> bool:
        try:
            names = {c["name"] for c in self.ctx.cookies()}
        except Exception:  # noqa: BLE001
            return False
        return any(n in names for n in LOGIN_COOKIES)

    def warm(self, force: bool = False) -> None:
        if not self.b.get("warmup", True) and not force:
            return
        log("预热：访问抖音热点页以取得风控 cookie …")
        try:
            self.goto("https://www.douyin.com/hot", wait_ms=4000)
        except Exception as e:  # noqa: BLE001
            log(f"预热失败（继续尝试）: {str(e)[:120]}")

    def goto(self, url: str, wait_ms: int | None = None) -> None:
        self.page.goto(url, wait_until="domcontentloaded",
                       timeout=int(self.b.get("navTimeoutMs") or 60000))
        self.page.wait_for_timeout(int(wait_ms if wait_ms is not None
                                       else self.b.get("settleMs") or 9000))

    # ------------------------------------------------------------------ login
    def _shot(self, tag: str) -> str:
        """Save a debug screenshot next to the library so a human can inspect it."""
        try:
            base = os.path.dirname(self.cfg.db_path)
            os.makedirs(base, exist_ok=True)
            path = os.path.join(base, f"debug-{tag}.png")
            self.page.screenshot(path=path)
            return path
        except Exception as e:  # noqa: BLE001
            return f"(截图失败: {e})"

    # ------------------------------------------------------------ login helpers
    def _qr_capture(self, qr_out, status_out, state: str, extra: dict | None = None) -> dict:
        """把登录二维码截出来、把当前登录状态写成 JSON，供 Web 页面轮询展示。"""
        info = {"state": state, "updatedAt": int(time.time())}
        try:
            info["url"] = self.page.url
        except Exception:  # noqa: BLE001
            info["url"] = None
        if extra:
            info.update(extra)
        if qr_out and state == "waiting":
            try:
                box = self.page.evaluate(
                    """() => {
                      const el = document.querySelector(
                        'canvas, img[src*="qrcode"], [class*="qrcode"], [class*="qr-code"]');
                      if (!el) return null;
                      const r = el.getBoundingClientRect();
                      if (r.width < 80 || r.height < 80) return null;
                      return {x: Math.max(0, r.x), y: Math.max(0, r.y),
                              width: r.width, height: r.height};
                    }""")
                shot = self.page.screenshot(clip=box) if box else self.page.screenshot()
                path = os.path.abspath(qr_out)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".part"
                with open(tmp, "wb") as fh:
                    fh.write(shot)
                os.replace(tmp, path)
                info["qrAt"] = int(time.time())
                info["qrBytes"] = len(shot)
                info["qrClipped"] = bool(box)
            except Exception as e:  # noqa: BLE001
                info["qrError"] = str(e)[:200]
        if status_out:
            try:
                path = os.path.abspath(status_out)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".part"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(info, fh, ensure_ascii=False)
                os.replace(tmp, path)
            except Exception:  # noqa: BLE001
                pass
        return info

    def login(self, wait_seconds: int = 240, qr_out: str | None = None,
              status_out: str | None = None) -> dict:
        """Open the login dialog and wait for the user to scan the QR code."""
        self.goto("https://www.douyin.com/", wait_ms=6000)
        try:
            self.page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        log(f"窗口：{force_show_retry(show=bool(self.b.get('showWindow')))}")
        if self.is_logged_in():
            self._qr_capture(qr_out, status_out, "loggedIn", {"alreadyLoggedIn": True})
            return {"loggedIn": True, "alreadyLoggedIn": True}
        # open the login dialog using the header entry only. A generic
        # button:has-text("登录") match can land on the phone-login submit
        # button and send an SMS, so never fall back to it.
        clicked = None
        for sel in ('[data-e2e="login-button"]', '#login-button',
                    'header [class*="login"]'):
            try:
                el = self.page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    clicked = sel
                    break
            except Exception:  # noqa: BLE001
                continue
        self.page.wait_for_timeout(2500)
        # make sure the QR tab is the active one
        for tab_sel in ('text=扫码登录', '[class*="tab"]:has-text("扫码登录")'):
            try:
                tab = self.page.query_selector(tab_sel)
                if tab and tab.is_visible():
                    tab.click()
                    self.page.wait_for_timeout(1200)
                    break
            except Exception:  # noqa: BLE001
                continue
        log(f"窗口置前（弹窗后）：{force_show_retry(attempts=6, show=True)}")
        self.page.wait_for_timeout(1200)
        marker = self._shot("login")
        log(f"登录入口：{clicked or '未找到登录按钮（可能已弹出/或页面结构变了）'}")
        log(f"当前页面：{self.page.url}")
        log(f"截图：{marker}")
        qr = self._qr_capture(qr_out, status_out, "waiting", {"clicked": clicked})
        if qr.get("qrAt"):
            log(f"二维码已截取：{os.path.abspath(qr_out)}（{qr.get('qrBytes')} 字节）")
        else:
            log("没截到二维码区域，退回整页截图")
        log(f"请用抖音 App 扫码登录（最长等待 {wait_seconds}s）…")
        deadline = time.time() + wait_seconds
        polls = 0
        while time.time() < deadline:
            polls += 1
            if polls % 8 == 1 and polls > 1:
                self._qr_capture(qr_out, status_out, "waiting")
            if polls == 4:
                try:
                    has_qr = self.page.evaluate(
                        "() => !!document.querySelector('canvas, img[src*=\"qrcode\"], "
                        "[class*=\"qrcode\"], [class*=\"qr-code\"]')")
                    log(f"二维码区域：{'已出现' if has_qr else '没看到'}")
                except Exception:  # noqa: BLE001
                    pass
            if self.is_logged_in():
                self.page.wait_for_timeout(1500)
                self._qr_capture(qr_out, status_out, "loggedIn")
                return {"loggedIn": True, "alreadyLoggedIn": False}
            self.page.wait_for_timeout(1500)
        state = "loggedIn" if self.is_logged_in() else "timeout"
        self._qr_capture(qr_out, status_out, state)
        return {"loggedIn": state == "loggedIn", "alreadyLoggedIn": False, "timedOut": True}

    # --------------------------------------------------------------- profile
    def user_profile(self, sec_uid: str) -> dict:
        self.drain()
        self.goto(f"https://www.douyin.com/user/{sec_uid}", wait_ms=8000)
        items = self._wait_for(
            lambda b: any(PROFILE_API in i["url"] and i.get("json") for i in b), 15000)
        for it in items:
            if PROFILE_API in it["url"] and it.get("json"):
                return it["json"].get("user") or {}
        raise DyxError(f"未能取得作者信息 {sec_uid}", code="profile_unavailable")

    # ---------------------------------------------------------------- posts
    # --------------------------------------------------------------- scrolling
    SCROLL_JS = """
    () => {
      const pick = () => {
        let best = null, bestH = -1;
        for (const el of document.querySelectorAll('div')) {
          if (el.scrollHeight > el.clientHeight + 80 && el.clientHeight > 200 && el.scrollHeight > bestH) {
            best = el; bestH = el.scrollHeight;
          }
        }
        return best;
      };
      const stale = window.__dyxScroller && !document.contains(window.__dyxScroller);
      const el = (!stale && window.__dyxScroller) ? window.__dyxScroller : pick();
      window.__dyxScroller = el;
      if (!el) {
        window.scrollTo(0, document.body.scrollHeight);
        return { mode: 'window' };
      }
      const step = Math.max(700, Math.round(el.clientHeight * 0.9));
      el.scrollTop = Math.min(el.scrollHeight, el.scrollTop + step);
      return { mode: 'container', top: el.scrollTop, sh: el.scrollHeight, ch: el.clientHeight };
    }
    """

    def _scroll_step(self) -> dict:
        """Advance the profile page's own scroll container (window scrolling is a no-op here)."""
        try:
            return self.page.evaluate(self.SCROLL_JS)
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _scan_posts(batch: list[dict], seen: set[str]) -> tuple[list[dict], bool, dict]:
        fresh: list[dict] = []
        has_more = True
        saw_post_api = False
        meta: dict = {}
        for it in batch:
            if POST_API not in it.get("url", "") or not it.get("json"):
                continue
            saw_post_api = True
            j = it["json"]
            if "has_more" in j:
                has_more = bool(j.get("has_more"))
            meta = {"has_more": has_more, "cursor": j.get("max_cursor")}
            for a in j.get("aweme_list") or []:
                aid = a.get("aweme_id")
                if aid and aid not in seen:
                    seen.add(aid)
                    fresh.append(a)
        return fresh, saw_post_api, meta

    def _has_fresh_post(self, batch: list[dict]) -> bool:
        return any(POST_API in i.get("url", "") and i.get("json") for i in batch)

    WALL_MARKERS = ("登录后查看更多作品", "登录查看更多", "立即登录")

    def login_wall(self) -> bool:
        """True when the page shows the anonymous 'log in to see more' cap."""
        try:
            text = self.page.evaluate("() => document.body.innerText || ''")
        except Exception:  # noqa: BLE001
            return False
        return any(m in text for m in self.WALL_MARKERS)

    def iter_user_posts(self, sec_uid: str, max_scrolls: int | None = None,
                        page_size: int | None = None,
                        on_batch: Callable[[list[dict], dict], None] | None = None,
                        ) -> Iterator[dict]:
        """Yield aweme objects from a public profile, newest first, handling cursors."""
        self.drain()
        self.goto(f"https://www.douyin.com/user/{sec_uid}", wait_ms=8000)
        self._wait_for(self._has_fresh_post, 20000)
        self.page.evaluate("() => { window.__dyxScroller = null; }")

        self.stop_reason = "unknown"
        seen: set[str] = set()
        failures = 0
        scrolls = 0
        limit = max_scrolls or int(self.cfg.data["fetch"].get("maxScrolls") or 400)
        delay = int(self.cfg.data["fetch"].get("requestDelayMs") or 900)

        while scrolls < limit:
            fresh, saw_post_api, meta = self._scan_posts(self.drain(), seen)
            if saw_post_api and on_batch is not None:
                on_batch(fresh, meta)
            if fresh:
                failures = 0
                for a in fresh:
                    yield a
            if saw_post_api and not meta.get("has_more", True):
                self.stop_reason = "no-more"
                log("作品已全部取完（has_more=0）")
                return
            if failures >= 3:
                if self.login_wall():
                    self.stop_reason = "login-required"
                    log("作品列表被登录墙截断（页面提示「登录后查看更多作品」）")
                else:
                    self.stop_reason = "no-more"
                    log("连续多次滚动都没取到新作品，结束翻页")
                return

            advanced = False
            for _ in range(3):
                self._scroll_step()
                waited = self._wait_for(self._has_fresh_post, 9000)
                if self._has_fresh_post(waited):
                    advanced = True
                    break
                self.page.wait_for_timeout(delay)
            if not advanced:
                failures += 1
            scrolls += 1
            self.page.wait_for_timeout(max(200, delay // 3))
        self.stop_reason = "max-scrolls"
        log(f"已达到滚动上限 {limit}，结束翻页")

    # ---------------------------------------------------------------- detail
    def aweme_detail(self, aweme_id: str, retries: int = 2) -> dict:
        for attempt in range(retries + 1):
            self.drain()
            try:
                self.goto(f"https://www.douyin.com/video/{aweme_id}", wait_ms=6000)
            except Exception as e:  # noqa: BLE001
                log(f"detail 页面打开失败 {aweme_id}: {str(e)[:100]}")
            items = self._wait_for(
                lambda b: any(DETAIL_API in i["url"] and i.get("json") for i in b), 12000)
            for it in items:
                if DETAIL_API not in it["url"] or not it.get("json"):
                    continue
                j = it["json"]
                ad = j.get("aweme_detail")
                if ad and ad.get("aweme_id"):
                    return ad
                reason = (j.get("filter_detail") or {}).get("filter_reason")
                if reason:
                    raise DyxError(f"作品 {aweme_id} 不可访问: {reason}",
                                   code="aweme_unavailable")
            if attempt < retries:
                self.page.wait_for_timeout(1200)
        raise DyxError(f"未能取得作品详情 {aweme_id}", code="detail_unavailable")

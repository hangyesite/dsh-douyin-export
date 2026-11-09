"""小红书取数：登录态、作者笔记列表、单篇视频地址。

和抖音（browser.py）的差别：
- 列表页对游客硬截断（只渲染 12 张卡片、不发 user_posted 请求），登录后才会滚动翻页；
- 视频地址不再出现在页面状态里（mediaV2 改成了 JSON 字符串且常为空），
  但播放器一定会请求 CDN 上的 .mp4，所以用「拦网络请求」取地址，纯 HTTP 直下即可。

本模块只负责「拿到数据」，落库/下载/转写交给 ops。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, Iterator, List, Optional

from .util import ShortVideoError, LoginRequired, log

XHS_HOME = "https://www.xiaohongshu.com"
NOTE_URL = XHS_HOME + "/explore/{note_id}?xsec_token={tok}&xsec_source=pc_user"
PROFILE_URL = XHS_HOME + "/user/profile/{uid}?xsec_token={tok}&xsec_source=pc_user"

USER_ID_RE = re.compile(r"/user/profile/([0-9a-f]{24})")
NOTE_ID_RE = re.compile(r"/(?:explore|discovery/item)/([0-9a-f]{24})")
XSEC_RE = re.compile(r"[?&]xsec_token=([^&]+)")
SHORT_RE = re.compile(r"https?://xhslink\.(?:com|cn)/[A-Za-z0-9/._-]+")

MP4_HINT = ".mp4"

# 登录后才能看到的墙：出现这些文案说明还是游客
WALL_MARKERS = ("登录即可查看", "登录后查看", "手机号登录", "扫码登录")

# 列表页在登录态下的接口
LIST_PATH = "/api/sns/web/v1/user_posted"


def note_url(note_id: str, xsec_token: str = "") -> str:
    """小红书笔记的可打开链接。

    单靠 note_id（/explore/<id>）在 PC 端会报「你访问的页面不见了」，
    必须带上该笔记自带的 xsec_token 才能打开。
    """
    base = f"{XHS_HOME}/explore/{note_id}"
    tok = (xsec_token or "").strip()
    if not tok:
        return base
    return f"{base}?xsec_token={tok}&xsec_source=pc_user"


def is_xhs_url(text: str) -> bool:
    t = (text or "").lower()
    return ("xhslink.com" in t or "xhslink.cn" in t
            or "xiaohongshu.com" in t or "xhscdn.com" in t)


def parse_xhs(text: str) -> Dict[str, Any]:
    """把一段文本里的小红书线索抠出来：user_id / note_id / xsec_token / 短链。"""
    t = text or ""
    out: Dict[str, Any] = {"user_id": None, "note_id": None, "xsec_token": None, "short": None}
    m = USER_ID_RE.search(t)
    if m:
        out["user_id"] = m.group(1)
    m = NOTE_ID_RE.search(t)
    if m:
        out["note_id"] = m.group(1)
    m = XSEC_RE.search(t)
    if m:
        out["xsec_token"] = m.group(1)
    m = SHORT_RE.search(t)
    if m:
        out["short"] = m.group(0)
    if out["xsec_token"]:
        import urllib.parse as _up
        out["xsec_token"] = _up.unquote(out["xsec_token"])
    return out


def resolve_short(url: str, timeout: int = 20) -> str:
    """短链 302 到真实地址（桌面 UA 即可，不需要 cookie）。"""
    import urllib.request

    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            final = r.geturl()
    except Exception as e:  # 已跟随重定向时 urllib 会抛，这里回落到原串
        log(f"短链解析失败：{e}")
        return url
    # 未登录时服务端会跳到 /login?redirectPath=<真正目标>，从里面还原真实地址
    if "/login" in final and "redirectPath=" in final:
        import urllib.parse as _up
        q = _up.parse_qs(_up.urlparse(final).query)
        real = (q.get("redirectPath") or [""])[0]
        if real:
            return _up.unquote(real) if "%2F" in real else real
    return final


def _num(v: Any) -> int:
    if isinstance(v, dict):
        v = v.get("count")
    if v in (None, ""):
        return 0
    s = str(v).replace("+", "").strip()
    try:
        if s.endswith("万"):
            return int(float(s[:-1]) * 10000)
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def _srt_of(media: Any) -> Optional[str]:
    """在 note.video 的任意层级里找出官方字幕（.srt）地址。"""
    found: List[str] = []

    def walk(o: Any, depth: int = 0) -> None:
        if o is None or depth > 8 or found:
            return
        if isinstance(o, str):
            if o[:1] in "{[":
                try:
                    walk(json.loads(o), depth + 1)
                except (ValueError, TypeError):
                    pass
                return
            if o.startswith("http") and o.split("?")[0].endswith(".srt"):
                found.append(o)
            return
        if isinstance(o, list):
            for x in o:
                walk(x, depth + 1)
                if found:
                    return
            return
        if isinstance(o, dict):
            for v in o.values():
                walk(v, depth + 1)
                if found:
                    return

    walk(media)
    return found[0] if found else None


class XhsSession:
    """小红书浏览器会话（headed，持久化 profile，登录一次长期有效）。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.profile = (cfg.get("browser.xhsProfileDir")
                        or os.path.join(cfg.home, "xhs-profile"))
        hl = cfg.get("browser.xhsHeadless")
        self.headless = bool(cfg.get("browser.headless")) if hl is None else bool(hl)
        self.nav_timeout = int(cfg.get("browser.navTimeoutMs") or 60000)
        self.settle_ms = int(cfg.get("browser.settleMs") or 6000)
        self._pw = None
        self.ctx = None
        self.page = None
        self._mp4: List[str] = []
        self._list_payloads: List[Dict[str, Any]] = []
        self.stop_reason = "unknown"

    # ---------- 生命周期 ----------
    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        profile = self.profile or os.path.join(self.cfg.home, "chrome-profile")
        args = [
            "--disable-blink-features=AutomationControlled",
            "--lang=zh-CN",
            "--no-first-run",
            "--mute-audio",
            "--start-maximized",
        ]
        kwargs: Dict[str, Any] = dict(
            user_data_dir=profile,
            headless=self.headless,
            args=args,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 900},
        )
        last: Optional[Exception] = None
        for channel in ("chrome", "msedge", None):
            try:
                if channel:
                    self.ctx = self._pw.chromium.launch_persistent_context(channel=channel, **kwargs)
                else:
                    kwargs.pop("channel", None)
                    self.ctx = self._pw.chromium.launch_persistent_context(**kwargs)
                break
            except Exception as e:  # 依次回退
                last = e
        if self.ctx is None:
            self._pw.stop()
            raise ShortVideoError(f"无法启动浏览器：{last}", code="browser_launch_failed")
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.on("request", self._on_request)
        self.page.on("response", self._on_response)
        return self

    def __exit__(self, *exc):
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self._pw:
                self._pw.stop()
        return False

    # ---------- 事件 ----------
    def _on_request(self, req) -> None:
        url = req.url
        if MP4_HINT in url and "xhscdn" in url:
            self._mp4.append(url)

    def _on_response(self, resp) -> None:
        if LIST_PATH not in resp.url:
            return
        try:
            body = resp.text()
        except Exception:
            return
        if not body or len(body) < 40:
            return
        try:
            data = json.loads(body)
        except ValueError:
            return
        if isinstance(data, dict):
            self._list_payloads.append(data)

    def drain(self) -> None:
        self._mp4.clear()
        self._list_payloads.clear()

    # ---------- 登录态 ----------
    def cookies(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        try:
            for c in self.ctx.cookies():
                out[c.get("name")] = c.get("value")
        except Exception:
            pass
        return out

    def is_logged_in(self) -> bool:
        """仅以 id_token 为准（游客会有 unread，但不会有 id_token）。"""
        return bool(self.cookies().get("id_token"))

    def _dump(self, qr_out):
        if not qr_out:
            return
        try:
            qr = self._qr_visible()
            if qr is not None:
                box = qr.bounding_box()
                if box and box.get("width", 0) > 80 and box.get("height", 0) > 80:
                    pad = 12
                    self.page.screenshot(path=qr_out, clip={
                        "x": max(0, box["x"] - pad),
                        "y": max(0, box["y"] - pad),
                        "width": box["width"] + pad * 2,
                        "height": box["height"] + pad * 2,
                    })
                    return
            self.page.screenshot(path=qr_out, full_page=False)
        except Exception as e:  # noqa: BLE001
            log(f"二维码截图失败：{e}")

    def _status(self, status_out, state, **extra):
        if not status_out:
            return
        try:
            payload = {"state": state, "platform": "xiaohongshu",
                       "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S")}
            payload.update(extra)
            tmp = status_out + ".part"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, status_out)
        except OSError:
            pass


    LOGIN_ENTRY_SELECTORS = (
        '[class*="login-btn"]',
        '[class*="login"] span',
        'button:has-text("\u767b\u5f55")',
        'span:has-text("\u767b\u5f55")',
        '#login-btn',
    )
    QR_SELECTORS = (
        'canvas',
        'img[src*="qrcode"]',
        '[class*="qrcode"]',
        '[class*="qr-code"]',
        '[class*="login-container"]',
        '[class*="login-modal"]',
    )

    def _qr_visible(self):
        for sel in self.QR_SELECTORS:
            try:
                for el in self.page.query_selector_all(sel):
                    if el.is_visible():
                        return el
            except Exception:
                continue
        return None

    def _wait_qr(self, timeout_ms=8000):
        end = time.time() + timeout_ms / 1000.0
        while time.time() < end:
            if self._qr_visible() is not None:
                return True
            self.page.wait_for_timeout(300)
        return False

    def _open_login_modal(self):
        """首页 -> 点【登录】 -> 等二维码弹窗出来。返回 (ok, tried)"""
        tried = []
        for sel in self.LOGIN_ENTRY_SELECTORS:
            try:
                nodes = self.page.query_selector_all(sel)
            except Exception:
                continue
            for el in nodes:
                try:
                    if not el.is_visible():
                        continue
                    el.click(timeout=2500)
                    tried.append(sel)
                    if self._wait_qr(7000):
                        return True, tried
                except Exception:
                    continue
        # 兜底：用 JS 找文字精确等于「登录」的最小元素并点它
        try:
            clicked = self.page.evaluate(
                "() => {"
                " const c = [];"
                " document.querySelectorAll('div,span,button,a').forEach(el => {"
                "  if ((el.textContent || '').trim() !== '\u767b\u5f55') return;"
                "  const r = el.getBoundingClientRect();"
                "  if (!r.width || !r.height) return;"
                "  c.push({el, area: r.width * r.height});"
                " });"
                " if (!c.length) return '';"
                " c.sort((a, b) => a.area - b.area);"
                " c[0].el.click();"
                " return 'js-click';"
                "}"
            )
            if clicked:
                tried.append("js")
                if self._wait_qr(8000):
                    return True, tried
        except Exception:
            pass
        return False, tried

    def login(self, wait_seconds: int = 240, qr_out=None, status_out=None, force: bool = False):
        """打开小红书首页，点开登录弹窗（扫码），可选截图供页面显示。"""
        if force:
            # 强制换新会话：清掉小红书域名的 cookie，这样才能弹出二维码
            try:
                self.ctx.clear_cookies()
                log("已清空浏览器 cookie，准备重新扫码")
            except Exception as e:  # noqa: BLE001
                log(f"清 cookie 失败：{e}")
        self.page.goto(XHS_HOME, wait_until="domcontentloaded", timeout=self.nav_timeout)
        self.page.wait_for_timeout(2500)
        if not force and self.is_logged_in():
            self._status(status_out, "loggedIn", alreadyLoggedIn=True)
            return {"ok": True, "alreadyLoggedIn": True, "loggedIn": True}

        ok, tried = self._open_login_modal()
        if not ok:
            log("首页没弹出登录框，改试 /login 页")
            try:
                self.page.goto(XHS_HOME + "/login", wait_until="domcontentloaded", timeout=self.nav_timeout)
                self.page.wait_for_timeout(2500)
            except Exception:
                pass
            ok, tried2 = self._open_login_modal()
            tried = tried + tried2
            if not ok:
                ok = self._wait_qr(4000)
        log("登录弹窗：" + ("已打开" if ok else "未打开") + " 命中：" + str(tried))
        self._status(status_out, "waiting", modal=bool(ok), clicked=tried)
        self._dump(qr_out)

        deadline = time.time() + wait_seconds
        tick = 0
        while time.time() < deadline:
            self.page.wait_for_timeout(2000)
            tick += 1
            if tick % 2 == 0:
                self._dump(qr_out)
            if self.is_logged_in():
                log("登录成功")
                self._save_cookies()
                self._status(status_out, "loggedIn", clicked=tried)
                return {"ok": True, "loggedIn": True, "clicked": tried, "modal": bool(ok)}
            if tick % 8 == 0:
                for sel in ('[class*="refresh"]', 'span:has-text("\u5237\u65b0")'):
                    try:
                        el = self.page.query_selector(sel)
                        if el and el.is_visible():
                            el.click(timeout=1500)
                            break
                    except Exception:
                        continue
        self._status(status_out, "expired", clicked=tried)
        return {"ok": True, "loggedIn": False, "timedOut": True, "clicked": tried}

    def _save_cookies(self):
        try:
            jar = {c["name"]: c["value"] for c in self.ctx.cookies() if c.get("name")}
            path = os.path.join(os.path.dirname(self.cfg.db_path), COOKIE_FILE)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(jar, f, ensure_ascii=False, indent=1)
            log("登录态已保存：" + path)
        except Exception as e:  # noqa: BLE001
            log("保存登录态失败：" + str(e))

    # ---------- 作者笔记列表 ----------

    def user_notes(self, user_id: str, xsec_token: str = "", limit: Optional[int] = None,
                   max_scrolls: int = 200) -> Iterator[Dict[str, Any]]:
        """滚动作者主页，产出笔记列表（登录态才翻得动）。"""
        url = PROFILE_URL.format(uid=user_id, tok=xsec_token or "")
        self.drain()
        self.page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout)
        self.page.wait_for_timeout(self.settle_ms)

        seen: Dict[str, Dict[str, Any]] = {}
        stale = 0
        self.stop_reason = "unknown"
        for i in range(max_scrolls):
            fresh = 0
            for payload in self._list_payloads:
                for note in (payload.get("data") or {}).get("notes") or []:
                    nid = note.get("note_id") or note.get("id")
                    if not nid or nid in seen:
                        continue
                    seen[nid] = note
                    fresh += 1
                    yield note
            self._list_payloads.clear()
            if limit and len(seen) >= limit:
                self.stop_reason = "limit"
                return
            if fresh == 0:
                stale += 1
            else:
                stale = 0
            if stale >= 6:
                self.stop_reason = "no-more" if self.is_logged_in() else "login-required"
                return
            self._scroll()
            self.page.wait_for_timeout(1200)
        self.stop_reason = "max-scrolls"

    def _scroll(self) -> None:
        try:
            self.page.evaluate(
                """() => {
                    let best = null, h = 0;
                    for (const el of document.querySelectorAll('div')) {
                        if (el.scrollHeight > el.clientHeight + 40 && el.clientHeight > 300) {
                            if (el.scrollHeight > h) { h = el.scrollHeight; best = el; }
                        }
                    }
                    const t = best || document.scrollingElement;
                    if (t) t.scrollTop = t.scrollTop + Math.max(800, (t.clientHeight || 800) * 0.9);
                }"""
            )
        except Exception:
            pass

    def author_info(self, user_id: str, xsec_token: str = "") -> Dict[str, Any]:
        """从主页 __INITIAL_STATE__ 抓作者档案。"""
        if not self.page.url or user_id not in self.page.url:
            self.page.goto(PROFILE_URL.format(uid=user_id, tok=xsec_token or ""),
                           wait_until="domcontentloaded", timeout=self.nav_timeout)
            self.page.wait_for_timeout(self.settle_ms)
        try:
            return self.page.evaluate(
                """() => {
                    const s = window.__INITIAL_STATE__ || {};
                    const u = s.user || {};
                    const d = (u.userPageData && u.userPageData.basicInfo) ||
                              (u.loggedIn && u.userInfo) || u.userInfo || {};
                    const basic = (u.userPageData && u.userPageData.basicInfo) || d;
                    const inter = (u.userPageData && u.userPageData.interactions) || [];
                    const cnt = (t) => { const x = inter.find(i => i && i.type === t); return x ? (x.count || '') : ''; };
                    return {user_id: basic.userId || '', nickname: basic.nickname || '',
                            red_id: basic.redId || '', desc: basic.desc || '',
                            fans: cnt('fans'), follows: cnt('follows'), liked: cnt('liked')};
                }"""
            )
        except Exception as e:
            log(f"作者档案读取失败：{e}")
            return {}

    # ---------- 单篇 ----------
    def note_media(self, note_id: str, xsec_token: str = "", wait_sec: int = 9) -> Dict[str, Any]:
        """打开笔记页，拦到播放器的 mp4 地址，并附带标题/作者/时长/字幕。"""
        # 关键：每次导航前清空捕获队列，避免拿到上一条的 mp4
        try:
            self._mp4.clear()
        except Exception:
            self._mp4 = []
        before = 0
        url = NOTE_URL.format(note_id=note_id, tok=xsec_token or "")
        self.page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout)
        try:
            _b = (self.page.title() or "") + " " + (self.page.inner_text("body")[:400])
        except Exception:
            _b = ""
        if ("验证" in _b) or ("captcha" in (self.page.url or "").lower()):
            try:
                _dir = os.path.dirname(self.cfg.db_path) or "."
                os.makedirs(_dir, exist_ok=True)
                self.page.screenshot(path=os.path.join(_dir, "xhs-captcha.png"))
            except Exception:
                pass
            raise ShortVideoError(
                "小红书要求验证码（风控）", code="xhs_captcha")
        deadline = time.time() + wait_sec
        while time.time() < deadline and len(self._mp4) <= before:
            self.page.wait_for_timeout(1000)
        if len(self._mp4) <= before:
            try:
                self.page.click("video", timeout=2000)
            except Exception:
                pass
            self.page.wait_for_timeout(3500)
        fresh = self._mp4[before:]
        mp4 = fresh[-1] if fresh else None

        info: Dict[str, Any] = {}
        try:
            info = self.page.evaluate(
                """() => {
                    const s = window.__INITIAL_STATE__ || {};
                    const map = (s.note || {}).noteDetailMap || {};
                    const k = Object.keys(map)[0];
                    const n = k ? (map[k].note || {}) : {};
                    const strip = (o) => { try { return JSON.parse(JSON.stringify(o || {})); } catch (e) { return {}; } };
                    return {note_id: n.noteId || '', title: (n.title || '').trim(),
                            desc: n.desc || '', type: n.type || '',
                            time: n.lastUpdateTime || n.time || 0,
                            user: (n.user || {}).nickname || '',
                            user_id: (n.user || {}).userId || '',
                            interact: strip(n.interactInfo),
                            video: strip(n.video), imageList: strip(n.imageList),
                            stateMp4: (() => {
                                const v = n.video || {};
                                const pick = (s) => {
                                    if (!s || typeof s !== 'string') return '';
                                    try {
                                        const j = JSON.parse(s);
                                        const arr = (j && (j.stream || (j.media && j.media.stream))) || [];
                                        for (const it of arr) {
                                            if (it && it.master_url) return it.master_url;
                                        }
                                    } catch (e) {}
                                    const m = s.match(/https:[^"\\]+\.mp4[^"\\]*/);
                                    return m ? m[0] : '';
                                };
                                let out = pick(v.mediaV2);
                                if (!out) {
                                    const st = v.media && v.media.stream ? v.media.stream : [];
                                    for (const it of st) { if (it && it.master_url) { out = it.master_url; break; } }
                                }
                                return out;
                            })()};
                }"""
            )
        except Exception as e:
            log(f"笔记信息读取失败：{e}")
        if not mp4:
            mp4 = info.get("stateMp4") or None
        if not mp4:
            # 再给一次机会：刷新一次后再等几秒
            try:
                self.page.reload(wait_until="domcontentloaded", timeout=self.nav_timeout)
                end2 = time.time() + 7
                while time.time() < end2 and len(self._mp4) <= before:
                    self.page.wait_for_timeout(1000)
                fresh2 = self._mp4[before:]
                if fresh2:
                    mp4 = fresh2[-1]
            except Exception as e:  # noqa: BLE001
                log(f"笔记页刷新失败：{e}")
        # og:video 就是这一条笔记自己的 mp4（最可靠，不会串页）
        try:
            meta = self.page.evaluate(
                "() => { const g = (p) => { const m = document.querySelector('meta[property=\"' + p + '\"]');"
                " return m ? m.getAttribute('content') : ''; };"
                " return {video: g('og:video'), title: g('og:title'), time: g('og:videotime')}; }"
            )
        except Exception:
            meta = {}
        og_video = (meta or {}).get("video") or ""
        if og_video:
            mp4 = og_video
        if not (info.get("title") or "").strip() and (meta or {}).get("title"):
            info["title"] = str(meta["title"]).replace(" - 小红书", "").strip()
        info["mp4"] = mp4
        info["subtitle"] = _srt_of(info.get("video"))
        return info


def normalise_note(note: Dict[str, Any], user_id: str, author_name: str = "") -> Dict[str, Any]:
    """把列表里的笔记对象（或 note_media 的 info）转成 videos 表记录。"""
    nid = note.get("note_id") or note.get("noteId") or note.get("id") or ""
    cover = ""
    if isinstance(note.get("cover"), dict):
        cover = note["cover"].get("url_default") or note["cover"].get("url") or ""
    stats = note.get("interact_info") or note.get("interact") or {}
    t = note.get("time") or note.get("lastUpdateTime") or 0
    try:
        t = int(t)
    except (TypeError, ValueError):
        t = 0
    if t > 10 ** 12:
        t //= 1000
    title = (note.get("display_title") or note.get("displayTitle")
             or note.get("title") or "").strip()
    # 许多笔记 title/desc 都是空，话题标签就是它的“标题”
    tags = [str(x.get("name") or "").strip()
            for x in (note.get("tag_list") or note.get("tagList") or [])
            if isinstance(x, dict) and (x.get("name") or "").strip()]
    if not title and tags:
        title = " ".join("#" + t for t in tags[:3])
    return {
        "platform": "xiaohongshu",
        "aweme_id": nid,
        "sec_uid": user_id,
        "author_name": author_name or (note.get("user") or {}).get("nickname", "") if isinstance(note.get("user"), dict) else author_name,
        "desc": title,
        "create_time": t,
        "duration_ms": 0,
        "width": None,
        "height": None,
        "play_uri": "",
        "ratio": "",
        "cover": cover,
        "tags": tags,
        "image_count": len(note.get("imageList") or []) if isinstance(note.get("imageList"), list) else 0,
        "is_image_post": 1 if (note.get("type") or "").lower() == "normal" else 0,
        "stats_json": json.dumps({
            "digg_count": _num(stats.get("liked_count") or stats.get("likedCount")),
            "collect_count": _num(stats.get("collected_count") or stats.get("collectedCount")),
            "comment_count": _num(stats.get("comment_count") or stats.get("commentCount")),
            "share_count": _num(stats.get("share_count") or stats.get("shareCount")),
        }, ensure_ascii=False),
        "tags": "",
        "share_url": note_url(nid, str(note.get("xsec_token") or note.get("xsecToken") or "")),
        "xsec_token": note.get("xsec_token") or "",
        "raw_json": json.dumps(note, ensure_ascii=False)[:20000],
    }

# ============================ 接口直连（推荐路径） ============================
# 成熟做法（MediaCrawler / XHS-Downloader 同款）：登录一次拿 cookie → 纯算法签名
# x-s / x-t / x-s-common / x-rap-param → 直接打 edith.xiaohongshu.com 的
# user_posted / feed 接口，再直连 CDN 下 mp4。不开浏览器逐页，快一个数量级。
API_HOST = "https://edith.xiaohongshu.com"
COOKIE_FILE = "xhs-cookies.json"


def api_available() -> bool:
    try:
        import xhshow  # noqa: F401
        return True
    except Exception:
        return False


def cookies_of(cfg, refresh: bool = False) -> Dict[str, str]:
    """拿小红书 cookie：优先读缓存，其次从持久化 profile 里读一次再缓存。"""
    path = os.path.join(cfg.home, COOKIE_FILE)
    if not refresh and os.path.exists(path):
        try:
            data = json.loads(open(path, encoding="utf-8").read())
            if data.get("web_session"):
                return data
        except Exception:
            pass
    with XhsSession(cfg) as s:
        jar = s.cookies()
    keep = {k: v for k, v in jar.items() if v}
    if keep.get("web_session"):
        try:
            open(path, "w", encoding="utf-8").write(json.dumps(keep, ensure_ascii=False, indent=1))
        except Exception:
            pass
    return keep


class XhsApi:
    """纯算法签名的小红书接口客户端（需要 xhshow）。"""

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

    def __init__(self, cfg, cookies: Optional[Dict[str, str]] = None):
        from xhshow import Xhshow
        self.cfg = cfg
        self.cookies = cookies or cookies_of(cfg)
        if not self.cookies.get("web_session"):
            raise LoginRequired("小红书登录态缺失。先运行 sv_login platform=xiaohongshu 扫码一次。")
        self._client = Xhshow()
        self._ck = "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    # ---------- 底层 ----------
    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        import urllib.parse
        import urllib.request
        h = dict(self._client.sign_headers_get(uri=API_HOST + path, cookies=self.cookies,
                                               params={k: str(v) for k, v in params.items()}))
        h["User-Agent"] = self.UA
        h["Referer"] = XHS_HOME + "/"
        h["Cookie"] = self._ck
        qs = "&".join(f"{k}={urllib.parse.quote(str(v), safe=',')}" for k, v in params.items())
        req = urllib.request.Request(f"{API_HOST}{path}?{qs}", headers=h)
        return self._read(req)

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        import urllib.request
        h = dict(self._client.sign_headers_post(uri=API_HOST + path, cookies=self.cookies,
                                                payload=payload, x_rap=True))
        h["User-Agent"] = self.UA
        h["Referer"] = XHS_HOME + "/"
        h["Content-Type"] = "application/json;charset=UTF-8"
        h["Cookie"] = self._ck
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(API_HOST + path, data=body, headers=h, method="POST")
        return self._read(req)

    def _read(self, req) -> Dict[str, Any]:
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (461, 471):
                raise ShortVideoError(
                    "小红书要求验证码（风控）。请在浏览器里打开小红书过一次验证后重试。",
                    code="xhs_captcha")
            if e.code in (401, 403, 429):
                raise ShortVideoError(f"小红书接口被拒（HTTP {e.code}），可能登录态过期或请求过快。",
                                      code="xhs_blocked")
            raise ShortVideoError(f"小红书接口失败：HTTP {e.code}", code="xhs_http_error")
        try:
            data = json.loads(raw)
        except ValueError:
            raise ShortVideoError("小红书接口返回的不是 JSON（多半被风控了）。", code="xhs_bad_response")
        if data.get("code") in (300011, 300012):
            hint = "账号安全限制" if data.get("code") == 300011 else "IP 被限制"
            raise ShortVideoError(f"小红书拒绝了本次请求：{hint}。", code="xhs_blocked")
        if data.get("success") is False and data.get("code") not in (0, None):
            raise ShortVideoError(f"小红书接口报错：{data.get('msg') or data.get('code')}",
                                  code="xhs_api_error")
        return data

    # ---------- 业务 ----------
    def user_info(self, user_id: str) -> Dict[str, Any]:
        try:
            d = self._get("/api/sns/web/v1/user/otherinfo",
                          {"target_user_id": user_id, "xsec_token": "", "xsec_source": "pc_user"})
            return ((d.get("data") or {}).get("basic_info") or {})
        except ShortVideoError:
            return {}

    def user_posts(self, user_id: str, xsec_token: str = "", limit: Optional[int] = None
                   ) -> Iterator[Dict[str, Any]]:
        """逐条产出笔记（内部按页 30 条拉取）。"""
        cursor = ""
        total = 0
        for _ in range(400):
            d = self._get(LIST_PATH, {"num": "30", "cursor": cursor, "user_id": user_id,
                                      "image_formats": "jpg,webp,avif",
                                      "xsec_token": xsec_token or "", "xsec_source": "pc_user"})
            data = d.get("data") or {}
            notes = data.get("notes") or []
            if notes:
                if limit:
                    notes = notes[:max(0, limit - total)]
                total += len(notes)
                for note in notes:
                    yield note
            cursor = data.get("cursor") or ""
            if not notes or not data.get("has_more") or (limit and total >= limit):
                return

    def feed(self, note_id: str, xsec_token: str = "") -> Dict[str, Any]:
        if not (xsec_token or "").strip():
            raise ShortVideoError(
                f"缺少这条笔记的 xsec_token（{note_id}），无法取详情字幕。"
                f"重新同步一次作者主页即可补上。", code="xhs_needs_token")
        d = self._post("/api/sns/web/v1/feed", {
            "source_note_id": note_id,
            "image_formats": ["jpg", "webp", "avif"],
            "xsec_token": xsec_token or "",
            "xsec_source": "pc_user",
        })
        items = (d.get("data") or {}).get("items") or []
        if not items:
            raise ShortVideoError(f"小红书笔记取不到：{note_id}", code="xhs_note_missing")
        card = items[0].get("note_card") or {}
        card["note_id"] = items[0].get("id") or note_id
        card["xsec_token"] = xsec_token or ""
        return card

    # ---------- 从 note_card 提料 ----------
    @staticmethod
    def video_url(card: Dict[str, Any]) -> Optional[str]:
        stream = ((card.get("video") or {}).get("media") or {}).get("stream") or {}
        cands = []
        if isinstance(stream, dict):
            for arrs in stream.values():
                if not isinstance(arrs, list):
                    continue
                for a in arrs:
                    if isinstance(a, dict) and a.get("master_url"):
                        cands.append((a.get("avg_bitrate") or a.get("video_bitrate") or 0,
                                      a["master_url"]))
        if cands:
            return max(cands)[1]
        for u in re.findall(r'https?://[^"\\ ]+?\.mp4\?sign=[^"\\ ]*',
                            json.dumps(card, ensure_ascii=False)):
            return u
        return None

    @staticmethod
    def subtitles(card: Dict[str, Any]) -> Dict[str, str]:
        """返回 {'zh': url, 'en': url}；没有就空 dict。"""
        out: Dict[str, str] = {}
        urls = set(re.findall(r'https?://[^"\\ ]+?\.srt\?sign=[^"\\ ]*',
                              json.dumps(card, ensure_ascii=False)))
        for u in urls:
            low = u.split("?")[0].lower()
            out.setdefault("zh" if ("_1." in low or "_11." in low) else "en", u)
        return out


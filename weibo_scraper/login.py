"""
扫码登录模块

实现微博扫码登录流程，获取可用于 m.weibo.cn 的登录 Cookie。

流程（基于 passport.weibo.com/sso/v2/ 接口）：
1. /sso/v2/qrcode/image 获取二维码 (qrid + image URL)
2. 用户用微博 App 扫码并确认
3. 轮询 /sso/v2/qrcode/check 直到返回 alt 令牌
4. 用 alt 换 SUB 等登录 Cookie（通过 cross-domain SSO 跳转）
5. 访问 m.weibo.cn 让 Cookie 落到该域上

Cookie 持久化：可保存到本地文件，下次直接复用。
"""

import logging
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

logger = logging.getLogger("weibo_scraper.login")

# ============================================================
# 端点（passport.weibo.com 的 v2 接口）
# ============================================================
PASSPORT_BASE = "https://passport.weibo.com"
SSO_SIGNIN_PAGE = (
    PASSPORT_BASE + "/sso/signin?entry=miniblog&source=miniblog&disp=popup"
    "&url=https%3A%2F%2Fweibo.com%2F"
)
QR_IMAGE_URL = PASSPORT_BASE + "/sso/v2/qrcode/image?entry=miniblog&size=180"
QR_CHECK_URL = (
    PASSPORT_BASE + "/sso/v2/qrcode/check?tabtype=weibo&gid=102803"
    "&openLoginLayer=0&url=https%3A%2F%2Fweibo.com%2F&qrid={qrid}&disp=popup"
)
# alt 换 Cookie（cross-domain SSO）
SSO_LOGIN_URL = (
    "https://login.sina.com.cn/sso/login.php?entry=weibo&returntype=TEXT"
    "&crossdomain=1&cdult=3&domain=weibo.com&alt={alt}&savestate=30"
)
# 让 Cookie 落到 m.weibo.cn 域
M_WEIBO_HOME = "https://m.weibo.cn/"

# 扫码状态码
RETCODE_SUCCESS = 20000000    # 扫码并确认
RETCODE_UNUSED = 50114001     # 未扫码
RETCODE_SCANNED = 50114002    # 已扫码未确认
RETCODE_EXPIRED = 50114004    # 二维码过期

# 默认 UA（PC 浏览器，扫码登录接口要求 PC UA）
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# 校验 Cookie 登录态时使用的移动端 UA（与爬取时一致）
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.0 Mobile/15E148 Safari/604.1"
)


class LoginError(Exception):
    """登录失败"""


class LoginManager:
    """微博扫码登录管理器"""

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": DEFAULT_UA,
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": SSO_SIGNIN_PAGE,
        })

    # -----------------------------------------------------------
    # 第 1 步：获取二维码
    # -----------------------------------------------------------

    def get_qrcode(self) -> dict:
        """获取二维码，返回 {qrid, image_url}"""
        resp = self.session.get(QR_IMAGE_URL, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        if data.get("retcode") != RETCODE_SUCCESS:
            raise LoginError(f"获取二维码失败: {data}")
        payload = data.get("data", {})
        qrid = payload.get("qrid")
        image_url = payload.get("image", "").replace("\\", "")
        if not qrid or not image_url:
            raise LoginError(f"二维码响应格式异常: {data}")
        return {"qrid": qrid, "image_url": image_url}

    # -----------------------------------------------------------
    # 第 2 步：轮询扫码状态
    # -----------------------------------------------------------

    def poll_status(self, qrid: str) -> dict:
        """
        检查一次扫码状态。

        返回:
            {"status": "unused" | "scanned" | "confirmed" | "expired" | "unknown",
             "alt": str  # 仅 confirmed 时有值}
        """
        url = QR_CHECK_URL.format(qrid=qrid)
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        retcode = data.get("retcode")
        payload = data.get("data") or {}

        if retcode == RETCODE_SUCCESS:
            # alt 可能直接在 data.alt，也可能在 data.url 的 query 参数中
            alt = payload.get("alt")
            login_url = payload.get("url") or payload.get("alt_login_url")
            if not alt and login_url:
                # 从 url 中解析 alt 参数
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(login_url).query)
                alt = (qs.get("alt") or [None])[0]
            if not alt:
                raise LoginError(f"扫码成功但未返回 alt: {data}")
            return {"status": "confirmed", "alt": alt, "login_url": login_url}
        if retcode == RETCODE_SCANNED:
            return {"status": "scanned"}
        if retcode == RETCODE_UNUSED:
            return {"status": "unused"}
        if retcode == RETCODE_EXPIRED:
            return {"status": "expired"}
        return {"status": "unknown", "retcode": retcode, "raw": data}

    def wait_for_scan_result(self, qrid: str, timeout: int = 180) -> dict:
        """
        阻塞等待用户扫码确认，返回 poll_status 的完整结果 dict（含 alt）。
        timeout 秒内未完成则抛出 LoginError。
        """
        deadline = time.time() + timeout
        last_status = None
        while time.time() < deadline:
            result = self.poll_status(qrid)
            status = result["status"]
            if status != last_status:
                if status == "scanned":
                    print("  ✓ 已扫码，请在手机上点击「确认登录」")
                elif status == "expired":
                    raise LoginError("二维码已过期，请重新运行")
                last_status = status
            if status == "confirmed":
                return result
            time.sleep(2)
        raise LoginError(f"等待扫码超时（{timeout} 秒）")

    # -----------------------------------------------------------
    # 第 3 步：alt 换 Cookie
    # -----------------------------------------------------------

    def exchange_alt_for_cookie(self, alt: str, login_url: Optional[str] = None) -> str:
        """
        用 alt 令牌换取登录 Cookie，返回 Cookie 字符串。
        会跟随微博跨域 SSO，最终在 m.weibo.cn 域上落下 SUB/SUBP 等。
        """
        # 优先使用扫码返回的 login_url（passport.weibo.com/sso/v2/login）
        if login_url:
            try:
                self.session.get(login_url, timeout=self.timeout, allow_redirects=True)
            except requests.RequestException as e:
                logger.debug("访问 login_url 失败: %s", e)

        # 再走传统跨域 SSO 接口，进一步同步 cookie
        url = SSO_LOGIN_URL.format(alt=quote(alt))
        try:
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            if str(data.get("retcode")) != "0":
                logger.warning("alt 换 Cookie 跨域接口返回非 0: %s", data)
            else:
                # 解析 cross-domain SSO 列表，逐个访问让 cookie 落到各域
                cross_domain_urls = data.get("crossDomainUrlList", [])
                for sso_url in cross_domain_urls:
                    try:
                        if "passport" in sso_url and "action=login" not in sso_url:
                            sso_url = sso_url + "&action=login"
                        self.session.get(sso_url, timeout=self.timeout, allow_redirects=False)
                    except requests.RequestException as e:
                        logger.debug("SSO cross-domain 访问失败 (%s): %s", sso_url, e)
        except requests.RequestException as e:
            logger.warning("alt 换 Cookie 跨域接口访问失败: %s", e)

        # 访问 m.weibo.cn 主页，确保 cookie 在该域上生效
        try:
            self.session.get(M_WEIBO_HOME, timeout=self.timeout)
        except requests.RequestException as e:
            logger.warning("访问 m.weibo.cn 失败: %s", e)

        cookie_str = self._serialize_cookies()
        if "SUB" not in cookie_str:
            raise LoginError(
                f"登录后未获取到 SUB Cookie，登录可能未成功。"
                f"当前 Cookie: {cookie_str or '(空)'}"
            )
        return cookie_str

    # -----------------------------------------------------------
    # 完整登录流程
    # -----------------------------------------------------------

    def login(self, qr_display: str = "auto") -> str:
        """
        执行完整扫码登录流程，返回登录后的 Cookie 字符串。

        Parameters
        ----------
        qr_display : str
            "auto"  - 优先弹窗显示二维码，失败则给出图片 URL
            "window"- 弹出 GUI 窗口显示二维码图片（tkinter 在主线程）
            "ascii" - 用终端 ASCII 显示
            "url"   - 仅打印图片 URL
        """
        print("\n[扫码登录] 正在获取二维码...", end=" ")
        qr = self.get_qrcode()
        print("OK")

        # window 模式：tkinter mainloop 在主线程跑，轮询通过 after 触发
        if qr_display in ("auto", "window"):
            try:
                result = self._show_qrcode_window(
                    qr["image_url"],
                    poller=lambda: self.poll_status(qr["qrid"]),
                )
                if result is None or result.get("status") != "confirmed":
                    raise LoginError(
                        "扫码未完成（窗口被关闭或二维码过期），请重试"
                    )
                alt = result["alt"]
                login_url = result.get("login_url")
                print("  ✓ 已确认，正在换取 Cookie...", end=" ")
                cookie = self.exchange_alt_for_cookie(alt, login_url=login_url)
                print("OK")
                return cookie
            except LoginError:
                raise
            except Exception as e:
                if qr_display == "window":
                    raise
                logger.debug("GUI 二维码窗口显示失败，回退到 URL 模式: %s", e)

        # ascii / url 模式：用 wait_for_scan_result 在主线程轮询
        shown = False
        if qr_display == "ascii":
            try:
                self._print_ascii_qrcode(qr["image_url"])
                shown = True
            except Exception as e:
                logger.debug("终端 ASCII 二维码显示失败: %s", e)
                if qr_display == "ascii":
                    raise

        if not shown or qr_display == "url":
            print(f"\n请打开以下链接，用微博 App 扫码登录:")
            print(f"  {qr['image_url']}")
            print("(或在已登录的微博网页版右上角「扫一扫」)\n")

        print("等待扫码确认...")
        result = self.wait_for_scan_result(qr["qrid"], timeout=180)
        alt = result["alt"]
        login_url = result.get("login_url")
        print("  ✓ 已确认，正在换取 Cookie...", end=" ")
        cookie = self.exchange_alt_for_cookie(alt, login_url=login_url)
        print("OK")
        return cookie

    # -----------------------------------------------------------
    # Cookie 持久化
    # -----------------------------------------------------------

    @staticmethod
    def save_cookie(cookie: str, path: str) -> str:
        """保存 Cookie 到文件，返回文件路径"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(cookie, encoding="utf-8")
        return str(p)

    @staticmethod
    def load_cookie(path: str) -> Optional[str]:
        """从文件加载 Cookie，文件不存在返回 None"""
        p = Path(path)
        if not p.exists():
            return None
        cookie = p.read_text(encoding="utf-8").strip()
        return cookie or None

    @staticmethod
    def is_cookie_valid(cookie: str, timeout: int = 10) -> bool:
        """
        校验 Cookie 是否处于已登录状态。

        通过 m.weibo.cn/api/config 的 login 字段判断。
        注意：Cookie 中若存在重复的 SUB（多域 SSO 残留），登录态会失败，
        因此本方法也用于检测需要重新登录的情形。
        """
        if not cookie or "SUB" not in cookie:
            return False
        try:
            resp = requests.get(
                "https://m.weibo.cn/api/config",
                headers={
                    "User-Agent": MOBILE_UA,
                    "Cookie": cookie,
                    "Referer": "https://m.weibo.cn/",
                },
                timeout=timeout,
            )
            data = resp.json().get("data", {})
            return bool(data.get("login"))
        except Exception:
            return False

    # -----------------------------------------------------------
    # 内部辅助
    # -----------------------------------------------------------

    def _serialize_cookies(self) -> str:
        """
        把 session 中的 Cookie 拼成 'k1=v1; k2=v2' 形式。

        同名 Cookie 可能来自多个域（SSO 跨域残留，例如 SUB 会同时出现在
        login.sina.com.cn / passport.weibo.com / m.weibo.cn 上）。若全部拼进
        Cookie 头，服务器可能取到错误域的 SUB，导致登录态失效（login=false）。
        因此按名字去重，优先保留 m.weibo.cn 域的版本。
        """
        def _domain_score(cookie):
            d = (cookie.domain or "").lower()
            if d == "m.weibo.cn":
                return 3
            if d.endswith(".weibo.cn"):
                return 2
            if "weibo.cn" in d:
                return 1
            return 0

        chosen = {}
        for c in self.session.cookies:
            prev = chosen.get(c.name)
            if prev is None or _domain_score(c) > _domain_score(prev):
                chosen[c.name] = c
        return "; ".join(f"{c.name}={c.value}" for c in chosen.values())

    @staticmethod
    def _print_ascii_qrcode(image_url: str):
        """
        下载二维码图片并在终端打印 ASCII 二维码。
        依赖 Pillow 库（pip install Pillow）。
        """
        try:
            import io
            from PIL import Image
        except ImportError as e:
            raise LoginError(
                "终端显示二维码需要 Pillow 库，"
                '请运行: pip install Pillow'
            ) from e

        # 下载图片
        resp = requests.get(image_url, timeout=15)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content)).convert("L")

        # 缩放并二值化
        width, height = img.size
        # 二维码是正方形
        step = max(1, width // 80)  # 控制终端宽度
        chars = " 01"
        print("\n请用微博 App 扫描下方二维码登录:\n")
        for y in range(0, height, step * 2):  # 行高加倍（终端行比列高）
            line = []
            for x in range(0, width, step):
                pixel = img.getpixel((x, y))
                line.append(chars[1] if pixel < 128 else chars[0])
            print("".join(line))
        print()

    @staticmethod
    def _show_qrcode_window(image_url: str, poller):
        """
        在主线程运行 tkinter mainloop 显示二维码图片；
        poller 是一个 callable，每秒被调用一次（通过 after），
        返回 dict: {"status": ...}，状态为 "confirmed" 时关闭窗口。
        依赖 tkinter（Python 内置）和 Pillow。

        注意：tkinter 必须在主线程跑，所以本方法会阻塞主线程，
        直到窗口关闭（扫码完成或用户手动关闭）。
        """
        import io
        import tkinter as tk
        from PIL import Image, ImageTk

        # 下载图片
        resp = requests.get(image_url, timeout=15)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content))

        # 放大显示（二维码原图较小）
        if img.width < 300:
            img = img.resize((img.width * 2, img.height * 2), Image.NEAREST)

        root = tk.Tk()
        root.title("微博扫码登录 - 用微博 App 扫描下方二维码")
        root.geometry(f"{img.width + 40}x{img.height + 100}")
        root.resizable(False, False)

        # 居中显示
        root.update_idletasks()
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        x = (sw - root.winfo_width()) // 2
        y = (sh - root.winfo_height()) // 2
        root.geometry(f"+{x}+{y}")

        tk_img = ImageTk.PhotoImage(img)
        label = tk.Label(root, image=tk_img, padx=20, pady=10)
        label.image = tk_img  # 防止被 GC
        label.pack()

        hint = tk.Label(
            root,
            text="打开微博 App → 右上角「+」→「扫一扫」\n扫码后在手机上点击「确认登录」",
            font=("Microsoft YaHei", 10),
            pady=8,
        )
        hint.pack()

        status_label = tk.Label(root, text="等待扫码...", fg="#666",
                                font=("Microsoft YaHei", 9))
        status_label.pack(pady=(0, 8))

        result_holder = {"status": "unused", "result": None, "closed": False}

        def _poll():
            if result_holder["closed"]:
                return
            try:
                r = poller()
            except Exception as e:
                status_label.config(text=f"出错: {e}", fg="red")
                root.after(2000, _poll)
                return
            status = r.get("status", "unknown")
            result_holder["status"] = status
            result_holder["result"] = r
            if status == "unused":
                status_label.config(text="等待扫码...", fg="#666")
            elif status == "scanned":
                status_label.config(text="✓ 已扫码，请在手机上点击「确认登录」", fg="#1da1f2")
            elif status == "confirmed":
                status_label.config(text="✓ 已确认，正在登录...", fg="green")
                root.update()
                root.after(300, _close)
                return
            elif status == "expired":
                status_label.config(text="❌ 二维码已过期", fg="red")
                root.after(500, _close)
                return
            else:
                status_label.config(text=f"状态: {status}", fg="#666")
            root.after(1500, _poll)

        def _close():
            result_holder["closed"] = True
            try:
                root.quit()
                root.destroy()
            except tk.TclError:
                pass

        # 窗口被手动关闭
        def _on_close():
            result_holder["closed"] = True
            try:
                root.quit()
                root.destroy()
            except tk.TclError:
                pass
        root.protocol("WM_DELETE_WINDOW", _on_close)

        root.after(500, _poll)
        root.mainloop()

        # mainloop 退出后返回最后的扫码结果
        return result_holder["result"]

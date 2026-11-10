# -*- coding: utf-8 -*-
"""通过 CDP 驱动 Edge/Chrome，在 DeepSeek 开放平台「现取」支付二维码。

思路
    不让用户手动截图。插件自己拉起一个带调试端口的独立浏览器 profile，
    复用里面的登录态，每次要码时打开 /top_up，选好金额与支付宝，点「去支付」，
    等二维码 canvas 出现后把像素读出来，返回 PNG 字节。

为什么是独立 profile
    用主浏览器 profile 需要先关掉用户正在用的浏览器，体验太差。独立 profile
    只需登录一次，之后插件自己管进程，随用随开。
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:  # websockets 是 AstrBot 运行环境自带的
    from websockets.sync.client import connect as _ws_connect
except Exception as _exc:  # noqa: BLE001
    _ws_connect = None
    _WS_IMPORT_ERROR = _exc
else:
    _WS_IMPORT_ERROR = None


TOP_UP_URL = "https://platform.deepseek.com/top_up"
SIGN_IN_URL = "https://platform.deepseek.com/sign_in"
TRANSACTIONS_URL = "https://platform.deepseek.com/transactions"

BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/microsoft-edge",
    "/usr/bin/google-chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


class QrError(Exception):
    """抓码过程中的所有可预期错误。"""


class LoginRequired(QrError):
    """掉登录了，需要人再登录一次。"""


class PageShapeError(QrError):
    """页面长得和预期不一样（改版 / 还没渲染出来）。"""


def _parse_amount(text: str) -> float | None:
    cleaned = (
        str(text or "")
        .replace("¥", "")
        .replace("￥", "")
        .replace(",", "")
        .strip()
    )
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_dt(text: str) -> float | None:
    """账单里的「2026-11-10 17:39:54」按本地时区解析成时间戳。"""
    raw = str(text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(raw, fmt))
        except (ValueError, TypeError):
            continue
    return None


# ------------------------------------------------------------------ CDP 基础


class CdpSession:
    """一个页面级 CDP 连接，短连接、用完就关。"""

    def __init__(self, ws_url: str, timeout: float = 45.0):
        if _ws_connect is None:
            raise QrError(f"缺少 websockets 依赖：{_WS_IMPORT_ERROR}")
        self.ws_url = ws_url
        self.timeout = timeout
        self._ws = None
        self._id = 0

    def __enter__(self) -> CdpSession:
        self._ws = _ws_connect(self.ws_url, max_size=None, open_timeout=self.timeout)
        return self

    def __exit__(self, *exc) -> bool:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        return False

    @property
    def ws(self):
        if self._ws is None:
            raise QrError("CDP 连接还没建立")
        return self._ws

    def call(self, method: str, **params):
        self._id += 1
        msg_id = self._id
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            remaining = max(0.5, deadline - time.time())
            try:
                raw = self.ws.recv(timeout=remaining)
            except TimeoutError as exc:
                raise QrError(f"CDP 调用超时：{method}") from exc
            msg = json.loads(raw)
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise QrError(msg["error"].get("message") or str(msg["error"]))
                return msg.get("result", {})
        raise QrError(f"CDP 调用超时：{method}")

    def eval(self, expression: str, await_promise: bool = True):
        result = self.call(
            "Runtime.evaluate",
            expression=expression,
            returnByValue=True,
            awaitPromise=await_promise,
        )
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            desc = (
                details.get("exception", {}).get("description")
                or details.get("text")
                or "页面脚本抛错"
            )
            raise QrError(str(desc)[:400])
        return result.get("result", {}).get("value")


# ------------------------------------------------------------------ 抓码器


class DeepseekQrFetcher:
    def __init__(
        self,
        *,
        port: int = 9222,
        edge_path: str | None = None,
        profile_dir: str | Path | None = None,
        headless: bool = True,
        offscreen: bool = True,
        timeout: float = 45.0,
        amount: int = 10,
    ) -> None:
        self.port = int(port)
        self.edge_path = (edge_path or "").strip() or self._find_browser()
        self.profile_dir = Path(profile_dir) if profile_dir else None
        self.headless = bool(headless)
        self.timeout = float(timeout)
        self.amount = int(amount)
        # 无头模式会被风控拦（页面报「请求异常」），默认用有头 + 窗口挪出屏幕
        self.offscreen = bool(offscreen)
        self._proc: subprocess.Popen | None = None
        self._launched_headless: bool | None = None

    # ------------------------------------------------------------ 进程管理

    @staticmethod
    def _find_browser() -> str | None:
        for candidate in BROWSER_CANDIDATES:
            if Path(candidate).exists():
                return candidate
        return None

    def _http_json(self, path: str):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=4) as resp:
            return json.load(resp)

    def is_alive(self) -> bool:
        try:
            self._http_json("/json/version")
            return True
        except Exception:  # noqa: BLE001
            return False

    def ensure_browser(self, headless: bool | None = None) -> None:
        """保证调试端口的浏览器在跑；需要时自己拉起一个。"""
        want_headless = self.headless if headless is None else bool(headless)

        if self.is_alive():
            if self._launched_headless is None:
                # 端口上是一个不是我拉起的浏览器（可能是用户手动起的），直接复用
                return
            if self._launched_headless == want_headless:
                return
            # 模式变了：先关掉自己拉的那个，重新起
            self.stop_browser()
        if not self.edge_path:
            raise QrError(
                "没找到 Edge / Chrome，请在插件配置 edge_path 里填浏览器可执行文件路径。"
            )
        if not self.profile_dir:
            raise QrError("调试 profile 目录没配置")

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        args = [
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--hide-crash-restore-bubble",
            "--disable-features=msEdgeSidebarV2,msEdgeFirstRunExperience,EdgeSplitScreen",
        ]
        if want_headless:
            args.append("--headless=new")
        elif self.offscreen:
            # 有头但挪到屏幕外：视觉上不打扰人，又不会被无头风控拦
            args.extend(["--window-position=-3200,-3200", "--window-size=1024,900"])
        args.append("about:blank")

        creationflags = 0
        if os.name == "nt" and want_headless:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            self._proc = subprocess.Popen(
                [self.edge_path, *args],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise QrError(f"启动浏览器失败：{exc}") from exc

        self._launched_headless = want_headless

        deadline = time.time() + 25
        while time.time() < deadline:
            if self.is_alive():
                return
            time.sleep(0.4)
        raise QrError("调试浏览器没有起来（端口没开），可能被杀毒软件拦了。")

    def stop_browser(self) -> None:
        proc = self._proc
        self._proc = None
        self._launched_headless = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        deadline = time.time() + 8
        while time.time() < deadline and self.is_alive():
            time.sleep(0.3)

    def close(self) -> None:
        self.stop_browser()

    def _close_browser_via_cdp(self) -> None:
        """通过 CDP 关掉端口上的任何浏览器实例（包括不是自己拉起的）。"""
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/json/version", timeout=4
            ) as resp:
                info = json.load(resp)
            ws_url = info.get("webSocketDebuggerUrl")
            if not ws_url or _ws_connect is None:
                return
            with _ws_connect(ws_url, max_size=None, open_timeout=10) as ws:
                ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
                try:
                    ws.recv(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
        deadline = time.time() + 10
        while time.time() < deadline and self.is_alive():
            time.sleep(0.3)

    # ------------------------------------------------------------ 页面

    def _pages(self) -> list[dict]:
        try:
            targets = self._http_json("/json/list")
        except Exception:  # noqa: BLE001
            return []
        return [t for t in targets if t.get("type") == "page"]

    def _find_deepseek_page(self) -> dict | None:
        for page in self._pages():
            if "deepseek" in (page.get("url") or ""):
                return page
        return None

    def _new_page(self, url: str) -> dict:
        quoted = urllib.parse.quote(url, safe=":/?=&")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/json/new?{quoted}",
            method="PUT",
        )
        with urllib.request.urlopen(request, timeout=10) as resp:
            return json.load(resp)

    def _page(self) -> dict:
        page = self._find_deepseek_page()
        if page:
            return page
        pages = self._pages()
        if pages:
            return pages[0]
        return self._new_page(TOP_UP_URL)

    def _wait_js(self, session: CdpSession, expr: str, timeout: float | None = None) -> bool:
        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            try:
                if session.eval(f"(() => {{ try {{ return !!({expr}); }} catch (e) {{ return false; }} }})()"):
                    return True
            except QrError:
                pass
            time.sleep(0.35)
        return False

    def _wait_element(self, session: CdpSession, selector: str, timeout: float = 20) -> bool:
        expr = (
            "(() => { try { return !!document.querySelector(%s); } catch (e) { return false; } })()"
            % json.dumps(selector)
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if session.eval(expr):
                    return True
            except QrError:
                pass
            time.sleep(0.35)
        return False

    def _navigate(self, session: CdpSession, url: str) -> None:
        session.call("Page.enable")
        session.call("Page.navigate", url=url)
        self._wait_js(session, "document.readyState === 'complete'", timeout=30)
        time.sleep(1.0)  # 给前端框架一点渲染时间

    def _check_login(self, session: CdpSession) -> None:
        deadline = time.time() + 25
        while time.time() < deadline:
            href = str(session.eval("location.href") or "")
            if "sign_in" in href:
                raise LoginRequired(
                    "调试浏览器里的 DeepSeek 登录态失效了，需要主人重新登录一次"
                    "（用「/讨大白饭 登录」打开登录窗口）。"
                )
            if "top_up" in href:
                break
            time.sleep(0.4)
        else:
            raise PageShapeError("页面没有停在充值页，可能被重定向了。")

        if not self._wait_element(session, 'input[name="topUpAmount"]', timeout=18):
            raise PageShapeError("充值页没渲染出金额档位（可能登录态失效或页面改版）。")

    # ------------------------------------------------------------ 具体操作

    def _click_radio(self, session: CdpSession, name: str, value: str) -> bool:
        expr = (
            "(() => { const el = document.querySelector('input[name=\"%s\"][value=\"%s\"]');"
            " if (!el) return false; (el.closest('label') || el).click(); return true; })()"
        ) % (name, value)
        return bool(session.eval(expr))

    def _dismiss_cookie_banner(self, session: CdpSession) -> None:
        try:
            session.eval(
                """(() => {
                    const b = [...document.querySelectorAll('button,[role=button]')]
                        .find(e => /接受全部|全部接受|^Accept all$/i.test((e.innerText || '').trim()));
                    if (b) { b.click(); return true; }
                    return false;
                })()"""
            )
        except QrError:
            pass

    def _select_amount(self, session: CdpSession) -> None:
        if not self._click_radio(session, "topUpAmount", str(self.amount)):
            raise PageShapeError(f"充值页上找不到 {self.amount} 元档位。")
        time.sleep(0.35)

    def _select_alipay(self, session: CdpSession) -> None:
        if not self._click_radio(session, "paymentMethod", "alipay"):
            raise PageShapeError("充值页上找不到支付宝支付方式。")
        time.sleep(0.35)

    def _submit(self, session: CdpSession) -> None:
        ok = session.eval(
            """(() => {
                const b = [...document.querySelectorAll('button,[role=button]')]
                    .find(e => (e.innerText || '').trim() === '去支付');
                if (!b) return false;
                b.click();
                return true;
            })()"""
        )
        if not ok:
            raise PageShapeError("找不到「去支付」按钮。")

    @staticmethod
    def _decode_data_url(data_url: str) -> bytes:
        if data_url.startswith("data:image"):
            _, _, b64 = data_url.partition(",")
            return base64.b64decode(b64)
        if data_url.startswith("http"):
            with urllib.request.urlopen(data_url, timeout=10) as resp:
                return resp.read()
        raise QrError("二维码数据格式看不懂")

    def _grab_qr(self, session: CdpSession) -> bytes:
        """等二维码出现，把它的像素读出来（canvas 优先，img 兜底）。"""
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            payload = session.eval(
                """(() => {
                    const cs = [...document.querySelectorAll('canvas')]
                        .filter((c) => c.width >= 100 && c.height >= 100);
                    if (cs.length) {
                        try {
                            return { kind: 'canvas', data: cs[cs.length - 1].toDataURL('image/png') };
                        } catch (e) { /* tainted canvas, fall through */ }
                    }
                    const imgs = [...document.querySelectorAll('img')].filter((el) => {
                        const src = el.src || '';
                        return el.naturalWidth >= 100 && el.naturalHeight >= 100 &&
                            (src.startsWith('data:image') ||
                             /qr|code/i.test(src + (el.alt || '') + (el.className || '')));
                    });
                    if (imgs.length) return { kind: 'img', data: imgs[imgs.length - 1].src };
                    return null;
                })()"""
            )
            if payload and payload.get("data"):
                return self._decode_data_url(str(payload["data"]))
            time.sleep(0.45)
        raise PageShapeError("等了半天二维码没出来（弹窗没弹？）。")

    # ------------------------------------------------------------ 对外接口

    def open_login_window(self) -> None:
        """拉起一个「在屏幕里」的有头浏览器并停在登录页，等人手动登录。"""
        # 先关掉当前实例（可能正在屏幕外跑着），不然没法用可见窗口重新打开
        if self.is_alive():
            self._close_browser_via_cdp()
        self.offscreen = False
        self._launched_headless = None
        self.ensure_browser(headless=False)
        page = self._page()
        with CdpSession(page["webSocketDebuggerUrl"], timeout=self.timeout) as session:
            self._navigate(session, SIGN_IN_URL)

    def fetch(self) -> bytes:
        """现取一张支付二维码，返回 PNG 字节。"""
        self.ensure_browser()
        page = self._page()
        with CdpSession(page["webSocketDebuggerUrl"], timeout=self.timeout) as session:
            # 窗口在屏幕外时会被判定「后台」，页面定时器会被节流，假装它在前台
            try:
                session.call("Emulation.setFocusEmulationEnabled", enabled=True)
            except QrError:
                pass
            self._navigate(session, TOP_UP_URL)
            self._dismiss_cookie_banner(session)
            self._check_login(session)
            self._select_amount(session)
            self._select_alipay(session)
            self._submit(session)
            return self._grab_qr(session)

    # ------------------------------------------------------------ 账单（查支付）

    def _check_signed_in(self, session: CdpSession) -> None:
        href = str(session.eval("location.href") or "")
        if "sign_in" in href:
            raise LoginRequired(
                "调试浏览器里的 DeepSeek 登录态失效了，需要主人重新登录一次"
                "（用「/讨大白饭 登录」打开登录窗口）。"
            )

    def read_transactions(self, max_rows: int = 20) -> list[dict]:
        """读「账单 → 充值账单」的订单列表。

        返回 [{order_id, status, amount, method, created_text, created_at}]，
        其中 status 是页面上的原文（「成功」/「已取消」…），amount 是 float 元。
        """
        self.ensure_browser()
        page = self._page()
        with CdpSession(page["webSocketDebuggerUrl"], timeout=self.timeout) as session:
            try:
                session.call("Emulation.setFocusEmulationEnabled", enabled=True)
            except QrError:
                pass
            self._navigate(session, TRANSACTIONS_URL)
            self._check_signed_in(session)
            if not self._wait_element(session, "tbody tr", timeout=20):
                return []
            expr = (
                """(() => [...document.querySelectorAll('tbody tr')]
                    .map(tr => [...tr.querySelectorAll('td')]
                        .map(td => (td.innerText || '').replace(/\\s+/g, ' ').trim()))
                    .filter(cells => cells.length >= 5)
                    .slice(0, %d))()"""
            ) % max(1, int(max_rows))
            rows = session.eval(expr) or []

        out: list[dict] = []
        for cells in rows:
            order_id, status, amount_text, method, created_text = cells[:5]
            out.append(
                {
                    "order_id": order_id,
                    "status": status,
                    "amount": _parse_amount(amount_text),
                    "method": method,
                    "created_text": created_text,
                    "created_at": _parse_dt(created_text),
                }
            )
        return out

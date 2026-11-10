# -*- coding: utf-8 -*-
"""讨大白饭。

一句 /讨大白饭，机器人就现去 DeepSeek 开放平台（platform.deepseek.com/top_up）
捞一张「充 10 元」的支付宝支付二维码，画成一张讨饭卡片递到群里。

为什么不放一张固定的图
    支付二维码是动态的，支付宝那边 15 分钟就作废。所以插件用一个独立的浏览器
    profile（放在插件数据目录里，登录一次就行）去工作台现取，每次都是新的。

指令
    /讨大白饭          现取一张二维码发到群里
    /讨大白饭 登录     打开看得见的登录窗口（管理员，掉登录时用）
    /讨大白饭 状态     浏览器/登录/最近一次抓码情况（管理员）
    /讨大白饭 刷新     丢掉缓存，重新捞一张（管理员）
    /讨大白饭 我是谁   看自己的 openid，方便填 admin_openids
    /讨大白饭 帮助     用法
"""

from __future__ import annotations

import asyncio
import io
import random
import re
import time
from pathlib import Path

from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import GreedyStr

try:
    from .deepseek_qr import DeepseekQrFetcher, LoginRequired, QrError
except ImportError:  # 插件被当成独立脚本加载时的兜底
    import sys

    _PLUGIN_DIR = Path(__file__).resolve().parent
    if str(_PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(_PLUGIN_DIR))
    from deepseek_qr import DeepseekQrFetcher, LoginRequired, QrError

PLUGIN_NAME = "astrbot_plugin_tao_da_bai_fan"
PLUGIN_DIR = Path(__file__).resolve().parent
MASCOT_PATH = PLUGIN_DIR / "assets" / "mascot.png"

DEFAULT_THANK_TEXTS = """\
到账啦！¥{amount} 白饭钱收下，谢谢投喂本鱼的大善人 (๑˃̵ᴗ˂̵)و
呜哇，¥{amount} 到账！不知道是哪位好心人，本鱼先端起碗道个谢～
白饭 +¥{amount}，本鱼的碗又满了——谢谢这位见义勇为的饭票侠！
叮，收到 ¥{amount} 投喂！本鱼这就把饭吃香一点，下次还来讨。
"""

# ---------------------------------------------------------------- 配置小工具


def _cfg_str(config, key: str, default: str = "") -> str:
    value = config.get(key, default) if hasattr(config, "get") else default
    if value is None:
        return default
    return str(value).strip()


def _cfg_int(config, key: str, default: int) -> int:
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError, AttributeError):
        return default


def _cfg_bool(config, key: str, default: bool) -> bool:
    value = config.get(key, default) if hasattr(config, "get") else default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "是", "开", "启用"}
    return bool(value)


def _ids(value: str) -> list[str]:
    if not value:
        return []
    return [part for part in re.split(r"[,，;；\s、|]+", str(value)) if part]


def match_paid_orders(rows: list[dict], watchers: list[dict]) -> list[tuple[dict, dict]]:
    """把「已成功」的订单和发码工单对上号。

    - 只认状态里带「成功」的订单；
    - 金额要对得上（账单行解析不出金额时不卡金额）；
    - 订单创建时间与发码时间的差在 150 秒内（解析不出时间时不卡时间）；
    - 一笔订单最多认一个工单，取时间最接近的那个。
    """
    matched: list[tuple[dict, dict]] = []
    for row in rows:
        if "成功" not in str(row.get("status") or ""):
            continue
        amount = row.get("amount")
        created_at = row.get("created_at")
        candidates = [
            watcher
            for watcher in watchers
            if (amount is None or abs(float(amount) - watcher["amount"]) < 0.01)
            and (created_at is None or abs(created_at - watcher["at"]) <= 150)
        ]
        if not candidates:
            continue
        candidates.sort(key=lambda watcher: watcher["at"])
        matched.append((candidates[-1], row))
    return matched


def _human_delta(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 小时"
    return f"{hours // 24} 天"


# ---------------------------------------------------------------- 卡片渲染

BG = (255, 249, 238)
INK = (56, 42, 32)
MUTED = (152, 140, 122)
ACCENT = (214, 90, 49)
BORDER = (238, 226, 202)
WARN = (196, 60, 44)

_FONT_BOLD = [
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyhbd.ttf",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]
_FONT_REGULAR = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhl.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]
_FONT_CACHE: dict[tuple[int, bool], object] = {}


def _font(size: int, bold: bool = False):
    key = (size, bold)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached

    for candidate in _FONT_BOLD if bold else _FONT_REGULAR:
        try:
            if Path(candidate).exists():
                font = ImageFont.truetype(candidate, size)
                _FONT_CACHE[key] = font
                return font
        except Exception:  # noqa: BLE001 - 字体加载失败就换下一个
            continue

    try:
        font = ImageFont.load_default(size)
    except TypeError:
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _with_quiet_zone(img: PILImage.Image, ratio: float = 0.12) -> PILImage.Image:
    """给二维码补白边（静区）。工作台给的 canvas 是没留白边的，直接发容易扫不动。"""
    img = img.convert("RGB")
    width, height = img.size
    side = int(round(max(width, height) * (1 + ratio * 2)))
    canvas = PILImage.new("RGB", (side, side), (255, 255, 255))
    scale = side / max(width, height)
    resized = img.resize(
        (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
        PILImage.LANCZOS,
    )
    resized = resized.convert("L").point(lambda v: 0 if v < 140 else 255).convert("RGB")
    canvas.paste(resized, ((side - resized.width) // 2, (side - resized.height) // 2))
    return canvas


def _load_mascot() -> PILImage.Image | None:
    """读卡片顶部的表情包；没有就返回 None，退回纯文字版式。"""
    try:
        if MASCOT_PATH.exists():
            return PILImage.open(MASCOT_PATH).convert("RGB")
    except Exception:  # noqa: BLE001
        pass
    return None


def render_card(
    qr_img: PILImage.Image,
    *,
    title: str,
    subtitle: str,
    footer: str,
    note: str,
    warn: str = "",
) -> bytes:
    """把二维码渲染成一张讨饭卡片，返回 PNG 字节。

    有 assets/mascot.png 时顶部放那张表情包（大肥鱼端着白饭），
    没有就退回「横条 + 标题 + 副标题」的老版式。
    """
    card_w = 760
    pad = 32
    qr_box = 480
    qr_inner = 428

    img = PILImage.new("RGB", (card_w, 1600), BG)
    draw = ImageDraw.Draw(img)

    def center(text: str, y: float, font, fill) -> float:
        bbox = draw.textbbox((0, 0), text, font=font)
        width = bbox[2] - bbox[0]
        draw.text(
            (card_w / 2 - width / 2 - bbox[0], y - bbox[1]),
            text,
            font=font,
            fill=fill,
        )
        return bbox[3] - bbox[1]

    y = float(pad)

    mascot = _load_mascot()
    if mascot is not None:
        m_size = 560
        m_img = mascot.resize((m_size, m_size), PILImage.LANCZOS)
        mask = PILImage.new("L", (m_size, m_size), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, m_size - 1, m_size - 1], radius=34, fill=255
        )
        img.paste(m_img, ((card_w - m_size) // 2, int(y)), mask)
        y += m_size + 20
    else:
        bar = 96.0
        draw.rounded_rectangle(
            [card_w / 2 - bar / 2, y, card_w / 2 + bar / 2, y + 8],
            radius=4,
            fill=ACCENT,
        )
        y += 8 + 16
        y += center(title, y, _font(46, True), INK) + 12
        y += center(subtitle, y, _font(24), MUTED) + 22

    # 二维码白框
    qx = (card_w - qr_box) // 2
    draw.rounded_rectangle(
        [qx, y, qx + qr_box, y + qr_box],
        radius=22,
        fill=(255, 255, 255),
        outline=BORDER,
        width=2,
    )
    q = _with_quiet_zone(qr_img).resize((qr_inner, qr_inner), PILImage.LANCZOS)
    q = q.convert("L").point(lambda v: 0 if v < 140 else 255).convert("RGB")
    img.paste(q, (qx + (qr_box - qr_inner) // 2, int(y) + (qr_box - qr_inner) // 2))
    y += qr_box + 22

    y += center(footer, y, _font(28, True), ACCENT) + 8
    y += center(note, y, _font(20), MUTED)
    if warn:
        y += 12
        y += center(warn, y, _font(21, True), WARN)

    bottom = min(1600, int(y + pad))
    canvas = img.crop((0, 0, card_w, bottom))

    buf = io.BytesIO()
    canvas.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------- 插件本体


@register(
    PLUGIN_NAME,
    "大肥鱼",
    "讨大白饭：一句 /讨大白饭，机器人现去 DeepSeek 开放平台捞一张「充 10 元」的支付宝二维码，"
    "画成卡片递到群里。码是实时的，掉登录时发「/讨大白饭 登录」重新登一次。",
    "1.1.1",
    "https://github.com/xuanRanDesu/astrbot_plugin_tao_da_bai_fan",
)
class TaoDaBaiFanPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._lock = asyncio.Lock()
        self._cache: tuple[bytes, float] | None = None
        self._cooldowns: dict[str, float] = {}
        self._fetcher_obj: DeepseekQrFetcher | None = None
        self._last_ok_at: float = 0.0
        self._last_error: str = ""

        # 支付监听：发码后挂一个工单，盯着账单里这笔订单什么时候变成「成功」
        self._watchers: list[dict] = []
        self._thanked: set[str] = set()
        self._thanked_loaded = False
        self._watch_task: asyncio.Task | None = None
        self._shutdown = False
        self._watched_last_at: float = 0.0

    # ------------------------------------------------------------ 存储 / 浏览器

    def _data_dir(self) -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

            root = Path(get_astrbot_plugin_data_path())
        except Exception:  # noqa: BLE001
            root = Path.home() / ".astrbot" / "data" / "plugin_data"
        path = root / PLUGIN_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _profile_dir(self) -> Path:
        path = self._data_dir() / "edge-profile"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _fetcher(self) -> DeepseekQrFetcher:
        if self._fetcher_obj is None:
            self._fetcher_obj = DeepseekQrFetcher(
                port=_cfg_int(self.config, "cdp_port", 9222),
                edge_path=_cfg_str(self.config, "edge_path", ""),
                profile_dir=self._profile_dir(),
                headless=_cfg_bool(self.config, "headless", False),
                offscreen=_cfg_bool(self.config, "offscreen", True),
                timeout=float(max(10, _cfg_int(self.config, "fetch_timeout_seconds", 60))),
                amount=_cfg_int(self.config, "amount", 10),
            )
        return self._fetcher_obj

    # ------------------------------------------------------------ 权限 / 冷却

    def _scope_reason(self, event: AstrMessageEvent) -> str | None:
        if event.is_private_chat():
            if not _cfg_bool(self.config, "allow_private", True):
                return "本鱼只在群里讨饭，私聊不讨（主人可以在插件配置里打开 allow_private）。"
            return None

        allowed = _ids(_cfg_str(self.config, "allowed_groups", ""))
        group_id = str(event.get_group_id() or "")
        if allowed and group_id not in allowed:
            return "这个群不在讨饭名单里，本鱼不在这儿讨饭。"
        return None

    def _cooldown_left(self, event: AstrMessageEvent) -> int:
        cooldown = _cfg_int(self.config, "cooldown_seconds", 30)
        if cooldown <= 0:
            return 0
        key = event.unified_msg_origin
        last = self._cooldowns.get(key, 0.0)
        left = cooldown - (time.time() - last)
        return int(left) if left > 0 else 0

    def _can_admin(self, event: AstrMessageEvent) -> bool:
        if _cfg_bool(self.config, "allow_anyone_admin", False):
            return True
        if event.is_admin():
            return True
        return str(event.get_sender_id() or "") in _ids(
            _cfg_str(self.config, "admin_openids", "")
        )

    def _perm_hint(self) -> str:
        return (
            "只有主人能操作这些。如果你是主人，二选一即可：\n"
            "① 在 AstrBot 面板把自己设成管理员；\n"
            "② 在插件配置的 admin_openids 里填上你的 openid（群里发「/讨大白饭 我是谁」能看到）。"
        )

    # ------------------------------------------------------------ 抓码

    async def _fetch_qr(self) -> bytes:
        async with self._lock:
            cache_seconds = _cfg_int(self.config, "cache_seconds", 0)
            if cache_seconds > 0 and self._cache:
                cached, at = self._cache
                if time.time() - at < cache_seconds:
                    return cached

            fetcher = self._fetcher()
            png = await asyncio.to_thread(fetcher.fetch)
            self._cache = (png, time.time())
            self._last_ok_at = time.time()
            self._last_error = ""
            return png

    def _render(self, qr_png: bytes, amount: int) -> bytes:
        note = _cfg_str(self.config, "note", "二维码有效期约 15 分钟，过期就再发一次")
        if not _cfg_bool(self.config, "card_enabled", True):
            return qr_png

        footer_template = _cfg_str(
            self.config, "footer", "支付宝扫一扫 · {amount} 元"
        )
        footer = footer_template.replace("{amount}", str(amount))
        qr_img = PILImage.open(io.BytesIO(qr_png))
        return render_card(
            qr_img,
            title=_cfg_str(self.config, "title", "讨大白饭"),
            subtitle=_cfg_str(self.config, "subtitle", "大肥鱼饿了，赏一碗白饭吧"),
            footer=footer,
            note=note,
        )

    async def _send_now(self, event: AstrMessageEvent):
        amount = _cfg_int(self.config, "amount", 10)
        if _cfg_bool(self.config, "send_working_hint", True):
            yield event.plain_result("本鱼去工作台捞二维码了，稍等几秒钟…")

        try:
            qr_png = await self._fetch_qr()
        except LoginRequired as exc:
            self._last_error = str(exc)
            yield event.plain_result(
                f"呜……{exc}\n主人发「/讨大白饭 登录」重新登一下就好。"
            )
            return
        except QrError as exc:
            self._last_error = str(exc)
            self.logger.warning("讨大白饭：抓码失败：%s", exc)
            yield event.plain_result(
                f"捞码翻车了：{exc}\n（主人可以先发「/讨大白饭 状态」看看卡在哪）"
            )
            return
        except Exception as exc:  # noqa: BLE001
            self._last_error = repr(exc)
            self.logger.exception("讨大白饭：抓码出现未预期错误")
            yield event.plain_result(f"捞码的时候出了点怪事：{exc}")
            return

        try:
            card = self._render(qr_png, amount)
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("讨大白饭：卡片渲染失败")
            # 渲染挂了也要把码发出去
            card = qr_png

        yield event.chain_result([Image.fromBytes(card)])
        await self._maybe_start_watch(event, amount)

    # ------------------------------------------------------------ 支付监听

    def _load_thanked(self) -> None:
        if self._thanked_loaded:
            return
        self._thanked_loaded = True
        try:
            state = self._read_state()
            self._thanked = {str(x) for x in (state.get("thanked_orders") or []) if x}
        except Exception:  # noqa: BLE001
            self._thanked = set()

    def _persist_thanked(self) -> None:
        state = self._read_state()
        state["thanked_orders"] = sorted(self._thanked)[-100:]
        if self._thanked:
            state["last_thanked_at"] = int(time.time())
        self._write_state(state)

    async def _maybe_start_watch(self, event: AstrMessageEvent, amount: int) -> None:
        if not _cfg_bool(self.config, "enable_thanks", True):
            return
        self._load_thanked()
        self._watchers.append(
            {
                "umo": event.unified_msg_origin,
                "at": time.time(),
                "amount": float(amount),
                "done": False,
            }
        )
        self._watchers = self._watchers[-20:]
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = asyncio.create_task(self._watch_loop())

    async def _watch_loop(self) -> None:
        """后台盯着账单：发出去的那笔订单一旦变成「成功」，就去对应群里道谢。"""
        poll = max(8, _cfg_int(self.config, "poll_interval_seconds", 20))
        watch_sec = max(60, _cfg_int(self.config, "watch_minutes", 20) * 60)

        while not self._shutdown:
            await asyncio.sleep(poll)

            now = time.time()
            for watcher in self._watchers:
                if not watcher["done"] and now - watcher["at"] > watch_sec:
                    watcher["done"] = True

            pending = [w for w in self._watchers if not w["done"]]
            if not pending:
                self._watchers = []
                continue

            try:
                rows = await self._read_transactions_locked()
            except LoginRequired as exc:
                self._last_error = str(exc)
                self.logger.warning("讨大白饭：监听支付时登录态失效：%s", exc)
                continue
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("讨大白饭：监听支付出错：%s", exc)
                continue

            self._watched_last_at = time.time()

            for target, row in match_paid_orders(rows, pending):
                order_id = str(row.get("order_id") or "")
                if order_id and order_id in self._thanked:
                    continue
                target["done"] = True
                if order_id:
                    self._thanked.add(order_id)
                self._persist_thanked()
                pending = [w for w in pending if not w["done"]]
                await self._send_thanks(target["umo"], row)

            self._watchers = [w for w in self._watchers if not w["done"]]

    async def _read_transactions_locked(self) -> list[dict]:
        async with self._lock:
            return await asyncio.to_thread(self._fetcher().read_transactions)

    async def _send_thanks(self, umo: str, row: dict) -> None:
        raw_texts = _cfg_str(self.config, "thank_texts", DEFAULT_THANK_TEXTS)
        texts = [line.strip() for line in raw_texts.splitlines() if line.strip()]
        template = random.choice(texts) if texts else DEFAULT_THANK_TEXTS.splitlines()[0]

        amount = row.get("amount")
        if amount is None:
            amount = _cfg_int(self.config, "amount", 10)
        amount_text = f"{float(amount):g}" if isinstance(amount, (int, float)) else str(amount)
        text = template.replace("{amount}", amount_text)

        try:
            await self.context.send_message(umo, MessageChain().message(text))
            self.logger.info(
                "讨大白饭：已向 %s 发送感谢（订单 %s，%s 元）",
                umo,
                row.get("order_id"),
                amount_text,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("讨大白饭：发送感谢失败（%s）：%s", umo, exc)

    async def terminate(self) -> None:
        self._shutdown = True
        task = self._watch_task
        self._watch_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        try:
            if self._fetcher_obj is not None:
                await asyncio.to_thread(self._fetcher_obj.close)
        except Exception:  # noqa: BLE001
            pass

    def _bump_sent(self) -> None:
        state = self._read_state()
        state["sent"] = int(state.get("sent", 0) or 0) + 1
        state["last_sent_at"] = int(time.time())
        self._write_state(state)

    def _state_path(self) -> Path:
        return self._data_dir() / "state.json"

    def _read_state(self) -> dict:
        path = self._state_path()
        if not path.exists():
            return {}
        try:
            import json

            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _write_state(self, state: dict) -> None:
        import json

        try:
            self._state_path().write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("讨大白饭：写状态文件失败：%s", exc)

    # ------------------------------------------------------------ 指令实现

    async def _handle_send(self, event: AstrMessageEvent):
        if not _cfg_bool(self.config, "enabled", True):
            yield event.plain_result("讨饭功能被关掉了（插件配置里 enabled = false）。")
            return

        reason = self._scope_reason(event)
        if reason:
            yield event.plain_result(reason)
            return

        left = self._cooldown_left(event)
        if left:
            yield event.plain_result(f"呜……刚讨过啦，{left} 秒后再来（本鱼的碗还没空呢）")
            return

        self._cooldowns[event.unified_msg_origin] = time.time()
        async for result in self._send_now(event):
            yield result
        self._bump_sent()

    async def _handle_login(self, event: AstrMessageEvent):
        if not self._can_admin(event):
            yield event.plain_result(self._perm_hint())
            return
        try:
            await asyncio.to_thread(self._fetcher().open_login_window)
        except QrError as exc:
            yield event.plain_result(f"登录窗口没打开：{exc}")
            return
        yield event.plain_result(
            "已经弹出 DeepSeek 登录窗口（这次是看得见的那种）。\n"
            "登录完回来说一声，再发 /讨大白饭 就能捞到码了。"
        )

    async def _handle_status(self, event: AstrMessageEvent):
        if not self._can_admin(event):
            yield event.plain_result(self._perm_hint())
            return

        fetcher = self._fetcher()
        try:
            alive = await asyncio.to_thread(fetcher.is_alive)
        except Exception:  # noqa: BLE001
            alive = False

        state = self._read_state()
        last_ok = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(self._last_ok_at)
        ) if self._last_ok_at else "还没成功抓过"
        last_ok_age = (
            f"（{_human_delta(time.time() - self._last_ok_at)}前）"
            if self._last_ok_at
            else ""
        )
        cache_seconds = _cfg_int(self.config, "cache_seconds", 0)
        cache_text = "不缓存（每次现捞）" if cache_seconds <= 0 else f"{cache_seconds} 秒"
        last_sent = state.get("last_sent_at")
        last_sent_text = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(float(last_sent)))
            if last_sent
            else "还没发过"
        )

        self._load_thanked()
        thanks_on = _cfg_bool(self.config, "enable_thanks", True)
        pending_watch = len([w for w in self._watchers if not w["done"]])
        watched_last = (
            f"{_human_delta(time.time() - self._watched_last_at)}前"
            if self._watched_last_at
            else "还没检查过"
        )

        yield event.plain_result(
            "讨大白饭 · 运行状态\n"
            f"调试浏览器：{'在跑' if alive else '没在跑（下次发码会自动拉起）'}\n"
            f"调试端口：{_cfg_int(self.config, 'cdp_port', 9222)}\n"
            f"profile：{self._profile_dir()}\n"
            f"浏览器：{fetcher.edge_path or '没找到，请配置 edge_path'}\n"
            f"最近成功抓码：{last_ok}{last_ok_age}\n"
            f"上次错误：{self._last_error or '无'}\n"
            f"二维码缓存：{cache_text}\n"
            f"支付监听：{'开' if thanks_on else '关'}"
            f"（每 {_cfg_int(self.config, 'poll_interval_seconds', 20)} 秒查一次账单，"
            f"等 {_cfg_int(self.config, 'watch_minutes', 20)} 分钟）\n"
            f"待监听订单：{pending_watch} 个，上次查账单：{watched_last}\n"
            f"已道谢订单：{len(self._thanked)} 笔\n"
            f"累计发送：{int(state.get('sent', 0) or 0)} 次（最近一次 {last_sent_text}）"
        )

    async def _handle_refresh(self, event: AstrMessageEvent):
        if not self._can_admin(event):
            yield event.plain_result(self._perm_hint())
            return
        self._cache = None
        async for result in self._send_now(event):
            yield result
        self._bump_sent()

    async def _handle_whoami(self, event: AstrMessageEvent):
        yield event.plain_result(
            "你是谁（配 admin_openids 用）\n"
            f"平台：{event.get_platform_name()}\n"
            f"会话类型：{'私聊' if event.is_private_chat() else '群聊'}\n"
            f"群号：{event.get_group_id() or '-'}\n"
            f"你的 openid / QQ：{event.get_sender_id()}\n"
            f"会话标识：{event.unified_msg_origin}\n"
            f"管理员：{'是' if event.is_admin() else '否'}"
        )

    def _help_text(self) -> str:
        return (
            "讨大白饭 · 用法\n"
            "/讨大白饭          现去工作台捞一张二维码发到群里\n"
            "/讨大白饭 登录     掉登录时打开可见窗口重新登录（主人）\n"
            "/讨大白饭 状态     浏览器 / 登录 / 最近抓码情况（主人）\n"
            "/讨大白饭 刷新     丢掉缓存重新捞一张（主人）\n"
            "/讨大白饭 我是谁   看自己的 openid，方便填配置\n"
            "小提示：支付二维码 15 分钟就作废，过期再发一次就有新的。"
        )

    # ------------------------------------------------------------ 指令入口

    @filter.command(
        "讨大白饭",
        alias={"讨饭", "讨白饭", "赏口饭"},
        desc="现去 DeepSeek 工作台捞一张「充 10 元」的支付二维码发到群里",
    )
    async def cmd_tao_da_bai_fan(self, event: AstrMessageEvent, action: GreedyStr):
        action_text = (action or "").strip()
        lowered = action_text.lower()

        if lowered in ("帮助", "help", "用法", "?", "？"):
            yield event.plain_result(self._help_text())
            return
        if lowered in ("登录", "login", "重新登录"):
            async for result in self._handle_login(event):
                yield result
            return
        if lowered in ("状态", "status", "info"):
            async for result in self._handle_status(event):
                yield result
            return
        if lowered in ("刷新", "refresh", "重新捞", "换一张"):
            async for result in self._handle_refresh(event):
                yield result
            return
        if lowered in ("我是谁", "whoami"):
            async for result in self._handle_whoami(event):
                yield result
            return

        async for result in self._handle_send(event):
            yield result


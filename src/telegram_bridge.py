"""Telegram 双向桥：主播私聊 ↔ 直播间聊天。

方向一（Telegram → 直播间）：
    只处理来自 ``TELEGRAM_CHAT_ID`` 的文本消息，以昵称「主播」写入 SQLite
    并广播给所有观众。telebot 是同步库，轮询放在独立线程里跑；线程内不做
    任何异步/共享状态操作，只把协程投递回主事件循环（见 _handle_host_message）。

方向二（直播间 → Telegram）：
    观众消息经 ``forward_message`` 转发到 ``TELEGRAM_CHAT_ID``。同步的
    ``send_message`` 用 ``asyncio.to_thread`` 挪出事件循环，绝不阻塞广播。

配置缺失（token 或 chat id 任一为空）时整个模块为 no-op：不启动轮询、
不转发，且不会让应用启动失败。

刻意不 import ``src.api``：publish 回调由 api 在启动时注入，避免循环导入。
"""

import asyncio
import logging
import threading
import time
from typing import Awaitable, Callable

import telebot

from src import webhook
from src.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

# 主播消息正文的截断上限。与 api.py 里聊天正文的 300 上限保持一致，
# 否则同一条消息从两个入口进入直播间的长度会不一致。
_MAX_TEXT_LEN = 300

# 轮询线程、bot 实例与主事件循环。start() 时填入，stop() 时清空。
_thread: threading.Thread | None = None
_bot: "telebot.TeleBot | None" = None
_loop: asyncio.AbstractEventLoop | None = None
# 由 api.py 注入的「发布主播消息」回调（内部做写库 + 广播）。
_publish_host_message: Callable[[str, int], Awaitable[None]] | None = None


def _configured() -> bool:
    """token 与 chat id 都配置齐才算启用；任一缺失即整体关闭。"""
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def _format_audience_text(message: dict) -> str:
    """把观众消息格式化成发给主播的文本：``{name} {ip}: {time}\\n{text}``。

    - 未共享 IP 时 ip 为空，输出 ``{name}: {time}\\n{text}``（去掉多余空格）。
    - 时间复用 webhook 的格式化逻辑（本地时区 ISO 8601，精确到秒），
      避免两边出现不一致的时间格式。
    """
    name = str(message.get("name") or "")
    ip = message.get("ip")
    time_str = webhook.format_time(message.get("time"))
    text = str(message.get("text") or "")
    if ip:
        return f"{name} {ip}: {time_str}\n{text}"
    return f"{name}: {time_str}\n{text}"


def _log_publish_result(future) -> None:
    """回调在事件循环线程内执行，用来取回发布协程的异常并记日志。

    若不取回，future 里悬挂的异常会触发 "exception was never retrieved" 警告，
    而主播消息其实静默丢了——这正是最难排查的情况。
    """
    try:
        future.result()
    except Exception as exc:  # noqa: BLE001 - 发布失败不影响轮询线程
        logger.warning("主播消息发布失败：%s", exc)


def _handle_host_message(message) -> None:
    """telebot 消息回调（运行在轮询线程内，必须快速返回）。

    只接受来自指定 chat 的文本；随后用 run_coroutine_threadsafe 把
    「写库 + 广播」协程投递回主事件循环。绝不在此线程直接碰 aiosqlite
    或 WebSocket 集合——那两者都不是线程安全的。
    """
    # chat.id 统一转成字符串比较：群组/频道 id 为负数，字符串比较最稳。
    if str(message.chat.id) != TELEGRAM_CHAT_ID:
        return
    raw = message.text
    if not isinstance(raw, str):
        return
    text = raw.strip()[:_MAX_TEXT_LEN]
    if not text:
        return

    if _loop is None or _publish_host_message is None:
        return
    created_at = int(time.time() * 1000)
    try:
        future = asyncio.run_coroutine_threadsafe(
            _publish_host_message(text, created_at), _loop
        )
    except RuntimeError:
        # 主循环已关闭（应用正在退出），丢弃该消息即可，不影响退出流程。
        logger.warning("主事件循环不可用，主播消息未发布")
        return
    future.add_done_callback(_log_publish_result)


def _run_polling() -> None:
    """轮询线程入口。infinity_polling 自带异常重试，正常只有 stop() 才会退出。"""
    assert _bot is not None
    try:
        _bot.infinity_polling(
            timeout=10,
            long_polling_timeout=5,
            logger_level=logging.ERROR,
        )
    except Exception as exc:  # noqa: BLE001 - 线程内异常不能冒泡，否则静默丢栈
        logger.warning("Telegram 轮询线程异常退出：%s", exc)


def start(
    loop: asyncio.AbstractEventLoop,
    publish_host_message: Callable[[str, int], Awaitable[None]],
) -> None:
    """启动 Telegram 桥（配置缺失时为 no-op）。

    loop 用于把轮询线程里的协程投递回主事件循环；publish_host_message
    是 api 注入的异步回调，负责写库与广播。
    """
    global _thread, _bot, _loop, _publish_host_message

    if not _configured():
        logger.info("Telegram 桥未配置，已禁用（需同时设置 TELEGRAM_BOT_TOKEN 与 TELEGRAM_CHAT_ID）")
        return
    if _thread is not None and _thread.is_alive():
        return  # 已在运行，避免重复启动

    _loop = loop
    _publish_host_message = publish_host_message
    try:
        # threaded=False：handler 直接在轮询线程执行。我们的 handler 只做
        # 协程投递、不阻塞，无需 telebot 额外的工作线程池。
        _bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode=None, threaded=False)
    except Exception as exc:  # noqa: BLE001 - 配置有误也不能让应用启动失败
        logger.warning("Telegram bot 初始化失败，桥已禁用：%s", exc)
        _bot = None
        _loop = None
        _publish_host_message = None
        return

    _bot.message_handler(content_types=["text"])(_handle_host_message)
    # daemon=True：即使 join 超时（轮询请求尚未返回），也不会拖住进程退出。
    _thread = threading.Thread(target=_run_polling, name="telegram-bridge", daemon=True)
    _thread.start()
    # 日志只记录"已启动"，绝不打印 token / chat id 之外的敏感信息。
    logger.info("Telegram 桥已启动")


def stop() -> None:
    """优雅停止轮询；幂等，可重复调用。"""
    global _thread, _bot, _loop, _publish_host_message

    bot = _bot
    thread = _thread
    _bot = None
    _loop = None
    _publish_host_message = None
    _thread = None

    if bot is not None:
        try:
            bot.stop_polling()
        except Exception as exc:  # noqa: BLE001
            logger.warning("停止 Telegram 轮询失败：%s", exc)
    if thread is not None:
        # 轮询线程最坏情况要等一次在飞的 getUpdates 返回；join 超时后靠 daemon
        # 线程在进程退出时被回收，不会卡住应用关闭。
        thread.join(timeout=10)


async def forward_message(message: dict) -> None:
    """把观众消息转发到主播的 Telegram（功能禁用时 no-op）。

    同步的 send_message 用 asyncio.to_thread 挪出事件循环；任何异常只记日志，
    绝不冒泡到聊天链路，也不阻塞广播。
    """
    bot = _bot
    if not _configured() or bot is None:
        return
    text = _format_audience_text(message)
    try:
        await asyncio.to_thread(bot.send_message, TELEGRAM_CHAT_ID, text)
    except Exception as exc:  # noqa: BLE001 - 转发失败不影响聊天
        # 只记异常类型与消息，不记录 token。
        logger.warning("转发观众消息到 Telegram 失败：%s", exc)

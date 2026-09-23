"""聊天消息 webhook 转发。

配置文件位于项目根目录的 webhook.json，顶层是一个数组，可同时配置多个目标。
每个目标支持以下字段：

    url      必填，目标地址；留空串会被跳过（方便先占位后启用）
    method   可选，默认 "POST"
    headers  可选，自定义请求头（对象形式）
    body     可选，模板内容；占位符在发送前替换
    timeout  可选，请求超时秒数，默认 5

body 支持两种写法：
  * 字符串（用于 text/plain 之类非 JSON 内容）
  * 对象 / 数组（用于 JSON 内容，可直接嵌套）

占位符（出现在 body 内的字符串里）：
  {{name}}  发言昵称
  {{text}}  消息正文
  {{time}}  消息时间（ISO 8601，本地时区）
  {{ip}}    发言 IP（未共享 IP 时为空串）

body 缺省时的默认模板为：
  {"name": "{{name}}", "text": "{{text}}", "time": "{{time}}", "ip": "{{ip}}"}
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

import httpx

from src.config import ROOT_DIR

logger = logging.getLogger(__name__)

WEBHOOK_PATH = ROOT_DIR / "webhook.json"

_PLACEHOLDERS = ("name", "text", "time", "ip")
_DEFAULT_BODY = {
    "name": "{{name}}",
    "text": "{{text}}",
    "time": "{{time}}",
    "ip": "{{ip}}",
}

# (文件 mtime, 规范化配置)。文件未改动时复用，避免每条消息都重新解析。
_cache: tuple[float, list[dict]] | None = None

# 同时在飞的 webhook 请求数上限。信号量必须等到事件循环启动后才真正有意义，
# 所以这里定义为模块级常量 + 模块级信号量，所有 notify 复用同一把锁；
# 若每次 notify 都新建信号量，限制就形同虚设。
# 取 8 的理由：单连接限频 5 条/秒，配合 N 个目标峰值约 5N 并发；8 能在高刷屏时
# 压住瞬时峰值、又不至于让正常消息排队过久。信号量只约束"同时在发"的请求数，
# 不约束连接池（见下方 _send 仍按需创建 AsyncClient 的取舍说明）。
_MAX_CONCURRENT = 8
_sem = asyncio.Semaphore(_MAX_CONCURRENT)

# 持有 fire-and-forget 任务的强引用。事件循环对 Task 仅持弱引用，
# 若不保留引用，Task 可能在执行完成前被 GC 回收，导致 webhook 请求被静默丢弃、
# 连日志都看不到。任务完成后会在回调里把自己移除，避免集合无限增长。
_pending: set[asyncio.Task] = set()


def _load_config() -> list[dict]:
    global _cache
    try:
        mtime = WEBHOOK_PATH.stat().st_mtime
    except FileNotFoundError:
        _cache = (0.0, [])
        return []
    if _cache is not None and _cache[0] == mtime:
        return _cache[1]

    try:
        raw = json.loads(WEBHOOK_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 配置损坏不应影响聊天主流程
        logger.warning("webhook.json 读取失败，本次不发送：%s", exc)
        _cache = (mtime, [])
        return []

    items = raw if isinstance(raw, list) else [raw]
    normalized: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:  # 留空即禁用，方便先写占位
            continue
        raw_headers = item.get("headers") or {}
        normalized.append(
            {
                "url": url,
                "method": str(item.get("method") or "POST").upper(),
                "headers": {str(k): str(v) for k, v in raw_headers.items()},
                "body": item.get("body"),
                "timeout": float(item.get("timeout") or 5),
            }
        )
    _cache = (mtime, normalized)
    return normalized


def format_time(ms: Any) -> str:
    """把毫秒时间戳格式化为本地时区 ISO 8601（精确到秒）。

    公开函数：供其他模块（如 Telegram 桥）复用，以保持时间格式一致。
    """
    try:
        return datetime.fromtimestamp(int(ms) / 1000).astimezone().isoformat(
            timespec="seconds"
        )
    except (TypeError, ValueError, OSError):
        return ""


def _build_values(message: dict) -> dict[str, str]:
    ip = message.get("ip")
    return {
        "name": str(message.get("name") or ""),
        "text": str(message.get("text") or ""),
        "time": format_time(message.get("time")),
        "ip": str(ip) if ip else "",
    }


def _json_escape(value: str) -> str:
    # json.dumps 的结果去掉首尾引号，得到可安全嵌入 JSON 字符串字面量的转义文本
    return json.dumps(value, ensure_ascii=False)[1:-1]


def _render_node(node: Any, values: dict[str, str], escape: bool) -> Any:
    if isinstance(node, str):
        rendered = node
        for key in _PLACEHOLDERS:
            token = "{{" + key + "}}"
            if token in rendered:
                value = values[key]
                rendered = rendered.replace(token, _json_escape(value) if escape else value)
        return rendered
    if isinstance(node, list):
        return [_render_node(item, values, escape) for item in node]
    if isinstance(node, dict):
        return {str(k): _render_node(v, values, escape) for k, v in node.items()}
    return node


def _is_json_content(headers: dict[str, str]) -> bool:
    for key, value in headers.items():
        if key.lower() == "content-type":
            return "json" in value.lower()
    return True  # 缺省按 JSON 补 Content-Type


async def _send_one(client: httpx.AsyncClient, cfg: dict, message: dict) -> None:
    # 信号量约束同时在飞的请求数，避免刷屏时瞬间接入所有目标而打爆出站连接；
    # 整个发送过程（含实际出站请求）都须在该上下文内，否则限制形同虚设。
    async with _sem:
        values = _build_values(message)
        headers = dict(cfg["headers"])
        json_mode = _is_json_content(headers)
        kwargs: dict[str, Any] = {"headers": headers}

        body = cfg["body"]
        if body is None:
            kwargs["json"] = _render_node(_DEFAULT_BODY, values, True)
        elif isinstance(body, (dict, list)):
            kwargs["json"] = _render_node(body, values, True)
        else:
            rendered = _render_node(str(body), values, json_mode)
            kwargs["content"] = rendered.encode("utf-8")

        response = await client.request(
            cfg["method"], cfg["url"], timeout=cfg["timeout"], **kwargs
        )
        if response.status_code >= 400:
            logger.warning(
                "webhook %s %s -> HTTP %s",
                cfg["method"],
                cfg["url"],
                response.status_code,
            )


async def _send(items: list[dict], message: dict) -> None:
    try:
        async with httpx.AsyncClient() as client:
            results = await asyncio.gather(
                *(_send_one(client, cfg, message) for cfg in items),
                return_exceptions=True,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("webhook 发送失败：%s", exc)
        return
    for result in results:
        if isinstance(result, Exception):
            logger.warning("webhook 目标发送异常：%s", result)


def notify(message: dict) -> None:
    """消息入库成功后调用，把消息异步推送给所有已配置的目标。"""
    items = _load_config()
    if not items:
        return
    try:
        task = asyncio.create_task(_send(items, dict(message)))
    except RuntimeError:
        logger.warning("没有运行中的事件循环，webhook 未发送")
        return
    # 持强引用，防止任务在跑完前被 GC 回收而丢失发送（事件循环只持弱引用）
    _pending.add(task)
    # 任务完成后自行从集合移除，避免 _pending 无限增长；
    # set.discard(task) 正好匹配回调的入参签名（回调会被传入 task 自身）。
    # 这里不额外调用 task.exception() 取异常记日志——_send 内部已通过
    # gather(return_exceptions=True) 取回每个目标的异常并记日志、外层 try/except
    # 也兜底了整体异常，异常已被"取回"，不会触发 "Task exception was never retrieved"
    # 警告；再取一次只会重复日志，反而干扰排查，故不做。
    task.add_done_callback(_pending.discard)

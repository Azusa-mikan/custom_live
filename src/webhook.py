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


def _format_time(ms: Any) -> str:
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
        "time": _format_time(message.get("time")),
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
            "webhook %s %s -> HTTP %s", cfg["method"], cfg["url"], response.status_code
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
        asyncio.create_task(_send(items, dict(message)))
    except RuntimeError:
        logger.warning("没有运行中的事件循环，webhook 未发送")

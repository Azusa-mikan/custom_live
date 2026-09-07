import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from src import sql, webhook
from src.config import LIVE_TITLE, MEDIAMTX_CDN_SECRET, MEDIAMTX_URL, STREAM_URLS


# 复用的媒体代理客户端，lifespan 中创建/关闭
_mtx_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _mtx_client
    await sql.init_db()
    _mtx_client = httpx.AsyncClient(base_url=MEDIAMTX_URL, timeout=None)
    try:
        yield
    finally:
        await _mtx_client.aclose()
        _mtx_client = None
        await sql.close_db()


app = FastAPI(lifespan=lifespan)


class _MtxAccessFilter(logging.Filter):
    """从 uvicorn.access 日志中过滤 /mtx 代理请求（分片/播放列表高频请求，避免刷屏）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access 行形如 ... - "GET /mtx/xxx HTTP/1.1" 200
        return " /mtx/" not in record.getMessage()


logging.getLogger("uvicorn.access").addFilter(_MtxAccessFilter())

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
templates = Jinja2Templates(directory=ASSETS_DIR)

connections: set[WebSocket] = set()


def _message_payload(row: dict) -> dict:
    return {"name": row["name"], "text": row["text"], "time": row["created_at"]}


_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def _frontend_stream_url(stream: str, cdn_key_set: bool) -> str:
    """把 .env 里的 STREAM_N 解析成播放器可用的线路 URL。

    - 带协议（http://、https://…）：视为外部完整地址，原样使用；
    - 否则视为本服务内建端点：固定拼成 /mtx/<流名>/index.m3u8，
      未配置 CDN key 时再拼 ?cookieCheck=1（跳过 mediaMTX 首次 302）。
    """
    if _SCHEME_RE.match(stream):
        return stream
    url = f"/mtx/{stream.strip('/')}/index.m3u8"
    if not cdn_key_set:
        url += "?cookieCheck=1"
    return url


@app.get("/")
async def index(request: Request):
    stream_urls = [
        _frontend_stream_url(stream, bool(MEDIAMTX_CDN_SECRET))
        for stream in STREAM_URLS
    ]
    return templates.TemplateResponse(
        request,
        "index.html",
        {"live_title": LIVE_TITLE, "stream_urls": stream_urls},
    )


@app.get("/assets/{file}")
async def get_assets(file: str):
    asset = (ASSETS_DIR / file).resolve()
    # 防止路径穿越，仅允许访问 assets 目录下的文件
    if ASSETS_DIR not in asset.parents or not asset.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(asset)


# 透传时剔除逐跳头，避免把 Host/分块信息错发给上游
_SKIP_HEADERS = {
    "host", "connection", "keep-alive", "transfer-encoding",
    "upgrade", "te", "proxy-authorization", "content-length",
}


@app.api_route("/mtx/{path:path}", methods=["GET"])
async def mtx_proxy(request: Request, path: str):
    """把 HLS 请求转发到本机 mediaMTX（目标由 MEDIAMTX_URL 配置）。

    配置了 MEDIAMTX_CDN_SECRET 时注入 Bearer，使 mediaMTX 走共享单会话；
    未配置则不带该头，作为普通透传（mediaMTX 默认 per-viewer 流程）仍可用。
    """
    if not MEDIAMTX_URL or _mtx_client is None:
        raise HTTPException(status_code=404, detail="MTX proxy disabled")

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _SKIP_HEADERS
    }
    if MEDIAMTX_CDN_SECRET:
        headers["authorization"] = f"Bearer {MEDIAMTX_CDN_SECRET}"

    query = request.url.query
    url = f"{path}?{query}" if query else path
    try:
        upstream = await _mtx_client.send(
            _mtx_client.build_request("GET", url, headers=headers),
            stream=True,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="MTX upstream unreachable") from exc

    async def _iter_body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in _SKIP_HEADERS
    }
    return StreamingResponse(
        _iter_body(),
        status_code=upstream.status_code,
        headers=response_headers,
    )


@app.get("/api/likes")
async def get_likes():
    return {"likes": await sql.get_likes()}


@app.post("/api/likes")
async def live_love():
    return {"likes": await sql.increment_likes()}


@app.websocket("/api/chat")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    # 先回放最近消息，再接收新消息
    history = await sql.list_messages(limit=50)
    await websocket.send_json(
        {"type": "history", "items": [_message_payload(row) for row in history]}
    )
    connections.add(websocket)
    client = websocket.client
    ip = client.host if client else None
    try:
        while True:
            data = await websocket.receive_json()
            text = str(data.get("text") or "").strip()
            if not text:
                continue
            name = str(data.get("name") or "我").strip()[:20] or "我"
            created_at = int(time.time() * 1000)
            await sql.insert_message(
                text,
                created_at,
                name=name,
                ip=ip if data.get("share_ip") else None,
            )
            webhook.notify(
                {
                    "name": name,
                    "text": text,
                    "time": created_at,
                    "ip": ip if data.get("share_ip") else None,
                }
            )
            payload = {
                "type": "message",
                "message": {"name": name, "text": text, "time": created_at},
            }
            for conn in list(connections):
                try:
                    await conn.send_json(payload)
                except Exception:
                    connections.discard(conn)
    except WebSocketDisconnect:
        pass
    finally:
        connections.discard(websocket)

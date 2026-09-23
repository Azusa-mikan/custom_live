import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote

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

# 并发广播时单条 send_json 的最长等待时间（秒）：观众网络慢或写缓冲满会让
# send_json 长时间挂起，超过该值即判定该连接失效并丢弃，避免拖垮全体观众。
_BROADCAST_SEND_TIMEOUT = 5

# 同时在线 WebSocket 连接数上限，防止恶意客户端耗尽资源。
MAX_CONNECTIONS = 200

# 单连接限频参数：滑动窗口 RATE_LIMIT_PERIOD 秒内最多允许 RATE_LIMIT_MAX 条消息。
# 阈值取偏保守的 5 条/秒，足以覆盖正常聊天节奏，又能挡住刷屏灌库/刷 webhook 的恶意行为。
RATE_LIMIT_PERIOD = 1.0
RATE_LIMIT_MAX = 5

# 心跳探查参数：超过 HEARTBEAT_TIMEOUT 秒没收到该连接的任何消息，就主动发一个
# JSON ping 帧探查对端是否存活；连续 HEARTBEAT_MAX_MISSED 次探查都无消息，才判定为
# 半开/死连接并关闭。receive_json 超时本身不等于死亡（正常观众可能只是没说话），
# 所以不能一超时就断，先用 ping 探测、累计多次无响应再断。
HEARTBEAT_TIMEOUT = 30
HEARTBEAT_MAX_MISSED = 3


async def _broadcast(payload: dict) -> None:
    """并发把消息发给所有连接，单条独立超时；任一连接异常都不冒泡到调用方。

    这样设计是因为 WebSocket 发送是网络 IO，某观众写缓冲满会让 send_json 挂起，
    串行发送会卡住所有人的聊天。改成并发后，单条用 wait_for 兜底超时，异常（含超时）
    的连接直接移除，保证一个连接的问题绝不影响消息入库和给其他连接的广播。
    """
    conns = list(connections)

    async def _send_one(conn: WebSocket) -> None:
        await asyncio.wait_for(conn.send_json(payload), timeout=_BROADCAST_SEND_TIMEOUT)

    # return_exceptions=True：让单个连接的异常留在结果里，而不是让 gather 整体抛错
    results = await asyncio.gather(
        *(_send_one(conn) for conn in conns),
        return_exceptions=True,
    )
    for conn, res in zip(conns, results):
        if isinstance(res, Exception):
            connections.discard(conn)


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
    # ASSETS_DIR 定义时只对 __file__ 做了 .resolve()，"assets" 那段并未解析；
    # 若 assets 目录本身是符号链接，必须用 .resolve() 把两边都解析成真实路径，
    # 否则 is_relative_to 会因真实父链里不含该链接而误判为越界。
    asset = (ASSETS_DIR / file).resolve()
    # is_relative_to 同时覆盖 "祖先目录" 与元素自身，比 parents 只含祖先更严谨；
    # 越界一律返回 404 而非 403：403 会泄露"路径存在但你不能访问"的信息。
    if not asset.is_relative_to(ASSETS_DIR.resolve()) or not asset.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(asset)


# 透传时剔除逐跳头，避免把 Host/分块信息错发给上游
_SKIP_HEADERS = {
    "host", "connection", "keep-alive", "transfer-encoding",
    "upgrade", "te", "proxy-authorization", "content-length",
}


def _repeated_unquote(value: str) -> str:
    """反复做 URL 解码直到结果稳定，专门对付 %252e%252e 这类多重编码绕过。

    只解码一次会被 `%25`(即 %) 再次封印，必须循环到不再变化才算真正解码干净。
    """
    prev = None
    cur = value
    while cur != prev:
        prev, cur = cur, unquote(cur)
    return cur


def _validate_mtx_path(path: str) -> None:
    """校验 HLS 代理要转发给上游的 path，非法直接抛 400（而非 404）。

    选 400 而非 404 的理由：这些是被客户端主动塞进来的畸形/越权输入（穿越、开放中继、
    header 注入载体），语义上是「请求本身非法」，用 400 Bad Request 更准确；404 会让人
    误以为只是「资源不存在」，反而可能泄露/弱化本端点的校验存在。404 只保留给
    “MEDIAMTX_URL 未配置、代理整体关闭”这种合法的配置态。
    返回即代表安全；所有判断都基于“彻底解码后”的值，避免编码绕过。
    """
    # 空 path 或纯空白拼到上游 URL 毫无意义，直接拒绝
    if not path or not path.strip():
        raise HTTPException(status_code=400, detail="Empty mtx path")
    decoded = _repeated_unquote(path)
    # 控制字符（含 NUL/CR/LF，码点 < 0x20 或 0x7f）是 HTTP 头注入与协议混淆的载体
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in decoded):
        raise HTTPException(status_code=400, detail="Control character in mtx path")
    # 反斜杠在 URL 路径中没有合法用途，且可能被某些上游当作分隔符
    if "\\" in decoded:
        raise HTTPException(status_code=400, detail="Backslash in mtx path")
    # 以 / 或 \ 开头会触发 scheme-relative（//evil.com）解析，替换 base_url 主机，
    # 把本端点变成任意站点的开放中继（SSRF / 带宽盗用），必须拒绝
    if decoded.startswith(("/", "\\")):
        raise HTTPException(status_code=400, detail="mtx path must not start with / or \\")
    # 逐段检查，出现 .. 段即目录穿越，拒绝；用段判断而非子串，避免误伤普通含点文件名
    if any(seg == ".." for seg in decoded.split("/")):
        raise HTTPException(status_code=400, detail="Path traversal in mtx path")


def _validate_mtx_query(query: str) -> None:
    """校验拼到上游 URL 的查询串，非法直接抛 400。

    查询串无法改变上游主机，所以只防控制字符/反斜杠这类头注入载体；
    = & ? 等都是合法字符，不能误伤。
    """
    if not query:
        return
    decoded = _repeated_unquote(query)
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in decoded):
        raise HTTPException(status_code=400, detail="Control character in mtx query")
    if "\\" in decoded:
        raise HTTPException(status_code=400, detail="Backslash in mtx query")


@app.api_route("/mtx/{path:path}", methods=["GET"])
async def mtx_proxy(request: Request, path: str):
    """把 HLS 请求转发到本机 mediaMTX（目标由 MEDIAMTX_URL 配置）。

    配置了 MEDIAMTX_CDN_SECRET 时注入 Bearer，使 mediaMTX 走共享单会话；
    未配置则不带该头，作为普通透传（mediaMTX 默认 per-viewer 流程）仍可用。
    """
    if not MEDIAMTX_URL or _mtx_client is None:
        raise HTTPException(status_code=404, detail="MTX proxy disabled")

    # 拼上游 URL 前先做强校验：基于“彻底解码后”的值校验，
    # 这样 %2e%2e%2f / %00 等编码绕过（含双重编码）都会被识别并挡在转发之前。
    _validate_mtx_path(path)
    _validate_mtx_query(request.url.query)
    # 转发时用解码后的规范化值：既和校验口径一致，也避免把编码后的穿越序列原样丢给
    # 上游、被上游再次解码而造成穿透。
    safe_path = _repeated_unquote(path)
    safe_query = _repeated_unquote(request.url.query)
    url = f"{safe_path}?{safe_query}" if safe_query else safe_path

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _SKIP_HEADERS
    }
    if MEDIAMTX_CDN_SECRET:
        headers["authorization"] = f"Bearer {MEDIAMTX_CDN_SECRET}"

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
    # 连接数上限保护：accept 后若已满，立刻关闭并提示稍后再试。
    # 1013 = try again later，表示服务暂时过载/不可用，客户端可稍后重连，
    # 比 1008（policy violation）更贴合“只是暂时满了”的语义。
    if len(connections) >= MAX_CONNECTIONS:
        await websocket.close(code=1013)
        return
    # 先回放最近消息，再接收新消息
    history = await sql.list_messages(limit=50)
    await websocket.send_json(
        {"type": "history", "items": [_message_payload(row) for row in history]}
    )
    connections.add(websocket)
    client = websocket.client
    ip = client.host if client else None
    # 单连接限频用的滑动窗口时间戳列表（每个连接独立，不共享）
    send_times: list[float] = []
    # 心跳：连续多少次探查都没收到客户端消息，就判定为死连接
    missed = 0
    try:
        while True:
            try:
                # 用 wait_for 给接收加超时，超时即进入心跳探查分支（不直接断连）
                data = await asyncio.wait_for(
                    websocket.receive_json(), timeout=HEARTBEAT_TIMEOUT
                )
            except asyncio.TimeoutError:
                # 超时只说明这段时间客户端没说话，正常观众也可能只是不发言，
                # 因此先发一个 JSON ping 帧探查对端是否还活着（前端会忽略未知 type）。
                missed += 1
                if missed >= HEARTBEAT_MAX_MISSED:
                    # 连续多次探查都收不到任何消息，判定为半开/死连接，主动关闭
                    break
                try:
                    await asyncio.wait_for(
                        websocket.send_json({"type": "ping"}),
                        timeout=_BROADCAST_SEND_TIMEOUT,
                    )
                except Exception:
                    # 连 ping 都发不出去，连接已失效，直接关闭
                    break
                continue
            except WebSocketDisconnect:
                break
            # 收到任意消息说明连接存活，重置心跳计数
            missed = 0
            text = str(data.get("text") or "").strip()
            if not text:
                continue
            # 服务端兜底长度上限，与前端 input maxlength="300" 保持一致；超长直接截断
            text = text[:300]
            if not text:
                continue
            # 单连接限频：只保留窗口内的时间戳，超出阈值静默丢弃（不写库/不广播/不断连）
            now = time.time()
            cutoff = now - RATE_LIMIT_PERIOD
            while send_times and send_times[0] < cutoff:
                send_times.pop(0)
            if len(send_times) >= RATE_LIMIT_MAX:
                continue
            send_times.append(now)
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
            await _broadcast(payload)
    except WebSocketDisconnect:
        pass
    finally:
        connections.discard(websocket)

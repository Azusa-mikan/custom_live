import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from src import sql, webhook
from src.config import LIVE_TITLE, STREAM_URLS


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await sql.init_db()
    yield
    await sql.close_db()


app = FastAPI(lifespan=lifespan)

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
templates = Jinja2Templates(directory=ASSETS_DIR)

connections: set[WebSocket] = set()


def _message_payload(row: dict) -> dict:
    return {"name": row["name"], "text": row["text"], "time": row["created_at"]}


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"live_title": LIVE_TITLE, "stream_urls": STREAM_URLS},
    )


@app.get("/assets/{file}")
async def get_assets(file: str):
    asset = (ASSETS_DIR / file).resolve()
    # 防止路径穿越，仅允许访问 assets 目录下的文件
    if ASSETS_DIR not in asset.parents or not asset.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(asset)


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

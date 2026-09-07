# CUSTOM\_LIVE

一个轻量的自建直播间页面：播放 HLS 直播流 + 实时聊天 + 点赞，前端为纯手写原生 HTML/CSS/JS（Material Design 3 视觉风格，跟随系统深浅色），后端用 FastAPI。

适合自己或小圈子直播观看：把直播间挂在本地/内网服务器上，前端直连播放，聊天消息持久化并可选转发到外部 webhook。

## 功能

- 多线路 HLS 直播播放（`.env` 里配 `STREAM_1`…`STREAM_N`，页面上可切换，选择记在浏览器 localStorage）
- 支持 Safari 原生 HLS 与其它浏览器的 hls.js
- 自绘播放器控制条：播放/暂停、静音、音量、全屏、LIVE 标识；全屏时鼠标闲置自动隐藏
- 自动播放被浏览器拦截时降级为静音播放，点击画面恢复声音
- 实时聊天：WebSocket 收发，历史消息（最近 50 条）持久化到 SQLite，刷新后回放
- 点赞计数（服务端持久化）
- 首次进入弹窗设置昵称、是否共享 IP（之后可点设置按钮再次修改）
- 聊天消息可选转发到外部 webhook（见下文）
- 右键菜单 + 视频信息面板：实时帧率（基于 `requestVideoFrameCallback` 实测）、码率/清晰度（随 `LEVEL_SWITCHED` 更新）、当前线路与地址

## 快速开始

需要 Python ≥ 3.13 与 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync          # 安装依赖（fastapi[standard]、aiosqlite）
# 编辑 .env 填入直播地址
uv run main.py   # 启动，默认监听 0.0.0.0:8000（局域网内即可访问）
```

> 注意：当前 `main.py` **不带自动重载**，改后端代码（`src/*.py`、`main.py`）后需手动重启；改 `webhook.json` 无需重启，发送时会自动重载。开发期想热重载可改用 `fastapi dev`（FastAPI CLI，随 `fastapi[standard]` 安装）。

## .env 配置

```env
LIVE_TITLE="my live"          # 页面标题 / <h1>
STREAM_1="http://127.0.0.1:8888/mystream/index.m3u8?cookieCheck=1"
STREAM_2="https://example.com/live/index.m3u8"   # 可继续加 STREAM_3 …
```

- 线路按 `STREAM_N` 从 1 递增读取，全部未配置时页面显示"尚未配置直播地址"。
- 播放源为 HTTP（非 HTTPS）直连 MediaMTX 时，需在地址后追加 `?cookieCheck=1`（MediaMTX 的 HLS 防深链校验），例如：
  `STREAM_1="http://192.168.1.190:8888/aaccgg/index.m3u8?cookieCheck=1"`

## Webhook（聊天消息转发）

每条聊天消息成功入库后会异步推送到 `webhook.json` 中配置的全部目标；推送失败只记日志，不影响聊天。文件在项目根目录，顶层为数组，可配多个目标：

```json
[
  {
    "url": "https://your-server/hook",
    "method": "POST",
    "headers": {
      "Content-Type": "application/json",
      "Authorization": "Bearer YOUR_TOKEN"
    },
    "body": {
      "name": "{{name}}",
      "text": "{{text}}",
      "time": "{{time}}",
      "ip": "{{ip}}"
    },
    "timeout": 5
  }
]
```

| 字段        | 说明                                           |
| --------- | -------------------------------------------- |
| `url`     | 必填；留空串则该目标跳过（便于先占位）                          |
| `method`  | 默认 `POST`                                    |
| `headers` | 自定义请求头，如鉴权信息                                 |
| `body`    | 模板；缺省为 `{"name":…,"text":…,"time":…,"ip":…}` |
| `timeout` | 请求超时秒数，默认 5                                  |

Body 模板可用的占位符：

- `{{name}}` 发言昵称
- `{{text}}` 消息正文
- `{{time}}` 消息时间（ISO 8601，服务器本地时区）
- `{{ip}}` 发言 IP（对方未勾选共享 IP 时为空串）

JSON 目标中占位符值会自动做 JSON 转义，消息里的引号/换行不会破坏 payload；`body` 也可写成字符串模板（用于 `text/plain` 等非 JSON 内容，此时不做转义）。

## 播放与隐私备注

- 直播间页面、聊天、点赞都在你自己的服务上；除你主动配置的 webhook 外不向任何第三方发数据。
- 聊天消息若对方勾选了"共享 IP"，其来源 IP 会存入 SQLite 并可在 webhook 中使用；未勾选则 IP 字段为 NULL。


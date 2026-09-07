from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT_DIR / ".env"


def _read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip("\"'")
    return env


_env = _read_env(ENV_PATH)


def _env_value(key: str, default: str = "") -> str:
    """读取配置值：键缺失或值为空串时统一视为"未配置"，返回 default。"""
    raw = _env.get(key)
    return default if raw is None or raw == "" else raw


LIVE_TITLE = _env_value("LIVE_TITLE", "custom live")

# MediaMTX CDN 模式密钥。为空/未配置时为 None，此时 api.py 中的 /mtx 代理不注入 Bearer。
MEDIAMTX_CDN_SECRET = _env_value("MEDIAMTX_CDN_SECRET") or None

# MediaMTX 上游地址（/mtx 代理的转发目标）。空值回退默认本机回环。
MEDIAMTX_URL = _env_value("MEDIAMTX_URL", "http://127.0.0.1:8888")


def _load_stream_urls(env: dict[str, str]) -> list[str]:
    urls: list[str] = []
    index = 1
    while f"STREAM_{index}" in env:
        # 空值视为"该线路禁用"：跳过但不影响后续序号继续收集
        if env[f"STREAM_{index}"]:
            urls.append(env[f"STREAM_{index}"])
        index += 1
    return urls or [""]


STREAM_URLS = _load_stream_urls(_env)

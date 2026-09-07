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

LIVE_TITLE = _env.get("LIVE_TITLE", "mikan's live")


def _load_stream_urls(env: dict[str, str]) -> list[str]:
    urls: list[str] = []
    index = 1
    while f"STREAM_{index}" in env:
        urls.append(env[f"STREAM_{index}"])
        index += 1
    return urls or [""]


STREAM_URLS = _load_stream_urls(_env)

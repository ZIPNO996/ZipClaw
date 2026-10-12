"""保存微信登录凭证和收消息进度，不与 CLI 的聊天记录混用。"""

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"


@contextmanager
def account_run_lock(path: Path):
    """同一个微信绑定只允许一个收消息进程，防止两个入口抢同一个通道。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as file:
        file.seek(0, 2)
        if file.tell() == 0:
            file.write(b"0")
            file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("已有微信入口进程正在运行，请先停止它再启动。") from None

        try:
            yield
        finally:
            file.seek(0)
            if os.name == "nt":
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def validate_base_url(value: str) -> str:
    """只接受微信的 HTTPS 地址，防止把登录凭证发到其它网站。"""

    parsed = urlsplit(value)
    hostname = parsed.hostname or ""

    if (
        parsed.scheme != "https"
        or not hostname.endswith(".weixin.qq.com")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("微信接口地址必须是 weixin.qq.com 子域名下的 HTTPS 地址。")

    return f"https://{hostname}"


def read_json(path: Path) -> dict:
    """文件不存在时返回空字典；损坏文件明确报错，不当成空文件覆盖。"""

    if not path.exists():
        return {}

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise ValueError("微信本地记录不是合法 JSON，请检查对应文件。") from None

    if not isinstance(value, dict):
        raise ValueError("微信本地记录必须是 JSON 对象。")

    return value


def write_json(path: Path, value: dict) -> None:
    """在同一目录写临时文件，关闭后再替换正式文件。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".zipclaw-wechat-",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            json.dump(value, file, ensure_ascii=False, indent=2)

        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@dataclass
class WechatCredentials:
    """token 不参与对象的打印；扫码用户是第一版唯一允许使用的人。"""

    token: str = field(repr=False)
    bot_id: str
    owner_id: str
    base_url: str

    @classmethod
    def from_dict(cls, value: dict) -> "WechatCredentials":
        for name in ["token", "bot_id", "owner_id", "base_url"]:
            item = value.get(name)
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"微信登录记录缺少有效的 {name}。请重新扫码登录。")

        return cls(
            token=value["token"].strip(),
            bot_id=value["bot_id"].strip(),
            owner_id=value["owner_id"].strip(),
            base_url=validate_base_url(value["base_url"]),
        )

    def save(self, path: Path) -> None:
        write_json(
            path,
            {
                "token": self.token,
                "bot_id": self.bot_id,
                "owner_id": self.owner_id,
                "base_url": self.base_url,
            },
        )

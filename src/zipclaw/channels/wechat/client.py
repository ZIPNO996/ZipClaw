"""微信 iLink HTTP 客户端：只负责收发消息，不直接调用模型或执行工具。

协议依据：https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol_zh_CN.md
"""

import base64
import hashlib
import secrets
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .storage import DEFAULT_BASE_URL, WechatCredentials, validate_base_url


class WechatAPIError(RuntimeError):
    """提供简短的中文错误，不把服务器响应中的凭证打印出来。"""

    def __init__(
        self,
        message: str,
        retryable: bool = False,
        session_expired: bool = False,
    ):
        super().__init__(message)
        self.retryable = retryable
        # 单独标记凭证失效，入口不靠匹配中文提示来决定是否重新扫码。
        self.session_expired = session_expired


def split_text(text: str, max_bytes: int = 3000) -> list[str]:
    """按 UTF-8 字节分段；中文通常占多个字节，不能直接按字符数切片。"""

    parts = []
    characters = []
    byte_count = 0

    for character in text:
        size = len(character.encode("utf-8"))
        if byte_count + size > max_bytes and characters:
            parts.append("".join(characters))
            characters = []
            byte_count = 0

        characters.append(character)
        byte_count += size

    if characters:
        parts.append("".join(characters))

    return parts


class WechatClient:
    def __init__(
        self,
        credentials: WechatCredentials | None = None,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.credentials = credentials
        self.base_url = (
            credentials.base_url if credentials is not None else DEFAULT_BASE_URL
        )
        # HTTPX 默认不自动重试。发送消息超时后，不能认定消息一定没送到。
        self.http = http_client or httpx.AsyncClient(timeout=15.0)

    async def close(self) -> None:
        await self.http.aclose()

    async def request(
        self,
        method: str,
        endpoint: str,
        body: dict | None = None,
        params: dict | None = None,
        authenticated: bool = False,
        timeout: float = 15.0,
    ) -> dict:
        # 第一版使用自身版本作为客户端标识，不伪装成 OpenClaw。
        headers = {
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": str(1 << 8),  # 0.1.0 的版本编码。
        }

        if method == "POST":
            number_text = str(secrets.randbits(32))
            headers.update(
                {
                    "Content-Type": "application/json",
                    "AuthorizationType": "ilink_bot_token",
                    "X-WECHAT-UIN": base64.b64encode(
                        number_text.encode("utf-8")
                    ).decode("ascii"),
                }
            )

        if authenticated:
            if self.credentials is None:
                raise WechatAPIError("微信尚未登录，请先执行登录命令。")
            headers["Authorization"] = f"Bearer {self.credentials.token}"

        try:
            response = await self.http.request(
                method,
                self.base_url + "/" + endpoint,
                headers=headers,
                json=body,
                params=params,
                timeout=timeout,
            )
            response.raise_for_status()
        except httpx.TimeoutException:
            raise WechatAPIError("微信请求超时。", retryable=True) from None
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise WechatAPIError(
                f"微信接口请求失败，HTTP 状态码：{status}。",
                retryable=status in {408, 429, 500, 502, 503, 504},
                session_expired=authenticated and status == 401,
            ) from None
        except httpx.RequestError:
            raise WechatAPIError("无法连接微信接口，请检查网络。", retryable=True) from None

        # 输入状态接口可能只返回成功 HTTP 状态，不一定有 JSON 正文。
        if endpoint == "ilink/bot/sendtyping" and not response.content:
            return {}

        try:
            value = response.json()
        except ValueError:
            raise WechatAPIError("微信接口没有返回合法 JSON。") from None

        if not isinstance(value, dict):
            raise WechatAPIError("微信接口返回格式不正确。")

        for field in ["ret", "errcode"]:
            code = value.get(field, 0)
            if code is None or code == 0:
                continue
            if isinstance(code, bool) or not isinstance(code, int):
                raise WechatAPIError("微信接口业务错误码的格式不正确。")
            if code == -14:
                raise WechatAPIError(
                    "微信登录会话已失效，请重新扫码登录。",
                    session_expired=True,
                )
            raise WechatAPIError(f"微信接口返回业务错误，错误码：{code}。")

        return value

    def base_info(self) -> dict:
        return {"channel_version": "0.1.0", "bot_agent": "ZipClaw/0.1.0"}

    async def get_qrcode(self, local_tokens: list[str]) -> dict:
        return await self.request(
            "POST",
            "ilink/bot/get_bot_qrcode",
            body={"local_token_list": local_tokens},
            params={"bot_type": "3"},
        )

    async def get_login_status(self, qrcode: str, verify_code: str = "") -> dict:
        params = {"qrcode": qrcode}
        if verify_code:
            params["verify_code"] = verify_code

        return await self.request(
            "GET",
            "ilink/bot/get_qrcode_status",
            params=params,
            timeout=35.0,
        )

    def redirect_login(self, hostname: str) -> None:
        self.base_url = validate_base_url("https://" + hostname)

    async def get_updates(self, cursor: str) -> dict:
        return await self.request(
            "POST",
            "ilink/bot/getupdates",
            body={"get_updates_buf": cursor, "base_info": self.base_info()},
            authenticated=True,
            timeout=40.0,
        )

    async def send_text(self, user_id: str, text: str, context_token: str) -> None:
        if not context_token:
            raise WechatAPIError("收到的消息缺少 context_token，无法关联回复。")

        for part in split_text(text):
            # 每一段使用独立 ID；发生超时不自动重新发送，避免重复消息。
            await self.send_item(
                user_id,
                {"type": 1, "text_item": {"text": part}},
                context_token,
            )

    async def send_item(self, user_id: str, item: dict, context_token: str) -> None:
        """文字和图片共用消息外壳，仅 item 的内容不同。"""

        if not context_token:
            raise WechatAPIError("收到的消息缺少 context_token，无法关联回复。")
        await self.request(
            "POST",
            "ilink/bot/sendmessage",
            body={
                "msg": {
                    "from_user_id": "",
                    "to_user_id": user_id,
                    "client_id": "zipclaw-" + uuid4().hex,
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": [item],
                },
                "base_info": self.base_info(),
            },
            authenticated=True,
        )

    async def get_typing_ticket(self, user_id: str, context_token: str) -> str:
        result = await self.request(
            "POST",
            "ilink/bot/getconfig",
            body={
                "ilink_user_id": user_id,
                "context_token": context_token,
                "base_info": self.base_info(),
            },
            authenticated=True,
            timeout=10.0,
        )
        ticket = result.get("typing_ticket", "")
        return ticket if isinstance(ticket, str) else ""

    async def send_typing(self, user_id: str, ticket: str, active: bool) -> None:
        await self.request(
            "POST",
            "ilink/bot/sendtyping",
            body={
                "ilink_user_id": user_id,
                "typing_ticket": ticket,
                "status": 1 if active else 2,
                "base_info": self.base_info(),
            },
            authenticated=True,
            timeout=10.0,
        )

    async def send_image(self, user_id: str, data: bytes, context_token: str) -> None:
        """上传加密图片，再发送原生图片消息；不是发送本机路径或 Markdown。"""

        if not context_token:
            raise WechatAPIError("缺少微信会话关联值，无法发送图片。")

        # 微信媒体协议要求 AES-128-ECB 和 PKCS#7 填充。
        # key 是随机的 16 字节；十六进制字符串长度是 32。
        key = secrets.token_bytes(16)
        filekey = uuid4().hex
        padder = padding.PKCS7(128).padder()
        padded_data = padder.update(data) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        ciphertext = encryptor.update(padded_data) + encryptor.finalize()

        result = await self.request(
            "POST",
            "ilink/bot/getuploadurl",
            body={
                "filekey": filekey,
                "media_type": 1,
                "to_user_id": user_id,
                "rawsize": len(data),
                # MD5 是微信协议要求的文件校验值，不用于保护密钥。
                "rawfilemd5": hashlib.md5(data).hexdigest(),
                "filesize": len(ciphertext),
                "no_need_thumb": True,
                "aeskey": key.hex(),
                "base_info": self.base_info(),
            },
            authenticated=True,
        )
        upload_url = result.get("upload_full_url")
        if not isinstance(upload_url, str) or not upload_url.strip():
            upload_param = result.get("upload_param")
            if not isinstance(upload_param, str) or not upload_param:
                raise WechatAPIError("微信没有返回图片上传地址。")
            query = urlencode(
                {"encrypted_query_param": upload_param, "filekey": filekey}
            )
            upload_url = "https://novac2c.cdn.weixin.qq.com/c2c/upload?" + query

        parsed = urlsplit(upload_url)
        if (
            parsed.scheme != "https"
            or not (parsed.hostname or "").endswith(".weixin.qq.com")
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise WechatAPIError("微信返回的图片上传地址不正确。")

        # CDN 只收到图片密文，不能带上机器人的 Authorization 请求头。
        try:
            response = await self.http.post(
                upload_url,
                content=ciphertext,
                headers={"Content-Type": "application/octet-stream"},
                timeout=60.0,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise WechatAPIError(
                f"微信图片上传失败，HTTP 状态码：{exc.response.status_code}。"
            ) from None
        except httpx.RequestError:
            raise WechatAPIError("微信图片上传连接失败或超时。") from None

        download_param = response.headers.get("x-encrypted-param")
        if response.status_code != 200 or not download_param:
            raise WechatAPIError("微信 CDN 未返回有效的图片下载参数。")

        await self.send_item(
            user_id,
            {
                "type": 2,
                "image_item": {
                    "media": {
                        "encrypt_query_param": download_param,
                        # 与腾讯图片发送器一致：对十六进制密钥字符串做 base64。
                        "aes_key": base64.b64encode(
                            key.hex().encode("ascii")
                        ).decode("ascii"),
                        "encrypt_type": 1,
                    },
                    "mid_size": len(ciphertext),
                },
            },
            context_token,
        )

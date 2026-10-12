"""扫码登录。只有登录阶段询问终端输入；运行阶段通过微信确认工具。"""

import asyncio
import time
import webbrowser
from pathlib import Path

import qrcode
from qrcode.image.svg import SvgPathImage

from .client import WechatAPIError, WechatClient
from .storage import DEFAULT_BASE_URL, WechatCredentials, read_json


async def login(
    credentials_path: Path,
    *,
    show_qrcode: bool = False,
    force_refresh: bool = False,
) -> None:
    old_record = read_json(credentials_path)
    old_token = old_record.get("token")
    local_tokens = [old_token] if isinstance(old_token, str) and old_token else []
    if force_refresh:
        # 凭证失效时不向服务器声明“已有可用登录”，要求重新签发凭证。
        # 保留旧文件，只有新的扫码确认成功后才替换它。
        local_tokens = []
    client = WechatClient()
    deadline = time.monotonic() + 300
    verify_code = ""
    scanned_printed = False

    try:
        result = await client.get_qrcode(local_tokens)
        qrcode_value = result.get("qrcode")
        qrcode_content = result.get("qrcode_img_content")
        if not isinstance(qrcode_value, str) or not qrcode_value:
            raise WechatAPIError("微信没有返回有效的二维码标识。")
        if not isinstance(qrcode_content, str) or not qrcode_content:
            raise WechatAPIError("微信没有返回可以展示的二维码内容。")

        # 只使用本地二维码库，不把登录链接发给第三方二维码网站。
        code = qrcode.QRCode(border=2)
        code.add_data(qrcode_content)
        code.make(fit=True)
        code.print_ascii(invert=True)
        image_path = credentials_path.parent / "login.svg"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        code.make_image(image_factory=SvgPathImage).save(str(image_path))
        print(f"请用手机微信扫码并确认。二维码文件：{image_path}")

        if show_qrcode:
            # IDE 运行时在默认浏览器打开 PNG，避免控制台二维码被字体挤变形。
            # 不通过第三方二维码网站生成，也不把登录链接发送给其它服务。
            popup_path = credentials_path.parent / "login.png"
            code.make_image().save(str(popup_path))
            try:
                opened = webbrowser.open(popup_path.resolve().as_uri())
            except (webbrowser.Error, OSError):
                opened = False
            if not opened:
                print(f"二维码窗口未能自动打开，请手动打开：{popup_path}")

        while time.monotonic() < deadline:
            try:
                result = await client.get_login_status(qrcode_value, verify_code)
            except WechatAPIError as exc:
                if not exc.retryable:
                    raise
                print(f"{exc} 正在继续等待扫码。")
                await asyncio.sleep(2)
                continue

            status = result.get("status")
            if status == "confirmed":
                credentials = WechatCredentials.from_dict(
                    {
                        "token": result.get("bot_token"),
                        "bot_id": result.get("ilink_bot_id"),
                        "owner_id": result.get("ilink_user_id"),
                        "base_url": result.get("baseurl") or DEFAULT_BASE_URL,
                    }
                )
                credentials.save(credentials_path)
                print("微信登录成功，凭证已保存。")
                print("第一版只接受本次扫码用户的私聊消息。")
                return

            if status == "binded_redirect":
                if force_refresh:
                    raise WechatAPIError(
                        "微信没有签发新的登录凭证，请在手机上完成重新授权后再试。"
                    )
                # 服务端没有发新凭证时，只有本地凭证完整才算登录完成。
                WechatCredentials.from_dict(old_record)
                print("此微信已绑定，本地登录记录可以继续使用。")
                return
            if status == "scaned_but_redirect":
                host = result.get("redirect_host")
                if not isinstance(host, str) or not host:
                    raise WechatAPIError("扫码重定向缺少有效的微信地址。")
                client.redirect_login(host)
            elif status == "need_verifycode":
                verify_code = input("请输入手机微信上显示的验证码：").strip()
                if not verify_code.isdigit():
                    raise WechatAPIError("验证码必须是数字，请重新运行登录命令。")
            elif status == "scaned":
                if not scanned_printed:
                    print("已扫码，请在手机微信上确认。")
                    scanned_printed = True
            elif status in {"expired", "verify_code_blocked"}:
                raise WechatAPIError("二维码已过期或验证受限，请重新运行登录命令。")
            elif status != "wait":
                raise WechatAPIError("微信返回了当前未支持的扫码状态。")

            await asyncio.sleep(1)

        raise WechatAPIError("等待扫码超过五分钟，请重新运行登录命令。")
    finally:
        await client.close()

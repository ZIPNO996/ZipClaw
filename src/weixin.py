"""在 PyCharm 中右键运行本文件，自动登录并启动微信 Agent。"""

import asyncio
from pathlib import Path

from zipclaw.channels.wechat.client import WechatAPIError
from zipclaw.channels.wechat.main import main as wechat_main


# 默认操作当前用户的桌面，而不是 IDE 可能选择的 src 目录。
# 想操作其它项目时，可以把这里改成 Path(r"C:\你的项目目录")。
WORKSPACE = Path.home() / "Desktop"


async def main() -> None:
    # auto：没有记录时先扫码；已有记录则尝试运行，失效时自动重新扫码。
    # 扫码成功后继续启动，不需要手动删除旧登录记录。
    # 显式传参数，右键运行时就不需要在 PyCharm 中填写 login / run。
    await wechat_main(
        ["auto", "--workspace", str(WORKSPACE)],
        show_qrcode=True,
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n微信服务已停止。")
    except (WechatAPIError, ValueError, OSError) as exc:
        print(f"[微信入口错误] {exc}")
        raise SystemExit(1)

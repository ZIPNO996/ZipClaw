"""独立微信入口：扫码登录或在指定工作区运行 Agent。"""

import argparse
import asyncio
import hashlib
import os
from pathlib import Path

from dotenv import load_dotenv

from ...config import load_config
from ...core.loop import AgentLoop
from ...core.session import SessionStore
from ...llm.openai_llm import OpenAILLM
from ...tools.filesystem.edit_file import EditFileTool
from ...tools.filesystem.grep import GrepTool
from ...tools.filesystem.list_dir import ListDirTool
from ...tools.filesystem.read_file import ReadFileTool
from ...tools.filesystem.write_file import WriteFileTool
from ...tools.registry import ToolRegistry
from ...tools.shell.run_command import RunCommandTool
from ...tools.web.web_search import WebSearchTool
from .client import WechatAPIError, WechatClient
from .login import login
from .service import WechatService
from .send_image import SendImageTool
from .storage import WechatCredentials, account_run_lock, read_json


async def main(
    argv: list[str] | None = None,
    *,
    show_qrcode: bool = False,
) -> None:
    parser = argparse.ArgumentParser(description="将 ZipClaw 接入个人微信 ClawBot。")
    parser.add_argument(
        "action",
        nargs="?",
        default="auto",
        choices=["auto", "login", "run"],
        help="auto 自动登录并运行（默认）；login 只登录；run 只运行",
    )
    # 微信入口默认以桌面为工作区，不随终端或 IDE 的启动目录改变。
    # 显式传入 --workspace 时，仍然使用用户指定的目录。
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.home() / "Desktop",
        help="Agent 操作的目录，默认使用当前用户的桌面",
    )
    # 命令行入口不传 argv 时读取 sys.argv；IDE 入口可以传入明确的参数列表。
    args = parser.parse_args(argv)

    # 登录不需要模型密钥；只有运行 Agent 时才读取模型配置。
    channel_directory = Path.home() / ".zipclaw" / "wechat"
    credentials_path = channel_directory / "credentials.json"
    needs_login = (
        args.action == "login"
        or (args.action == "auto" and not credentials_path.exists())
    )
    if needs_login:
        with account_run_lock(channel_directory / "run.lock"):
            await login(
                credentials_path,
                show_qrcode=show_qrcode,
                force_refresh=args.action == "login",
            )
        # login 命令仍只负责登录；auto 则接着执行下面的启动代码。
        if args.action == "login":
            return

    # auto 模式最多重新扫码一次，避免服务端仍拒绝登录时不断弹出二维码。
    for attempt in range(2):
        try:
            await run_agent(args.workspace, credentials_path)
            return
        except WechatAPIError as exc:
            if (
                args.action != "auto"
                or not exc.session_expired
                or attempt == 1
            ):
                raise

            # run_agent 已退出并关闭旧连接、释放运行锁，再开始重新登录。
            print("保存的微信登录已失效，正在重新获取二维码。扫码后将自动继续启动。")
            with account_run_lock(channel_directory / "run.lock"):
                await login(
                    credentials_path,
                    show_qrcode=show_qrcode,
                    force_refresh=True,
                )


async def run_agent(workspace: Path, credentials_path: Path) -> None:
    """使用当前登录记录启动 Agent；退出或出错时关闭网络连接。"""

    channel_directory = credentials_path.parent
    load_dotenv()
    config = load_config()
    credentials = WechatCredentials.from_dict(read_json(credentials_path))
    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise ValueError("指定的工作区不存在，或不是目录。")

    # 微信会话与终端会话隔离；换微信账号或换工作区都会使用另一份记录。
    identity = "\n".join(
        [
            credentials.bot_id,
            credentials.owner_id,
            os.path.normcase(str(workspace)),
        ]
    )
    session_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    directory = channel_directory / "sessions" / session_id
    session_store = SessionStore(directory / "conversation.json")

    registry = ToolRegistry()
    registry.register(ListDirTool(workspace, full_access=config.wechat_full_access))
    registry.register(ReadFileTool(workspace, full_access=config.wechat_full_access))
    registry.register(GrepTool(workspace, full_access=config.wechat_full_access))
    registry.register(WriteFileTool(workspace, full_access=config.wechat_full_access))
    registry.register(EditFileTool(workspace, full_access=config.wechat_full_access))
    registry.register(RunCommandTool(workspace))

    # 与终端入口共用搜索工具；微信发送图片等通道功能仍然独立。
    if config.tavily_api_key is not None:
        registry.register(WebSearchTool(api_key=config.tavily_api_key))

    llm = OpenAILLM(
        api_key=config.api_key,
        model=config.model,
        base_url=config.base_url,
        thinking_mode=config.thinking_mode,
    )
    client = WechatClient(credentials)
    try:
        agent = AgentLoop(
            llm=llm,
            tools=registry,
            max_steps=config.max_steps,
            show_tool_logs=config.show_tool_logs,
            show_reasoning=False,
            context_char_budget=config.context_char_budget,
            session_store=session_store,
            keep_recent_turns=config.keep_recent_turns,
        )
        history = session_store.load()
        if history:
            if history[0].get("role") != "system":
                raise ValueError("微信会话第一条记录必须是系统消息。")
            agent.messages.extend(history[1:])

        agent.messages[0]["content"] += (
            "\n当前通过微信与用户交流。只处理当前用户提出的任务。\n"
            "你正在通过微信(WeChat)与用户聊天。回复会发送到微信，"
            "请使用适合微信阅读的格式（纯文本、适度使用 emoji、避免过长的表格）。\n"
            "用户可以通过 /cancel 停止任务，通过 /clear 清空会话。\n"
            "需要把图片发到手机时，调用 send_image 发送已有的本机 PNG 或 JPEG；"
            "仅返回本机路径或 Markdown 图片语法不会发送图片。\n"
        )
        # 只约束图片任务，不影响用户明确要求编写代码的任务。
        agent.messages[0]["content"] += (
            "\n当用户只要求生成、修改或发送图片时，只交付图片。\n"
            "除非用户明确要求源码，否则不要额外创建绘图脚本、"
            "README、依赖配置等文件。\n"
            "简单辅助代码优先通过 run_command 使用 Python 的 -c 参数直接执行，"
            "不要保存为 .py 文件。\n"
            "确实需要辅助脚本时，放在本任务专用临时目录；"
            "文件工具受工作区限制时，临时目录也应位于工作区内。\n"
            "最终图片保存在用户指定的位置，未指定时保存在工作区。\n"
            "任务结束时，仅清理本任务创建的临时辅助文件，"
            "不得删除最终图片或用户原有文件。\n"
            "生成成功后，调用 send_image 将图片发送给用户。\n"
        )
        if config.wechat_full_access:
            agent.messages[0]["content"] = agent.messages[0]["content"].replace(
                "文件工具使用工作区相对路径；",
                "文件工具允许绝对路径或工作区相对路径；",
            )
            agent.messages[0]["content"] += (
                "当前微信入口已开启完全访问，工具免确认执行。"
                "文件工具可使用绝对路径访问工作区之外；相对路径仍以工作区为基准。\n"
            )
        else:
            agent.messages[0]["content"] += (
                "文件修改和命令执行由程序请求用户确认，不要假装已经获准。\n"
            )
        service = WechatService(
            client,
            credentials,
            agent,
            session_store,
            directory / "receive-state.json",
            full_access=config.wechat_full_access,
        )
        registry.register(
            SendImageTool(
                workspace,
                sender=service.send_image,
                full_access=config.wechat_full_access,
            )
        )
        print(f"微信 Agent 工作区：{workspace}")
        print("微信访问模式：" + ("完全访问" if config.wechat_full_access else "需要操作确认"))
        print(f"微信会话文件：{session_store.file_path}")
        if config.tavily_api_key is not None:
            print("网页搜索：已启用（Tavily）。")
        else:
            print("网页搜索：未启用（未配置 TAVILY_API_KEY）。")
        # 锁按微信绑定共用，而不是按工作区分开。进程退出时操作系统也会释放锁。
        with account_run_lock(channel_directory / "run.lock"):
            await service.run()
    finally:
        await client.close()
        await llm.client.close()


def entrypoint() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n微信服务已停止。")
    except (WechatAPIError, ValueError, OSError) as exc:
        print(f"[微信入口错误] {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    entrypoint()

# src/zipclaw/cli.py

import asyncio
import os
import hashlib

from pathlib import Path
from dotenv import load_dotenv

from .core.loop import AgentLoop
from .llm.openai_llm import OpenAILLM
from .tools.filesystem.list_dir import ListDirTool
from .tools.filesystem.read_file import ReadFileTool
from .tools.filesystem.write_file import WriteFileTool
from .tools.filesystem.edit_file import EditFileTool
from .tools.filesystem.grep import GrepTool
from .tools.shell.run_command import RunCommandTool
from .tools.registry import ToolRegistry
from .error.custom_errors import AgentStepLimitError
from .core.session import SessionStore


async def main():
    # 从 .env 加载环境变量。
    load_dotenv()

    # 当前终端所在目录，
    # 就作为 智能体 工作区。
    #
    # 以后可以允许用户：
    #
    # codeclaw ./my-project
    workspace = Path.cwd().resolve()
    print(f"工作区:{workspace}")

    # 将工作区路径转换成稳定的标识。
    # 不同项目即使文件夹名称相同，只要完整路径不同，也会分开保存。
    # 对字节串做 SHA-256 哈希，返回一个 64 位十六进制字符串,同一个路径，每次运行都得到同样的 ID。
    # 路径不同，ID 几乎不可能相同（SHA-256 碰撞概率极低）
    workspace_text = os.path.normcase(str(workspace))
    workspace_bytes = workspace_text.encode("utf-8")
    workspace_id = hashlib.sha256(workspace_bytes).hexdigest()

    # 保存到用户目录，不往被操作的项目里放聊天记录。
    #保存位置类似C:\Users\28657\.zipclaw\sessions\一串标识.json
    session_directory = Path.home() / ".zipclaw" / "sessions"
    session_file = session_directory / f"{workspace_id}.json"

    session_store = SessionStore(session_file)

    print(f"会话文件:{session_file}")

    # 创建 模型。
    llm = OpenAILLM(
        api_key=os.environ["OPENAI_API_KEY"],
        model=os.environ["OPENAI_MODEL"],
        base_url=os.getenv("OPENAI_BASE_URL"),
    )

    # 创建 智能体 工具箱。
    registry = ToolRegistry()

    # 先用 list_dir 发现文件路径，再用 read_file 读取内容。
    # 注册后 registry.schemas() 会自动把所有工具的参数格式提供给模型。
    registry.register(
        ListDirTool(
            workspace=workspace
        )
    )
    registry.register(
        ReadFileTool(
            workspace=workspace
        )
    )
    registry.register(
        WriteFileTool(
            workspace=workspace
        )
    )
    registry.register(
        EditFileTool(
            workspace=workspace
        )
    )
    registry.register(
        GrepTool(
            workspace=workspace
        )
    )
    registry.register(
        RunCommandTool(
            workspace=workspace
        )
    )

    # 创建 智能体。
    agent = AgentLoop(
        llm=llm,
        tools=registry,
    )

    try:
        history = session_store.load()
    except (OSError, ValueError) as exc:
        print(f"会话恢复失败：{exc}")
        print("程序已停止启动，原会话文件不会被覆盖。")
        return

    if history:
        # 当前保存格式约定：第一条消息必须是系统提示词。
        # 不符合约定时不要猜测，更不要继续覆盖原文件。
        if history[0]["role"] != "system":
            print("会话格式不正确：第一条消息不是系统消息。")
            return

        # 保留新创建 Agent 中的最新系统提示词。
        # 这样以后修改提示词，不会被历史中的旧版本覆盖。
        previous_messages = history[1:]
        agent.messages.extend(previous_messages)

        message_count = len(previous_messages)
        print(f"已恢复 {message_count} 条历史消息。")

    print("输入 exit 或 quit 退出，输入 /clear 清空对话。")

    # agent 必须在循环外创建，这样才能一直保留同一个实例的历史。
    while True:
        try:
            task = input("\n > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            break

        # 空输入不调用模型。
        if not task:
            continue

        if task.lower() in {"exit", "quit"}:
            print("已退出。")
            break

        if task == "/clear":
            # 新列表中只保留当前的系统提示词。
            cleared_messages = [agent.messages[0]]

            # 先保存成功，再清空内存。
            # 如果磁盘保存失败，内存里的旧历史也仍然保留。
            try:
                session_store.save(cleared_messages)
            except (OSError, TypeError, ValueError) as exc:
                print(f"清空失败：{exc}")
                print("内存中的对话历史仍然保留。")
                continue

            agent.messages = cleared_messages

            print("对话已清空，已保存的历史也已同步清空。")
            continue

        try:
            result = await agent.run(task)

        except AgentStepLimitError as exc:
            print(f"\n[任务停止] {exc}")
            print("你可以继续补充要求，或输入 /clear 开始新对话。")

            # 这里不写 continue。
            # 达到上限后，也需要继续执行下面的保存操作。

        else:
            # try 没有抛出异常时，才进入 else。
            # 避免达到上限后，使用一个不存在的 result。
            print(f"\nZipClaw > {result}")

        # 正常完成、达到步数上限，两种情况都保存历史。
        try:
            session_store.save(agent.messages)
        except (OSError, TypeError, ValueError) as exc:
            print(f"\n[保存失败] {exc}")
            print("当前历史仍保留在内存中，但尚未成功保存到磁盘。")


def entrypoint():
    """命令行入口：启动异步主程序。"""
    asyncio.run(main())


if __name__ == "__main__":
    entrypoint()

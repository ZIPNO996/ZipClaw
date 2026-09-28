# src/codeclaw/cli.py

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from .core.loop import AgentLoop
from .llm.openai_llm import OpenAILLM
from .tools.filesystem.list_dir import ListDirTool
from .tools.filesystem.read_file import ReadFileTool
from .tools.filesystem.write_file import WriteFileTool
from .tools.filesystem.edit_file import EditFileTool
from .tools.registry import ToolRegistry


async def main():
    # 从 .env 加载环境变量。
    load_dotenv()

    # 当前终端所在目录，
    # 就作为 Agent Workspace。
    #
    # 以后可以允许用户：
    #
    # codeclaw ./my-project
    workspace = Path.cwd()
    print(f"工作区:{workspace}")

    # 创建 LLM。
    llm = OpenAILLM(
        api_key=os.environ["OPENAI_API_KEY"],
        model=os.environ["OPENAI_MODEL"],
        base_url=os.getenv("OPENAI_BASE_URL"),
    )

    # 创建 Agent 工具箱。
    registry = ToolRegistry()

    # 先用 list_dir 发现文件路径，再用 read_file 读取内容。
    # 注册后 registry.schemas() 会自动把两个工具的参数格式提供给模型。
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

    # 创建 Agent。
    agent = AgentLoop(
        llm=llm,
        tools=registry,
    )


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
            # 保留第一条 system 消息，删除后面的对话记录。
            del agent.messages[1:]
            print("对话已清空。")
            continue

        result = await agent.run(task)
        print(f"\nZipClaw > {result}")


def entrypoint():
    """命令行入口：启动异步主程序。"""
    asyncio.run(main())


if __name__ == "__main__":
    entrypoint()

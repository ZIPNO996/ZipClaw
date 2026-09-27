# src/codeclaw/cli.py

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from .core.loop import AgentLoop
from .llm.openai_llm import OpenAILLM
from .tools.filesystem.read_file import ReadFileTool
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

    # 创建 LLM。
    llm = OpenAILLM(
        api_key=os.environ["OPENAI_API_KEY"],
        model=os.environ["OPENAI_MODEL"],
        base_url=os.getenv("OPENAI_BASE_URL"),
    )

    # 创建 Agent 工具箱。
    registry = ToolRegistry()

    # 第一版只注册一个工具：
    # read_file
    registry.register(
        ReadFileTool(
            workspace=workspace
        )
    )

    # 创建 Agent。
    agent = AgentLoop(
        llm=llm,
        tools=registry,
    )

    # 接收用户输入。
    task = input("> ")

    # 执行任务。
    result = await agent.run(task)

    # 输出最终答案。
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
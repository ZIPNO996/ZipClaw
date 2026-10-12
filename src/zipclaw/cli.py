# src/zipclaw/cli.py

import asyncio
import os
import hashlib
import signal
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
from .error.custom_errors import AgentStepLimitError,ModelRequestError,ModelResponseError
from .core.session import SessionStore


async def main():
    # 从 .env 加载环境变量。
    load_dotenv()

    # 当前终端所在目录，
    # 就作为 智能体 工作区。
    #
    # 以后可以允许用户：
    #
    # zipclaw ./my-project（目前还没有实现这个参数入口）
    workspace = Path.cwd().resolve()
    print(f"工作区:{workspace}")

    # 将工作区路径转换成稳定的标识。
    # 不同项目即使文件夹名称相同，只要完整路径不同，也会分开保存。
    # 对规范化路径的字节串做 SHA-256 哈希，返回长度为 64 的十六进制字符串。
    # 同一个规范化路径，每次运行都得到同样的 ID。
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

    # 创建模型适配器；当前变量名是 USTC_*，实际服务由配置值决定。
    # 普通摘要请求使用 chat()，用户任务通过 stream() 接收流式响应。
    llm = OpenAILLM(
        api_key=os.environ["USTC_API_KEY"],
        model=os.environ["USTC_MODEL"],
        base_url=os.getenv("USTC_BASE_URL"),
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
        session_store=session_store,
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

    # 当前还没有正在执行的用户任务。
    current_task: asyncio.Task | None = None

    # 获取当前 main() 所在的事件循环。
    # 后面通过它安排“取消任务”这个操作。
    event_loop = asyncio.get_running_loop()

    def handle_interrupt(signum, frame):
        """
        收到 Ctrl+C 时执行这个函数。

        signum 是信号编号。
        frame 是信号发生时的 Python 执行位置。
        这是信号处理函数要求接收的参数，目前不需要使用它们。
        """

        # 没有正在执行的任务时，使用普通的 Ctrl+C 行为。
        # 例如正在等待用户输入，此时会抛出 KeyboardInterrupt，
        # 由你现有的输入异常处理代码退出程序。
        if current_task is None or current_task.done():
            signal.default_int_handler(signum, frame)
            return

        # 安排事件循环取消当前任务，同时唤醒可能正在等待的事件循环。
        # 这里只传入 cancel 方法，没有写括号立即执行它。
        event_loop.call_soon_threadsafe(current_task.cancel)


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

        # try:
        #     # 可能出错的代码
        #     ...
        # except 某种异常:
        #     # 出错时执行
        #     ...
        # else:
        #     # 没出错时才执行
        #     ...
        # 保存原来的 Ctrl+C 处理方式。
        # 本次任务结束后，需要恢复它。
        previous_handler = signal.getsignal(signal.SIGINT)

        try:
            # result = await agent.run(task)
            # 流式输出已打印
            # 回答已经由核心循环逐段显示。

            # 为本次用户请求创建独立任务。
            current_task = asyncio.create_task(
                agent.run(task)
            )

            # SIGINT 是 Ctrl+C 对应的信号。
            # 本次任务执行期间，收到它就调用 handle_interrupt。
            signal.signal(
                signal.SIGINT,
                handle_interrupt,
            )

            # 等待任务完成。
            # 如果任务被取消，这里会收到 CancelledError。
            await current_task

        except asyncio.CancelledError:
            main_task = asyncio.current_task()

            if main_task is not None and main_task.cancelling():
                raise

            # 先补充历史，让下次请求知道任务已经被用户中断。
            agent.record_interruption()

            print("\n[任务已停止] 已取消当前任务。")
            print("你可以继续输入新的要求，或输入 /clear 清空对话。")

            # 后面原有的保存代码会把中断记录一起保存。

        except AgentStepLimitError as exc:
            print(f"\n[任务停止] {exc}")
            print("你可以继续补充要求，或输入 /clear 开始新对话。")

        except ModelRequestError as exc:
            # exc 中保存着适配器传出来的中文说明。
            print(f"\n[模型请求失败] {exc}")

            # CLI 不再重发请求；如果错误来自摘要请求，有限重试已经在 context.py 完成。
            # 普通任务的流式请求不会因为 retryable=True 就自动重试。
            print("本次任务已停止，程序没有退出。")
            print("你可以继续输入，或输入 /clear 清空对话。")

        except ModelResponseError as exc:
            print(f"\n[模型响应无效] {exc}")
            print("本次任务已停止，程序没有退出。")
            print("你可以继续输入，或输入 /clear 清空对话。")


            # 不清空历史，也不伪造一条模型回答。
            # 此前已经执行的工具和对应结果仍然保留。
            # 这里不写 continue，让下面的保存操作继续执行。

        finally:
            # 无论正常完成、发生错误，还是被取消，
            # 都恢复本次任务开始前的 Ctrl+C 处理方式。
            signal.signal(
                signal.SIGINT,
                previous_handler,
            )

            # 本次任务已经结束，不再保留“正在执行”的标记。
            current_task = None

        # 流式输出，不再需要else
        # else:
        #     # try 没有抛出异常时，才进入 else。
        #     # 避免达到上限后，使用一个不存在的 result。
        #     print(f"\nZipClaw > {result}")

        # 正常完成，或遇到上面已处理的三类任务异常后，都尝试保存当前历史。
        # 历史可能含有旧对话摘要；压缩前的记录另外保存在归档文件中。
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

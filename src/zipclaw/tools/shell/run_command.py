from pydantic import BaseModel, Field
from ..base import BaseTool
from pathlib import Path
import subprocess
import asyncio
import os

class RunCommandArgs(BaseModel):
    """
    命令执行工具的参数。
    """
    command: list[str] = Field(
        min_length=1,
        description=(
            "要执行的程序及参数。"
            "例如 ['python', 'calculator.py']，"
            "第一项是程序，后面的项是参数。"
        ),
    )

    #ge:Greater than or Equal to    le:Less than or Equal to
    timeout: int = Field(
        default=30,
        ge=1,
        le=120,
        description="最多等待多少秒，默认 30 秒，最大 120 秒。",
    )

    max_output_chars: int = Field(
        default=8000,
        ge=1,
        le=20000,
        description=(
            "正常输出和错误输出各自最多显示的字符数。"
            "默认 8000，范围为 1 到 20000。"
        ),
    )


class RunCommandTool(BaseTool):
    """
    在工作区执行命令，返回输出和退出码。
    """
    name = "run_command"

    description = (
        "在当前工作区目录中执行程序，"
        "返回退出码、正常输出和错误输出。"
        "可以用于运行脚本、执行测试或查看 Git 差异。"
        "请将程序和参数分别放进列表，不要传入整条命令字符串。"
        "输出过长时会截断：正常输出保留开头，错误输出保留末尾。"
    )

    args_model = RunCommandArgs

    def __init__(self, workspace: Path):
        # 保存命令的工作目录。
        self.workspace = workspace.resolve()

    async def stop_process(
        self,
        process: asyncio.subprocess.Process,
        output_task: asyncio.Task,
    ) -> None:
        """终止直接启动的程序，并等待输出收集完成。"""

        # returncode 为 None，表示尚未取得进程退出码。
        if process.returncode is None:
            try:
                # 请求操作系统立即终止这个进程。
                process.kill()

            except ProcessLookupError:
                # 检查时程序还在运行，
                # 但调用 kill() 前它可能刚好自己退出了。
                pass

        # output_task 内部执行 communicate()：
        # 它会收集输出，并等待程序退出。
        await output_task

    async def execute(self, args: RunCommandArgs) -> str:
        # 列表不能为空，程序名称也不能只包含空格。
        program = args.command[0]

        if not program.strip():
            return "执行失败：程序名称不能为空。"

        # Windows 下为命令创建新的进程组。
        # 让 ZipClaw 接收 Ctrl+C，再由取消处理逻辑终止命令。
        creation_flags = 0

        if os.name == "nt":
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            # 启动程序，得到一个可用于管理它的进程对象。
            #
            # *args.command 会展开列表：
            # ["git", "--version"]
            # 相当于分别传入 "git" 和 "--version"。
            process = await asyncio.create_subprocess_exec(
                *args.command,

                # 命令从当前工作区开始执行。
                cwd=self.workspace,

                # 不提供交互输入。
                stdin=asyncio.subprocess.DEVNULL,

                # 用管道收集两种输出，不直接显示到终端。
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,

                creationflags=creation_flags,
            )

        except FileNotFoundError:
            return (
                f"启动失败：请检查程序 {program} 是否存在，"
                "以及工作区目录是否有效。"
            )

        # communicate() 同时收集正常输出、错误输出，
        # 并等待程序结束。
        #
        # 将它保存为独立任务，方便中断后继续完成清理。
        output_task = asyncio.create_task(
            process.communicate()
        )

        try:
            # wait_for() 限制等待时间。
            #
            # shield() 保护输出收集任务：
            # 外层超时或被取消时，它暂时继续收集，
            # 以便我们终止进程后收尾。
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                asyncio.shield(output_task),
                timeout=args.timeout,
            )

        except TimeoutError:
            # 超时不能只是停止等待，还需要终止进程。
            await self.stop_process(
                process,
                output_task,
            )

            return (
                f"执行超时：程序运行超过 {args.timeout} 秒，"
                "已结束直接启动的进程。"
            )

        except asyncio.CancelledError:
            # 用户取消任务时，先终止进程并完成清理。
            await self.stop_process(
                process,
                output_task,
            )

            # 继续向外抛出取消异常。
            # CLI 才能记录任务中断、显示提示并恢复聊天。
            raise

        # 异步进程接口返回 bytes，需要自行解码。
        # 无法按 UTF-8 解码的字节用替代字符显示。
        stdout = stdout_bytes.decode(
            "utf-8",
            errors="replace",
        )

        stderr = stderr_bytes.decode(
            "utf-8",
            errors="replace",
        )

        # 保持原来 text=True 的换行转换行为。
        stdout = stdout.replace("\r\n", "\n").replace("\r", "\n")
        stderr = stderr.replace("\r\n", "\n").replace("\r", "\n")

        # 从这里开始，继续保留你原来的输出截断代码。

        # 保存用户指定的长度上限。
        output_limit = args.max_output_chars

        # 正常输出：保留开头。
        stdout_length = len(stdout)

        if stdout_length > output_limit:
            omitted_chars = stdout_length - output_limit

            # [:output_limit] 表示取前 output_limit 个字符。
            stdout = stdout[:output_limit]

            stdout += (
                f"\n……[正常输出已截断，"
                f"省略了后面的 {omitted_chars} 个字符]"
            )

        # 错误输出：保留末尾，方便看到最后的错误类型和原因。
        stderr_length = len(stderr)

        if stderr_length > output_limit:
            omitted_chars = stderr_length - output_limit

            # [-output_limit:] 表示取最后 output_limit 个字符。
            stderr_tail = stderr[-output_limit:]

            truncation_message = (
                f"[错误输出已截断，"
                f"省略了前面的 {omitted_chars} 个字符]\n"
            )

            stderr = truncation_message + stderr_tail

        if not stdout:
            stdout = "（无）"

        if not stderr:
            stderr = "（无）"

        # 用列表逐段组织结果，最后拼成一个字符串。
        output = []

        output.append(f"退出码：{process.returncode}")
        output.append("正常输出：")
        output.append(stdout)
        output.append("错误输出：")
        output.append(stderr)

        return "\n".join(output)

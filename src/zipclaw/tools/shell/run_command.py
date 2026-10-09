from pydantic import BaseModel, Field
from ..base import BaseTool
from pathlib import Path
import subprocess
import asyncio

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

    async def execute(self, args: RunCommandArgs)->str:
        # 列表虽然不能为空，但第一项仍可能是空字符串。
        program = args.command[0]

        if not program.strip():
            return "执行失败：程序名称不能为空。"

        try:
            # subprocess.run() 负责启动程序，并等待它结束。
            #
            # 它会阻塞当前线程，所以使用 asyncio.to_thread()
            # 把等待过程放到另一个线程中执行。
            # await 表示当前方法等待执行结果。
            #asyncio.to_thread() 的设计就是：第一个参数是要调用的函数，后面的参数都转交给这个函数。
            #相当于让另一个线程执行：
            # result = subprocess.run(
            #     args.command,
            #     cwd=self.workspace,
            #     capture_output=True,
            #     timeout=args.timeout,
            #     ......
            # )
            result = await asyncio.to_thread(
                subprocess.run,
                args.command,

                # 程序从哪个目录开始运行。
                cwd=self.workspace,

                # 收集正常输出和错误输出，不直接打印到终端。
                capture_output=True,

                # 将输出解码成字符串。
                text=True,

                encoding="utf-8",

                # 按 UTF-8 解码；解码失败的字符用替代符显示。
                errors="replace",

                # 超过指定时间时抛出 TimeoutExpired。
                timeout=args.timeout,

                # 不提供交互输入，避免程序一直等待用户输入。
                stdin=subprocess.DEVNULL,

                # 直接启动程序，不通过 Shell 解析命令。
                # shell=False（默认）→ 不通过 Shell 解析命令。
                # subprocess.run(["ls", "-la"])
                # 第一项 "ls" 是程序
                # 后面 "-la" 是参数
                # 推荐这种写法，明确、安全
                # shell = True 字符串才会交给系统 Shell 解析
                shell=False,

                # 非零退出码也正常返回，方便模型查看错误并修复。
                check=False,
            )

        except subprocess.TimeoutExpired:
            return f"执行超时：程序运行超过 {args.timeout} 秒。"

        except FileNotFoundError:
            return (
                f"启动失败：请检查程序 {program} 是否存在，"
                "以及工作区目录是否有效。"
            )

            # stdout：正常输出；stderr：错误输出。
            # 没有输出时给一个明确的提示。
        stdout = result.stdout
        stderr = result.stderr

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

        output.append(f"退出码：{result.returncode}")
        output.append("正常输出：")
        output.append(stdout)
        output.append("错误输出：")
        output.append(stderr)

        return "\n".join(output)

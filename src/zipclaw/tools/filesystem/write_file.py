# src/zipclaw/tools/filesystem/write_file.py
#
# 这个工具的职责：在工作区内创建一个新文本文件。
# 它不负责修改已有文件，也不验证写进去的代码能否运行。
#
# 调用链：
# 模型给出参数字典 -> BaseTool.run() 验证参数 -> execute() 创建文件。

# Path 来自 Python 标准库 pathlib，用对象表示和操作文件路径。
# 相比手动拼接字符串，它可以处理不同系统的路径分隔符。
from pathlib import Path

from pydantic import BaseModel, Field

from ..base import BaseTool


# 括号里的 BaseModel 表示继承：WriteFileArgs 是一种 Pydantic 数据模型。
class WriteFileArgs(BaseModel):
    """write_file 的参数；由 BaseTool.run() 在执行前验证。"""

    # path: str 表示这个字段需要一个字符串，例如 "src/main.py"。
    # 单独的 Python 类型标注不会自动校验，但 Pydantic 会读取它并验证输入。
    # Field 没有给出默认值，因此调用者必须提供 path。
    # min_length=1 拒绝空字符串 ""；纯空格仍需下面 execute() 额外检查。
    # description 也会进入工具的 JSON Schema，帮助模型理解参数用途。
    path: str = Field(
        min_length=1,
        description="Relative path of the new file, for example: src/main.py.",
    )

    # content 是要写入文件的完整文本，不是文件路径，也不是局部补丁。
    # 例如 "print('hello')\n"；\n 在字符串里表示换行。
    # 这个字段也必填，但允许 ""，这样就能创建一个内容为空的文件。
    content: str = Field(
        description="Complete text content of the new file. May be empty.",
    )


# 继承 BaseTool，复用工具描述生成、参数验证等逻辑。
# 只需提供下面的配置以及 execute()，就能交给 ToolRegistry 注册和调用。
class WriteFileTool(BaseTool):
    """在工作区内创建 UTF-8 文本文件，不覆盖已有文件。"""

    # 这些写在类里面、方法外面的变量叫类属性。
    # name 是注册表和模型实际使用的工具名，要和其他工具名保持唯一。
    name = "write_file"

    # 括号里相邻的字符串会自动拼接成一个字符串，不是多个列表元素。
    # 描述告诉模型：这是“新建”工具，不要拿它修改已经存在的文件。
    description = (
        "Create a new UTF-8 text file inside the workspace. "
        "Use a path relative to the workspace root and provide the complete content. "
        "Missing parent directories are created automatically. "
        "Refuse to overwrite existing files; this tool is not for editing existing files."
    )

    # 这里保存的是 WriteFileArgs 这个类，不是 WriteFileArgs() 创建的对象。
    # BaseTool.run() 用它把参数字典验证并转换成 WriteFileArgs 实例。
    # 所以 execute() 里可以用 args.path、args.content 访问字段。
    args_model = WriteFileArgs

    # __init__ 是初始化方法：WriteFileTool(workspace=...) 时自动调用。
    # self 代表当前工具实例，workspace: Path 是参数的类型标注。
    def __init__(self, workspace: Path):
        # self.workspace 是实例属性，后面的 execute() 可以继续使用它。
        # resolve() 返回解析后的绝对路径，清理 .、.. 并解析路径中的链接。
        # 它不会替你创建目录；不存在的路径也可能被解析出来。
        self.workspace = workspace.resolve()

    # async def 定义异步方法，外部用 await tool.execute(...) 调用。
    # 这里是为了统一 BaseTool 接口；里面的文件操作仍然是同步操作，
    # 并不会因为加了 async 就自动变成后台线程或异步磁盘读写。
    # -> str 表示预期返回字符串，不是 Python 自动检查的返回值约束。
    async def execute(self, args: WriteFileArgs) -> str:
        """检查路径、创建父目录，再以独占模式创建文件。"""

        # Path 对象的 / 运算符用于拼接路径，不是数字除法。
        # 例如工作区 C:/demo 加上 src/main.py，得到 C:/demo/src/main.py。
        # 如果 args.path 是完整绝对路径，它可能取代左侧路径，
        # 所以“拼接了工作区”本身不代表安全，仍然要检查解析后的 target。
        requested_path = self.workspace / args.path
        target = requested_path.resolve()

        # is_relative_to() 判断 target 是否位于工作区路径范围内。
        # not 表示取反：不在工作区内就拒绝。
        # 必须在 mkdir()、open() 等修改磁盘的操作之前检查边界。
        # 这是一般路径检查，不是能防御所有并发路径替换的系统级沙箱。
        if not target.is_relative_to(self.workspace):
            # raise 是抛出异常，立即离开当前执行流程，而不是普通返回。
            # AgentLoop 已有的 except ValueError 会把它转成工具错误结果。
            raise ValueError("Cannot access files outside workspace.")

        # strip() 返回去掉首尾空白的新字符串，不会修改原来的 args.path。
        # "" 是假值，所以 not args.path.strip() 能识别全是空白的输入。
        # 这里只检查，不把去掉空白后的路径拿去写入，以免改变原路径含义。
        if not args.path.strip():
            raise ValueError("File path cannot be blank.")

        # 符号链接类似一个指向其他路径的入口，不一定是普通文件。
        # 检查未 resolve 的 requested_path，才能识别最后一段是不是链接。
        # 即使链接目标不存在（悬空链接），这里也拒绝创建。
        if requested_path.is_symlink():
            # f"..." 是 f-string，花括号中的表达式会替换为实际值。
            # return 会立即结束这个方法，这里只返回拒绝提示，不会写文件。
            return f"Cannot create file: path is a symbolic link: {args.path}"

        # 路径可能指向一个目录；目录不能当成普通文本文件写入。
        if target.is_dir():
            return f"Cannot create file: path is a directory: {args.path}"

        # exists() 检查是否已经存在，提前给出清楚的拒绝覆盖提示。
        # 但检查和写入之间存在时间间隔，所以不能只依赖这一项保护。
        if target.exists():
            return f"File already exists; not overwritten: {args.path}"

        # parent 是文件的父目录，例如 src/main.py 的父目录是 src。
        # mkdir() 创建目录；它不创建 main.py 文件。
        # parents=True：需要时连同缺失的祖先目录一起创建。
        # exist_ok=True：目录已存在也不报错，但同名普通文件仍会报错。
        # 父目录被文件占用、权限不足等 OSError 交给 AgentLoop 处理。
        target.parent.mkdir(parents=True, exist_ok=True)

        # try / except 用于处理预期异常；这里只单独捕获同名文件已存在。
        try:
            # open("x") 的 x 表示独占创建：文件已经存在就失败。
            # 即使其他程序在 exists() 检查之后创建了同名文件，也不会覆盖。
            # 与之不同，"w" / write_text() 会截断已有文件，所以这里不使用。
            #
            # encoding="utf-8" 指定文本保存为字节时使用的编码，支持中文。
            # newline="" 不自动转换换行符，按传入文本的换行写入。
            #
            # with 是上下文管理语法：离开代码块时会关闭文件，
            # 即使块内抛出异常也会关闭；它并不保证失败时自动撤销写入。
            # as file 把打开的文件对象命名为 file，供代码块内使用。
            with target.open("x", encoding="utf-8", newline="") as file:
                # write() 写入文本，返回字符数量；字符数不等于 UTF-8 字节数。
                written_chars = file.write(args.content)
        except FileExistsError:
            # FileExistsError 是 OSError 的一种，表示文件已经存在。
            # 到这里也不会改动那个已存在的文件。
            return f"File already exists; not overwritten: {args.path}"

        # relative_to() 去掉工作区前缀，得到 src/main.py 这样的相对路径。
        # as_posix() 将显示的路径分隔符统一成 /，方便模型继续使用。
        relative_path = target.relative_to(self.workspace).as_posix()

        # 这个返回值会作为 tool 消息发给模型，不一定原样打印到终端。
        # 只说明创建成功，不代表文件内的代码正确，也不代表测试通过。
        return f"Created {relative_path} ({written_chars} characters)."

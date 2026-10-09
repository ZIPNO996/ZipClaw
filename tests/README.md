# ZipClaw 测试说明

这里测试现有的六个工具、AgentLoop 核心循环和基础会话存储，不调用真实大模型，不需要 API Key，也不会消耗 API 额度。
使用 Python 自带的 `unittest`，不需要额外安装 pytest。
仍然需要项目已有的依赖，例如 Pydantic，以及 Python 3.12 或更高版本。

## 如何运行

在 PowerShell 中进入项目目录，运行全部测试：

```powershell
cd C:\Users\28657\Desktop\ZipClaw
.\.venv\Scripts\python.exe -B -X utf8 -m unittest discover -s tests -v
```

这些参数的意思：

- `.\.venv\Scripts\python.exe`：明确使用项目虚拟环境，不需要先激活。
- `-B`：运行时不生成 `__pycache__` 缓存。
- `-X utf8`：使用 UTF-8，方便显示中文。
- `-m unittest`：运行 Python 自带的测试程序。
- `discover -s tests`：到 tests 目录寻找 `test_` 开头的测试文件。
- `-v`：显示每个测试的名称和执行结果。

只运行编辑工具的测试：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m unittest tests.test_edit_file -v
```

只运行其中一个测试：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m unittest tests.test_edit_file.TestEditFileTool.test_replace_unique_text -v
```

只运行核心循环测试：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m unittest tests.test_agent_loop -v
```

只运行会话存储测试：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m unittest tests.test_session -v
```

在 PyCharm 中，运行某个 `test_` 方法只执行该项；运行测试类或整个文件会执行其中的全部测试。运行配置的解释器应使用项目 `.venv`。

## 测了哪些行为

| 文件 | 主要检查内容 |
| --- | --- |
| `test_write_file.py` | 新建文件、自动创建父目录、空内容、中文和换行、拒绝覆盖、非法参数、越界拦截 |
| `test_read_file.py` | 返回原文、保留换行、空文件、不存在的文件、目录误传、乱码替代、越界拦截 |
| `test_edit_file.py` | 唯一匹配替换、多行替换、删除文本、重复匹配拒绝、无匹配拒绝、保留断言和换行、非法编码不写回 |
| `test_list_dir.py` | 默认目录、排序、条目类型、子目录相对路径、不递归、空目录、结果截断、参数范围 |
| `test_grep.py` | 递归搜索、单文件搜索、行号、大小写、普通文本匹配、跳过缓存和二进制、大小限制、结果截断、读取失败 |
| `test_run_command.py` | 真实启动 Python、工作目录、输出和退出码、中文、参数传递、关闭标准输入、输出截断、超时、程序不存在 |
| `test_filesystem_links.py` | 链接指向工作区外、父目录链接越界、链接条目标记、搜索跳过链接、拒绝通过悬空链接写入 |
| `test_agent_loop.py` | 直接回答、多轮与批量工具调用、消息顺序和调用 ID、错误反馈、历史保留、推理字段、步数上限和显示截断 |
| `test_session.py` | 文件不存在时返回空列表、保存后完整恢复、JSON 损坏时报错且保留原文件 |

多个参数组合使用 `subTest` 放在同一个测试方法中，所以测试方法数量不等于所有输入组合的数量。

## 怎么看懂测试代码

建议先读 `test_write_file.py`，再读 `test_edit_file.py`。
每个测试基本都分三步：

1. 准备：在临时目录里建立文件，设置输入参数。
2. 执行：`await self.tool.run(arguments)` 调用真实工具。
3. 验证：检查返回内容，必要时再次读取磁盘，核对实际文件内容。

常见写法：

```python
# 检查两个值完全相等。
self.assertEqual(actual_content, expected_content)

# 检查返回文本里是否包含某个提示。
self.assertIn("已修改", result)

# 检查文件是否不存在。
self.assertFalse(target.exists())

# 这里预期会抛出异常：不抛出异常反而表示测试失败。
with self.assertRaises(ValueError):
    await self.tool.run(arguments)
```

`setUp()` 会在每个测试开始前执行，准备独立工作区。
`addCleanup()` 注册结束后的清理动作，测试失败也会尝试清理。
`IsolatedAsyncioTestCase` 支持异步测试，所以测试方法可以使用 `async def` 和 `await`。

这里主要从 `run()` 入口调用工具，而不是跳过参数验证直接调用 `execute()`。
因此非法参数测试也会检查 `BaseTool.run()` 中的 Pydantic 校验。

只有模拟文件读取权限不足的测试使用 `patch` 临时替换方法；其他文件读写和命令运行主要使用真实操作。
这样不会为了测试权限错误而更改电脑上的文件访问权限。

## 核心循环测试中的“假模型”是什么

`test_agent_loop.py` 中的 `FakeLLM` 继承真实的 `BaseLLM` 接口，但不联网。
我们提前给它一组 `LLMResponse`，每调用一次 `chat()`，它就返回下一条预设回复。

例如：

1. 第一次回复：调用 `read_file`。
2. 第二次回复：调用 `edit_file`。
3. 第三次回复：返回“修改完成”。

模型回复是预设的，但 AgentLoop、工具注册表、参数验证和文件工具都是真实执行的。
测试会检查模型第二次是否收到了第一次的工具结果，以及临时文件是否真的被修改。
这验证的是执行流程，不是模型是否真的理解了代码、能否自主选择正确的修复方案。

假模型还会用 `deepcopy()` 保存每次请求的快照。
因为 AgentLoop 会不断追加消息，如果只保存同一个列表的引用，就无法检查“某一轮请求时”究竟发送了什么。

`RecordTextTool` 是另一个仅用于测试的小工具，用来记录实际执行次数、模拟权限错误和检查返回值转换。
它不会安装或注册到正式 CLI 中。

核心循环测试包含 20 个测试方法，重点验证：

- 没有工具调用时直接回答，保存历史并停止请求。
- 一条回复中的多个工具按顺序执行，结果对应各自的调用 ID。
- 多轮请求持续携带工具参数说明和此前的完整消息。
- 参数验证失败、工具不存在、越界和文件操作错误会反馈给模型。
- 无效参数不会进入工具的 `execute()`，后续可以用正确参数重试。
- `function.arguments` 是合法 JSON 字符串，推理字段按原值保留。
- 连续任务共享同一个 Agent 的历史，不同 Agent 的历史互不混用。
- 达到步数上限后保留已执行结果，同一实例仍能接收下一次任务。
- 最后一轮直接回答应正常成功，不误报达到上限。
- 终端预览截断不会影响工具实际收到的参数或模型收到的结果。
- 模型请求异常当前仍向外抛出，不会伪造一个成功回答；这不是自动恢复能力。

测试会收集 AgentLoop 的终端输出，避免刷屏；这不改变核心循环的运行逻辑。

## 测试文件会放在哪里

测试数据保存在系统临时目录里，不会修改项目的真实源码、`.env` 或 `calculator.py`。
每个测试结束后自动清理自己的临时目录。

越界测试会在临时根目录里再创建一个 workspace：

```text
临时根目录/
├── workspace/       ← 工具允许访问的范围
└── outside.txt      ← 测试用的“工作区外文件”
```

因此，即使边界检查有错误，也只会触及测试准备的数据。
命令测试使用 `sys.executable` 指定测试自身的 Python 解释器，不依赖系统 PATH 里的 `python`、Git 或网络服务。

## 如何理解结果

- `ok`：该测试通过。
- `FAIL`：实际结果与断言不一致。
- `ERROR`：发生了测试未预期的异常。
- `skipped`：没有执行该项检查，不代表它已经通过。

2026-10-09 最近一次完整运行：共发现 91 项测试，86 项通过，5 项跳过，没有失败项。工具层 68 项、核心循环 20 项、会话存储 3 项。
跳过的 5 项都是符号链接测试，原因是当前系统未授权创建符号链接。
无需为此提升权限；在允许创建符号链接的环境中，它们会自动执行。

运行中偶尔出现 asyncio 的 `Executing ... took ... seconds` 是调试耗时提示；最终是否通过以 unittest 汇总为准。

## 当前边界

已保存的测试覆盖工具层、核心循环和基础会话存储，不是整个 Agent 的完整测试或安全审计。
尚未以正式测试覆盖真实模型接口、适配器解析、CLI 交互，也不证明并发文件替换、写入中断恢复或进程树清理是安全的。
停止后继续的测试直接再次调用 `AgentLoop.run()`，并不代替 CLI 异常捕获或键盘中断测试。
`run_command` 的工作目录不等于操作系统沙箱；本测试只执行预先写好的无害命令。

会话接入、请求失败后的继续输入，以及 12 类无效响应和历史保护另做过临时离线验证，使用假模型或假 SDK 响应和临时目录，没有真实 API 调用。脚本尚未保存到 tests，运行上述发现命令不会执行这些临时检查；下一步应将它们整理成正式的适配器与 CLI 测试。

以后修改工具后，重新运行上述命令即可。出现失败时先对照预期行为排查，不要为了让结果变绿而直接删除断言。

# src/zipclaw/core/loop.py

import json
from ..llm.base import BaseLLM
from ..tools.registry import ToolRegistry
from pydantic import ValidationError
from ..error.custom_errors import AgentStepLimitError

class AgentLoop:
    """
    ZipClaw 最核心的智能体循环。

    基本逻辑：

    模型
     ↓
    判断是否调用工具
     ↓
    如果调用 → 执行工具
     ↓
    把工具结果返回给模型
     ↓
    再让模型判断下一步
    """

    def __init__(
        self,
        llm: BaseLLM,
        tools: ToolRegistry,
        max_steps: int = 10,
        show_tool_logs: bool = False,
        show_reasoning: bool = True,
    ):
        # 使用哪个 模型。
        self.llm = llm

        # 智能体 有哪些工具。
        self.tools = tools

        # 最大循环次数。
        #
        # 防止模型进入死循环，
        # 一直调用工具停不下来。
        self.max_steps = max_steps

        # 控制工具日志是否显示。
        # True：显示工具名称、参数预览和结果预览。
        # False：隐藏这些日志，但工具仍然正常执行并记录到对话历史。
        # 操作前的确认提示单独显示，不受这个开关影响。
        self.show_tool_logs = show_tool_logs

        # 开关控制推理文本的显示
        self.show_reasoning = show_reasoning

        self.messages: list[dict] = [
            {
                "role": "system",
                "content": (
                    "你是一个编程智能体。\n"
                    "根据需要使用工具查看或修改用户的项目。\n"
                    "文件工具使用工作区相对路径；执行命令时，工作目录是工作区根目录。\n"
                    "请使用中文回答用户。\n"
                    "执行 Python 脚本或测试时，优先使用当前工作区的虚拟环境解释器。\n"
                    "Windows 下先检查 .venv/Scripts/python.exe 是否存在。\n"
                    "Linux 或 macOS 下先检查 .venv/bin/python 是否存在。\n"
                    "如果存在，使用该解释器执行，不需要先激活虚拟环境。\n"
                    "如果不存在，不要猜测解释器路径；确认可用的 Python 解释器后再执行。\n"
                ),
            }
        ]

    def confirm_tool_call(
        self,
        name: str,
        arguments: dict,
    ) -> bool:
        """决定是否允许本次工具调用。"""

        # 这些工具只读取信息，不修改文件，也不执行命令。
        # 它们可以直接运行，不需要用户确认。
        read_only_tools = {
            "read_file",
            "list_dir",
            "grep",
        }

        if name in read_only_tools:
            return True

        # 其它工具都需要确认。
        # 以后新增工具时，如果忘记分类，也不会直接自动执行。
        print("\n[等待确认]", flush=True)
        print(f"工具名称：{name}", flush=True)

        # 前面的日志只展示参数的前 300 个字符。
        # 确认操作时，展示完整参数，方便用户检查实际要做什么。
        arguments_text = json.dumps(
            arguments,
            ensure_ascii=False,
            indent=2,
        )

        print("本次操作的完整参数：", flush=True)
        print(arguments_text, flush=True)

        try:
            answer = input("允许执行吗？输入 y 同意，直接回车拒绝 [y/N]：")
        except (EOFError, KeyboardInterrupt):
            # 无法读取输入，或者用户在确认时按下 Ctrl+C，
            # 都不能当成同意执行。
            print("\n本次操作未获得确认，已拒绝。", flush=True)
            return False

        # 去掉前后空格，再统一转换成小写。大小写不敏感。
        answer = answer.strip().lower()

        # 只有明确输入 y 或 yes，才允许执行。
        if answer == "y" or answer == "yes":
            return True

        return False

    async def run(
        self,
        task: str,
    ) -> str:
        """
        执行一个用户任务。
        """

        # messages 就是整个 智能体 当前的上下文。
        # messages = [
        #     {
        #         "role": "system",
        #         "content": (
        #             "你是一个编程智能体。"
        #             "根据需要使用工具查看"
        #             "用户的项目。"
        #         ),
        #     },
        #     {
        #         "role": "user",
        #         "content": task,
        #     },
        # ]


        #append() 修改原列表，不需要赋回；重新创建列表并赋给局部变量，才需要考虑赋回。
        #也就是最后不需要写self.messages=messages
        messages = self.messages

        messages.append({
            "role": "user",
            "content": task,
        })

        # 智能体循环。
        #
        # 每循环一次，可以认为 智能体 做了一步。
        for step_index in range(self.max_steps):

            #range()从0开始，当前步数为循环轮次加一
            current_step = step_index + 1

            #flush=True表示立即输出
            print(
                f"\n[第 {current_step}/{self.max_steps} 轮] 正在请求模型……",
                flush=True,
            )

            # 把当前消息和 工具参数说明 全部交给 模型。
            response = await self.llm.chat(
                messages=messages,
                tools=self.tools.schemas(),
            )


            # 读取模型服务返回的推理文本。
            reasoning = response.reasoning_content

            # 并不是每个模型、每次响应都会提供这个字段。
            if self.show_reasoning:
                if reasoning:
                    print("\n[模型返回的思考过程]", flush=True)
                    print(reasoning, flush=True)

            # ==========================
            # 情况 1：模型 不调用工具
            # ==========================
            #
            # 说明模型认为：
            #
            # "现在信息够了，我可以直接回答用户。"
            if not response.has_tool_calls:
                content = response.content or ""

                # 最终回答也要存起来，否则下一轮只记得用户问了什么，
                # 却不记得模型回答了什么。
                assistant_message = {
                    "role": "assistant",
                    "content": content,
                }

                if response.reasoning_content is not None:
                    assistant_message["reasoning_content"] = response.reasoning_content

                messages.append(assistant_message)
                return content

            # ==========================
            # 情况 2：模型 请求调用 工具
            # ==========================
            # 一次模型响应对应一条 assistant 消息，
            # 其中可能包含多个工具调用。
            assistant_message = {
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(
                                call.arguments,
                                ensure_ascii=False,
                            ),
                        },
                    }
                    for call in response.tool_calls
                ],
            }

            # 原样保留模型返回的字段，不自己编写或拼接内容。
            if response.reasoning_content is not None:
                assistant_message["reasoning_content"] = response.reasoning_content

            messages.append(assistant_message)

            # 再逐个执行工具，将结果与调用 ID 对应起来。
            for call in response.tool_calls:


                # 关闭日志时，不需要生成用于展示的参数预览。
                if self.show_tool_logs:
                    print(f"\n[调用工具] {call.name}", flush=True)

                    # 参数是字典，转换成 JSON 字符串后方便展示。
                    # ensure_ascii=False 让中文直接显示。
                    arguments_text = json.dumps(
                        call.arguments,
                        ensure_ascii=False,
                    )

                    # 写文件的参数可能包含整段代码，因此只展示前 300 个字符。
                    if len(arguments_text) > 300:
                        arguments_preview = arguments_text[:300]
                        arguments_preview += "……[参数显示已截断]"
                    else:
                        arguments_preview = arguments_text

                    print(f"[参数] {arguments_preview}", flush=True)

                try:
                    # 先判断本次调用是否获得执行许可。
                    allowed = self.confirm_tool_call(
                        name=call.name,
                        arguments=call.arguments,
                    )

                    if allowed:
                        # 只有允许执行，才真正调用工具。
                        result = await self.tools.execute(
                            name=call.name,
                            arguments=call.arguments,
                        )
                        content = str(result)

                    else:
                        # 没有执行工具，也要生成对应的工具结果。
                        # 让模型知道：这次操作被拒绝，而不是执行成功。
                        content = (
                            "本次工具调用未获得用户确认，未执行任何操作。"
                            "请向用户说明，不要自行重试或改用其它工具"
                            "完成同一项被拒绝的操作。"
                        )

                except ValidationError as exc:
                    # 工具参数不符合 Pydantic 模型要求。
                    # 不回传原始输入，避免错误信息重复暴露敏感内容。
                    errors = exc.errors(
                        include_input=False,
                        include_url=False,
                        include_context=False,
                    )
                    # 保留字段位置和错误类型，但不用第三方库的英文错误说明。
                    for error in errors:
                        error["msg"] = {
                            "missing": "缺少必填参数。",
                            "string_type": "参数必须是字符串。",
                            "string_too_short": "字符串长度不足。",
                            "int_parsing": "参数无法转换为整数。",
                            "int_type": "参数必须是整数。",
                            "greater_than_equal": "参数低于允许的最小值。",
                            "less_than_equal": "参数超过允许的最大值。",
                        }.get(error["type"], "参数不符合工具要求，请检查类型和取值。")
                    content = (
                            "工具参数校验失败，请修正参数后重试："
                            + json.dumps(errors, ensure_ascii=False)
                    )

                except KeyError:
                    # 当前 registry.get() 用 KeyError 表示工具不存在。
                    content = f"工具不存在：{call.name}，请使用已提供的工具。"

                except ValueError as exc:
                    # 例如 read_file 拒绝访问工作区之外的路径。
                    content = f"工具执行被拒绝：{exc}"

                except OSError:
                    # 文件权限不足、文件在读取前被删除等。
                    content = "文件操作失败，请检查路径、文件是否存在及访问权限。"



                # content 此时已经是工具的执行结果，或者异常处理生成的错误提示。
                # 开关只控制终端显示；下方仍然会保存完整的工具结果。
                if self.show_tool_logs:
                    # 限制终端显示长度，避免读取整个文件时刷满屏幕。
                    if len(content) > 500:
                        result_preview = content[:500]
                        result_preview += "\n……[结果显示已截断]"
                    else:
                        result_preview = content

                    print("[工具结果]", flush=True)
                    print(result_preview, flush=True)




                # 无论成功还是失败，都给本次调用一个结果。
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": content,
                })

        # 如果循环次数超过限制，
        # 很可能 智能体 出现死循环。
        raise AgentStepLimitError(
            f"已达到 {self.max_steps} 轮执行上限，当前任务已停止，"
            "完成情况需要进一步确认。"
        )

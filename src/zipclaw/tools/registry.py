# src/zipclaw/tools/registry.py

from .base import BaseTool


class ToolRegistry:
    """
    负责注册、查找和执行所有 Tool。

    可以把它理解成 Agent 的"工具箱"。
    """

    def __init__(self):
        # key:
        #   工具名称，例如 read_file
        #
        # value:
        #   Tool 对象
        self._tools: dict[str, BaseTool] = {}

    def register(
        self,
        tool: BaseTool,
    ) -> None:
        """
        注册一个 Tool。
        """

        # 防止出现两个相同名字的工具。
        if tool.name in self._tools:
            raise ValueError(
                f"Tool already registered: {tool.name}"
            )

        self._tools[tool.name] = tool

    def get(
        self,
        name: str,
    ) -> BaseTool:
        """
        根据名字寻找工具。
        """

        if name not in self._tools:
            raise KeyError(
                f"Unknown tool: {name}"
            )

        return self._tools[name]

    def schemas(self) -> list[dict]:
        """
        把目前所有工具的 Schema 返回给 LLM。

        假设注册了：

        read_file
        bash

        那么这里就会返回这两个工具对应的 JSON Schema。
        """

        return [
            tool.schema()
            for tool in self._tools.values()
        ]

    async def execute(
        self,
        name: str,
        arguments: dict,
    ):
        """
        根据名字执行工具。

        AgentLoop 不需要知道具体是什么 Tool，
        只需要：

        registry.execute(
            "read_file",
            {"path": "main.py"}
        )
        """

        # 找到对应工具。
        tool = self.get(name)

        # 执行工具。
        #
        # run() 内部还会进行 Pydantic 参数校验。
        return await tool.run(arguments)
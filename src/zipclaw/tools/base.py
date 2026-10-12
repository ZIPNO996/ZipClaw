# src/zipclaw/tools/base.py

from abc import ABC, abstractmethod
from typing import Type

from pydantic import BaseModel


class BaseTool(ABC):
    """
    所有智能体工具的基类。

    例如已经实现或以后可以添加的工具：

    ReadFileTool
    WriteFileTool
    GrepTool
    RunCommandTool
    GitDiffTool
    """

    # 工具名称。
    #
    # 这个名字会直接告诉 模型。
    #
    # 例如：
    # read_file
    name: str

    # 工具说明。
    #
    # 模型 会根据 description 判断：
    # "我什么时候应该调用这个工具？"
    description: str

    # 工具参数的 Pydantic 模型。
    #
    # 例如：
    # ReadFileArgs
    #Type[BaseModel]:一个类，这个类是 BaseModel 或者 BaseModel 的子类
    args_model: Type[BaseModel]

    def schema(self) -> dict:
        """
        生成提供给模型的工具参数结构。

        Pydantic 可以自动把参数模型转换成 JSON 参数结构，
        所以我们不需要自己手写 properties / required。
        """

        parameters = self.args_model.model_json_schema()
        description = self.description
        if getattr(self, "full_access", False):
            # 同时告诉模型实际权限，避免描述仍要求只能操作工作区。
            description += (
                "当前为完全访问模式，允许工作区外的路径，"
                "此规则覆盖上文的工作区范围限制；其它参数约束仍然有效。"
            )
            path_schema = parameters.get("properties", {}).get("path")
            if path_schema is not None:
                path_schema["description"] = (
                    "允许绝对路径或工作区相对路径，可访问工作区外的文件或目录。"
                )

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": description,

                # Pydantic 自动生成 JSON 参数结构。
                # 这里参数的description也会告知给模型。
                "parameters": parameters,
            },
        }

    async def run(self, arguments: dict):
        """
        工具统一执行入口。

        模型参数解析后是一个普通字典：

        {
            "path": "main.py"
        }

        这里首先通过 Pydantic 做参数校验，
        然后再真正执行工具。
        """

        # 验证模型传过来的参数是否合法。
        args = self.args_model.model_validate(arguments)

        # 参数校验通过以后，
        # 才进入具体 工具 的 execute()。
        return await self.execute(args)

    @abstractmethod
    async def execute(self, args: BaseModel):
        """
        真正执行工具。

        每一个具体工具都必须自己实现。
        """

        ...

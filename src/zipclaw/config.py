"""集中读取启动配置：转换环境变量的类型，并提供清楚的错误提示。"""

import os
from dataclasses import dataclass, field


def read_required_env(name: str) -> str:
    """读取必填的字符串配置，未设置或只包含空白时拒绝启动。"""

    value = os.getenv(name)

    if value is None or not value.strip():
        # 错误提示只包含变量名，不包含配置值，避免打印密钥。
        raise ValueError(f"请设置环境变量 {name}，并且不要留空。")

    return value.strip()


def read_bool_env(name: str, default: bool) -> bool:
    """读取 true / false；未设置时使用默认值，填写错误时明确报错。"""

    value = os.getenv(name)

    if value is None:
        return default

    # 环境变量读出来是字符串。
    # 不能直接 bool(value)：bool("false") 也是 True。
    normalized_value = value.strip().lower()

    if normalized_value == "true":
        return True

    if normalized_value == "false":
        return False

    raise ValueError(f"环境变量 {name} 必须填写 true 或 false。")


def read_int_env(name: str, default: int, minimum: int = 1) -> int:
    """读取整数配置，并检查它是否达到允许的最小值。"""

    value = os.getenv(name)

    if value is None:
        number = default
    else:
        try:
            # 比如把字符串 "10" 转换成整数 10。
            # "十"、"1.5" 或空字符串不能转换为整数。
            number = int(value.strip())
        except ValueError:
            # 不重复显示原始值，也不向用户展示底层转换异常。
            raise ValueError(f"环境变量 {name} 必须填写整数。") from None

    if number < minimum:
        raise ValueError(f"环境变量 {name} 必须大于或等于 {minimum}。")

    return number


#@dataclass 是 Python 标准库 dataclasses 提供的装饰器。加上它之后，你只声明字段，Python 会自动帮你生成 __init__()、__repr__()、__eq__() 等方法。
@dataclass
class AppConfig:
    """保存已经读取并检查过的配置，供 CLI 创建模型和智能体时使用。"""

    # dataclass 会根据下面的字段自动生成 __init__()。
    # field(repr=False) 让打印整个配置对象时不显示 API Key。
    # 它并不是加密；程序仍可通过 config.api_key 取出密钥。
    api_key: str = field(repr=False)
    model: str
    base_url: str | None

    show_tool_logs: bool
    show_reasoning: bool
    max_steps: int
    context_char_budget: int
    keep_recent_turns: int
    thinking_mode: str


def load_config() -> AppConfig:
    """读取当前进程环境变量；CLI 应先执行 load_dotenv()。"""

    # 本函数不自行查找 .env，避免改变原来 CLI 的配置加载位置。
    # 也不在导入模块时读取，方便重新调用和独立验证。
    api_key = read_required_env("USTC_API_KEY")
    model = read_required_env("USTC_MODEL")

    # 地址沿用原来的可选行为：未设置或为空时，交给 SDK 使用默认地址。
    # 使用学校的兼容服务时，应在 .env 中保留对应的 USTC_BASE_URL。
    base_url_value = os.getenv("USTC_BASE_URL")
    base_url = None

    if base_url_value is not None:
        stripped_url = base_url_value.strip()

        if stripped_url:
            base_url = stripped_url

    show_tool_logs = read_bool_env(
        "ZIPCLAW_SHOW_TOOL_LOGS",
        default=False,
    )
    show_reasoning = read_bool_env(
        "ZIPCLAW_SHOW_REASONING",
        default=True,
    )
    max_steps = read_int_env(
        "ZIPCLAW_MAX_STEPS",
        default=10,
        minimum=1,
    )
    context_char_budget = read_int_env(
        "ZIPCLAW_CONTEXT_CHAR_BUDGET",
        default=60000,
        minimum=1,
    )

    keep_recent_turns = read_int_env(
        "ZIPCLAW_KEEP_RECENT_TURNS",
        default=2,
        minimum=1,
    )

    # 默认不向服务端发送 thinking 参数。
    # 只有确认当前接口支持这种格式时，才设置 enabled 或 disabled。
    thinking_mode = os.getenv(
        "ZIPCLAW_THINKING_MODE",
        "default",
    ).strip().lower()

    if thinking_mode not in {"default", "enabled", "disabled"}:
        raise ValueError(
            "环境变量 ZIPCLAW_THINKING_MODE "
            "必须填写 default、enabled 或 disabled。"
        )

    # 用字段名传入值，方便阅读，也避免混淆参数顺序。
    return AppConfig(
        api_key=api_key,
        model=model,
        base_url=base_url,
        show_tool_logs=show_tool_logs,
        show_reasoning=show_reasoning,
        max_steps=max_steps,
        context_char_budget=context_char_budget,
        keep_recent_turns=keep_recent_turns,
        thinking_mode=thinking_mode,
    )

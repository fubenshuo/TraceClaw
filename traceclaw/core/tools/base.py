"""
TraceClaw 工具基础设施
======================
定义工具的两种创建方式：装饰器模式（简单工具）和类模式（复杂工具）。

使用方式 1：装饰器模式（推荐用于简单工具）
  @traceclaw_tool
  def my_tool(param: str) -> str:
      \"""工具描述（会自动成为 tool description）\"""
      return f"结果: {param}"

使用方式 2：类模式（需要维护状态或复杂初始化）
  class MyTool(TraceClawBaseTool):
      name = "my_tool"
      description = "工具描述"
      args_schema = MyArgs  # Pydantic BaseModel

      def _run(self, param: str) -> str:
          return f"结果: {param}"

面试要点：
  - traceclaw_tool 其实就是 LangChain 的 @tool 装饰器，加别名是为了：
    1. 让框架用户不需要直接 import langchain
    2. 未来可以在别名里加入 TraceClaw 特有的行为（如自动审计、权限校验）
  - TraceClawBaseTool 预留了 required_permission_level 和 timeout_seconds 字段，
    这是面向未来的扩展点——当前版本未启用，但架构上已准备好
"""

from typing import Any, Type
from langchain_core.tools import BaseTool, tool
from abc import ABC, abstractmethod
import asyncio
from pydantic import BaseModel, Field

# ============================================================
# traceclaw_tool — 函数式工具装饰器
# ============================================================
# 将 LangChain 原生的 @tool 装饰器重命名并暴露出去。
# 开发者在使用 traceclaw 写简单工具时，只需要加一个装饰器和写好 docstring 即可。
# docstring 会自动成为工具的 description，参数类型注解会自动生成 args_schema。
# ============================================================
traceclaw_tool = tool

# 类模式工具（适合复杂场景）
class TraceClawBaseTool(BaseTool, ABC):
    """
    TraceClaw 的标准工具基类。
    如果你的工具需要复杂的初始化逻辑（比如维持一个数据库长连接），
    或者需要保存内部状态，请继承此类并实现 `_run` 方法。

    与装饰器模式的区别：
      - 装饰器模式：适合纯函数，无状态，每次调用独立
      - 类模式：适合有状态工具——比如需要维护数据库连接池、HTTP Session、
        或者在多次调用之间缓存计算结果

    必填字段：
      - name:           工具的唯一标识名（LLM 通过这个名字决定调用哪个工具）
      - description:    工具的功能描述（LLM 据此判断何时调用此工具）
      - args_schema:    参数的 Pydantic 模型（定义工具接受哪些参数及其类型）
    """

    # ============================================================
    # 预留扩展字段（当前版本未启用）
    # ============================================================
    # 未来在扩展层（Extended）做权限控制时，可以用到这个字段
    # 例如：0=无限制, 1=需要用户确认, 2=仅管理员
    # required_permission_level: int = 0

    # 也可以加上工具运行超时限制等统一配置
    # 例如：工具执行超过 30 秒则自动熔断，防止 LLM 卡死
    # timeout_seconds: int = 30
    # ============================================================

    name: str                     # 工具唯一名称（如 "get_weather"）
    description: str              # 工具功能描述（LLM 阅读后决定是否调用）
    args_schema: Type[BaseModel]  # 参数的 Pydantic Schema（自动生成 JSON Schema 给 LLM）

    @abstractmethod
    def _run(self, **kwargs: Any) -> Any:
        """
        工具的同步执行逻辑，子类必须实现。

        LangChain 框架在 LLM 决定调用此工具时，会自动解析 LLM 传回的参数，
        并以 **kwargs 形式传入此方法。返回值会被序列化后作为 ToolMessage 返还给 LLM。

        注意：如果工具涉及 I/O 或网络请求，建议同步实现 _run，
        然后通过 _arun 的 asyncio.to_thread 在线程池中执行以避免阻塞 event loop。
        """
        raise NotImplementedError("子类必须实现 _run 方法")

    async def _arun(self, **kwargs: Any) -> Any:
        """
        工具的异步执行逻辑（可选）。如果你的工具涉及网络请求，强烈建议实现。

        默认实现：在线程池中运行同步的 _run 方法。
        对于纯 I/O 操作（HTTP 请求、文件读写），覆盖此方法并使用原生 async/await
        可以获得更好的并发性能。

        示例覆盖写法：
            async def _arun(self, url: str) -> str:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        return await resp.text()
        """
        # 默认回退到同步执行（在线程池中运行，避免阻塞 event loop）
        return await asyncio.to_thread(self._run, **kwargs)

# =========================用法============================
# 以下是 TraceClawBaseTool 的使用示例（被注释掉，仅作参考）：
#
# 1. 先定义参数的 Pydantic 模型：
# class AddArgs(BaseModel):
#     a: int = Field(description="第一个加数")
#     b: int = Field(description="第二个加数")
#
#
# 2. 继承 TraceClawBaseTool 并实现 _run：
# class AddTool(TraceClawBaseTool):
#     name: str = "add"
#     description: str = "计算两个数的和"
#     args_schema: Type[BaseModel] = AddArgs
#
#     def _run(self, a: int, b: int) -> int:
#         return a + b
#
#
# 3. 使用工具：
# if __name__ == "__main__":
#     tool_instance = AddTool()
#
#     # 直接调用工具
#     result = tool_instance.invoke({"a": 2, "b": 3})
#     print("invoke result:", result)
#
#     # 异步调用工具
#     async def main():
#         result_async = await tool_instance.ainvoke({"a": 10, "b": 20})
#         print("ainvoke result:", result_async)
#
#     asyncio.run(main())
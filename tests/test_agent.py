"""
Agent 核心模块测试
==================
测试 AgentState 初始化 和 create_agent_app 的三种构建模式。

测试覆盖：
  1. AgentState 初始化验证（空状态创建）
  2. create_agent_app 基础创建（Mock Provider + 空工具列表）
  3. create_agent_app 自定义工具（传入外部工具列表）
  4. create_agent_app 带 Checkpointer（MemorySaver 持久化）

Mock 策略：
  - get_provider 被 Mock，因为测试不关心真实 LLM 调用
  - load_dynamic_skills 被 Mock，因为测试不需要扫描文件系统
  - BUILTIN_TOOLS 被 patch 为空列表，减少干扰

面试要点：
  - @patch 装饰器的 target 格式："模块路径.函数名"（不是物理路径）
  - Mock 的 bind_tools 为什么也要 Mock？→ LLM 实例的 bind_tools 是链式调用的一环
"""

import unittest
import os
import sys
from unittest.mock import Mock, patch, MagicMock

# 将项目根目录添加到 sys.path，确保可以 import traceclaw 包
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from traceclaw.core.context import AgentState
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage


class TestAgent(unittest.TestCase):

    def test_agent_state_initialization(self):
        """测试 AgentState 的初始化"""
        from traceclaw.core.context import AgentState

        # 创建一个空的 AgentState（TypedDict 用类似构造函数的语法）
        initial_state = AgentState(
            messages=[],     # 消息列表初始为空
            summary=""       # 摘要初始为空字符串
        )

        # 验证 TypedDict 的键值对存储正确
        self.assertEqual(initial_state["messages"], [])
        self.assertEqual(initial_state["summary"], "")

    # ── 三个 @patch 装饰器的含义 ──
    # @patch('traceclaw.core.provider.get_provider'):
    #   替换 agent.py 中 import 的 get_provider 函数——不真的去连接 LLM
    # @patch('traceclaw.core.skill_loader.load_dynamic_skills'):
    #   替换技能扫描函数——不真的去读文件系统
    # @patch('traceclaw.core.tools.builtins.BUILTIN_TOOLS', []):
    #   将内置工具列表替换为空列表——测试只关注 Graph 结构本身，不需要真实工具
    # 参数顺序：自下而上（最内层的 patch 对应第一个参数）
    @patch('traceclaw.core.provider.get_provider')
    @patch('traceclaw.core.skill_loader.load_dynamic_skills')
    @patch('traceclaw.core.tools.builtins.BUILTIN_TOOLS', [])
    def test_create_agent_app_basic(self, mock_load_skills, mock_get_provider):
        """测试创建基础代理应用（带 Mock）"""
        from traceclaw.core.agent import create_agent_app

        # ── Mock LLM Provider ──
        # Mock() 创建一个万能假对象，任何属性访问都返回新的 Mock
        mock_provider = Mock()
        # bind_tools 被调用时会返回另一个 Mock（LLM 绑定工具后的实例）
        mock_provider.bind_tools.return_value = Mock()
        mock_get_provider.return_value = mock_provider

        # Mock 动态技能加载返回空列表（不加载任何外部技能）
        mock_load_skills.return_value = []

        try:
            # 只传入 provider 和 model，不传 tools 和 checkpointer
            # → 应该使用 BUILTIN_TOOLS（已被 patch 为空列表）+ 动态技能（Mock 为空）
            app = create_agent_app(provider_name="openai", model_name="gpt-4o-mini")
            # 验证：编译后的 LangGraph 应用不为 None
            self.assertIsNotNone(app)
        except Exception as e:
            # 即使出现其他错误也记录
            print(f"Unexpected error: {e}")
            raise

    @patch('traceclaw.core.provider.get_provider')
    @patch('traceclaw.core.skill_loader.load_dynamic_skills')
    @patch('traceclaw.core.tools.builtins.BUILTIN_TOOLS', [])
    def test_create_agent_app_with_custom_tools(self, mock_load_skills, mock_get_provider):
        """测试创建带有自定义工具的代理应用（带 Mock）"""
        from traceclaw.core.agent import create_agent_app
        from langchain_core.tools import tool

        # Mock provider 返回值
        mock_provider = Mock()
        mock_provider.bind_tools.return_value = Mock()
        mock_get_provider.return_value = mock_provider

        # Mock 动态技能加载
        mock_load_skills.return_value = []

        # ── 创建一个真正的 LangChain tool（不是 Mock，是真实的 @tool 装饰器产物） ──
        # 使用 LangChain 的 @tool 装饰器创建一个测试用工具
        @tool
        def mock_tool(test_param: str) -> str:
            """A mock tool for testing"""
            return f"mock result: {test_param}"

        try:
            # 传入自定义工具列表 → create_agent_app 应该用这个替代 BUILTIN_TOOLS + 动态技能
            app = create_agent_app(
                provider_name="openai",
                model_name="gpt-4o-mini",
                tools=[mock_tool]          # 外部传入的工具列表
            )
            self.assertIsNotNone(app)
        except Exception as e:
            print(f"Unexpected error: {e}")
            raise

    @patch('traceclaw.core.provider.get_provider')
    @patch('traceclaw.core.skill_loader.load_dynamic_skills')
    @patch('traceclaw.core.tools.builtins.BUILTIN_TOOLS', [])
    def test_create_agent_app_with_checkpointer(self, mock_load_skills, mock_get_provider):
        """测试创建带有检查点的代理应用（带 Mock）"""
        from traceclaw.core.agent import create_agent_app
        # MemorySaver 是 LangGraph 提供的内存级 Checkpointer（对比生产环境的 SQLite）
        from langgraph.checkpoint.memory import MemorySaver

        # Mock provider 返回值
        mock_provider = Mock()
        mock_provider.bind_tools.return_value = Mock()
        mock_get_provider.return_value = mock_provider

        # Mock 动态技能加载
        mock_load_skills.return_value = []

        # ── 使用内存 Checkpointer ──
        # 生产环境用 AsyncSqliteSaver（SQLite 持久化），测试环境用 MemorySaver（内存，不落盘）
        memory_saver = MemorySaver()
        try:
            app = create_agent_app(
                provider_name="openai",
                model_name="gpt-4o-mini",
                checkpointer=memory_saver   # 传入 checkpointer 启用对话持久化
            )
            self.assertIsNotNone(app)
        except Exception as e:
            print(f"Unexpected error: {e}")
            raise


if __name__ == '__main__':
    unittest.main()

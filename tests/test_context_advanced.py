"""
上下文裁剪算法高级测试
======================
深入测试 trim_context_messages 在各种边界条件下的行为。

trim_context_messages 是 TraceClaw 记忆系统的核心算法——当对话回合数
超过 trigger_turns 时，将旧对话丢弃并保留最近 keep_turns 个回合。

回合定义：从一个 HumanMessage 开始，到下一个 HumanMessage 之前的
所有消息属于同一个"回合"。SystemMessage 不属于任何回合。

测试矩阵：
  1. 消息数低于阈值 → 全部保留
  2. 消息数超过阈值 → 裁剪旧回合，保留最近 N 回合 + SystemMessage
  3. 无 SystemMessage 的纯对话
  4. 只有 SystemMessage（无对话）
  5. 空消息列表
  6. 含 ToolMessage 的复杂回合（一个回合内可能有多次工具调用）
  7. 回合计数逻辑验证（Human→AI→Tool→AI 算一个回合）

面试要点：
  - 回合 vs 消息的区别：5 条消息可能只有 2 个回合
  - SystemMessage 的特殊处理：永远保留（系统指令不能丢）
  - keep 的是"最后 N 回合"而非"前 N 回合"——保留最近的上下文
"""

import unittest
import os
import sys
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from traceclaw.core.context import trim_context_messages, AgentState


class TestContextTrimming(unittest.TestCase):
    """trim_context_messages 函数的核心场景测试"""

    def test_trim_with_system_message_keep_all(self):
        """
        测试保留所有消息的情况（不超过阈值）。

        场景：5 条消息 = 1 条 SystemMessage + 2 个对话回合
        trigger_turns=10 → 2 回合 < 10 → 不裁剪，全部保留
        """
        messages = [
            SystemMessage(content="系统消息"),
            HumanMessage(content="用户消息1"),
            AIMessage(content="AI消息1"),
            HumanMessage(content="用户消息2"),
            AIMessage(content="AI消息2")
        ]

        kept, discarded = trim_context_messages(messages, trigger_turns=10, keep_turns=10)

        # 由于回合数(2) < 触发阈值(10)，不应裁剪
        self.assertEqual(len(kept), 5)        # 包含系统消息 + 4 条对话消息
        self.assertEqual(len(discarded), 0)    # 没有消息被丢弃

    def test_trim_with_system_message_discard_some(self):
        """
        测试裁剪部分消息的情况。

        场景：11 条消息 = 1 条 SystemMessage + 5 个对话回合
        trigger_turns=3, keep_turns=2 → 5 回合 > 3 → 裁剪
        保留：SystemMessage + 最后 2 回合（4 条消息）= 5 条
        丢弃：前 3 回合（6 条消息）
        """
        messages = [
            SystemMessage(content="系统消息"),
            HumanMessage(content="用户消息1"),
            AIMessage(content="AI消息1"),
            HumanMessage(content="用户消息2"),
            AIMessage(content="AI消息2"),
            HumanMessage(content="用户消息3"),
            AIMessage(content="AI消息3"),
            HumanMessage(content="用户消息4"),
            AIMessage(content="AI消息4"),
            HumanMessage(content="用户消息5"),
            AIMessage(content="AI消息5")
        ]

        kept, discarded = trim_context_messages(messages, trigger_turns=3, keep_turns=2)

        # 由于回合数(5) > 触发阈值(3)，应裁剪
        # 保留最后2个回合 + 系统消息 = 5条消息
        self.assertEqual(len(kept), 5)
        self.assertEqual(len(discarded), 6)  # 前3个回合的消息

        # 验证系统消息在保留的消息中（且永远在第一位）
        self.assertIsInstance(kept[0], SystemMessage)

        # 验证保留的是最后2个回合（Human4-AI4 → Human5-AI5）
        self.assertIsInstance(kept[1], HumanMessage)
        self.assertIsInstance(kept[2], AIMessage)
        self.assertIsInstance(kept[3], HumanMessage)
        self.assertIsInstance(kept[4], AIMessage)

    def test_trim_without_system_message(self):
        """
        测试没有系统消息时的裁剪。

        场景：6 条消息 = 3 个对话回合，无 SystemMessage
        trigger_turns=2, keep_turns=1 → 3 > 2 → 裁剪
        保留：最后 1 回合（2 条消息）
        丢弃：前 2 回合（4 条消息）
        """
        messages = [
            HumanMessage(content="用户消息1"),
            AIMessage(content="AI消息1"),
            HumanMessage(content="用户消息2"),
            AIMessage(content="AI消息2"),
            HumanMessage(content="用户消息3"),
            AIMessage(content="AI消息3")
        ]

        kept, discarded = trim_context_messages(messages, trigger_turns=2, keep_turns=1)

        # 回合数(3) > 触发阈值(2)，保留最后1个回合
        self.assertEqual(len(kept), 2)       # 最后一个回合(Human+AI)
        self.assertEqual(len(discarded), 4)  # 前2个回合

    def test_trim_only_system_message(self):
        """
        测试只有系统消息的情况。

        SystemMessage 不构成回合（turn_count = 0），
        所以即使 trigger_turns=1，0 < 1 → 不裁剪。
        SystemMessage 总是被保留。
        """
        messages = [
            SystemMessage(content="系统消息")
        ]

        kept, discarded = trim_context_messages(messages, trigger_turns=1, keep_turns=1)

        self.assertEqual(len(kept), 1)
        self.assertEqual(len(discarded), 0)
        self.assertIsInstance(kept[0], SystemMessage)

    def test_trim_empty_messages(self):
        """
        测试空消息列表。

        空列表 → 0 回合 → 不触发裁剪 → 返回两个空列表。
        """
        messages = []

        kept, discarded = trim_context_messages(messages, trigger_turns=1, keep_turns=1)

        self.assertEqual(len(kept), 0)
        self.assertEqual(len(discarded), 0)

    def test_trim_with_tool_messages(self):
        """
        测试包含工具消息的裁剪。

        场景：9 条消息，3 个回合（每个回合含 ToolMessage）
        trigger_turns=2, keep_turns=1 → 裁剪
        保留：最后 1 回合
        丢弃：前 2 回合的所有消息

        回合结构：
          回合 1: Human1 → AI1 → Tool1
          回合 2: Human2 → AI2 → Tool2
          回合 3: Human3 → AI3
        """
        messages = [
            SystemMessage(content="系统消息"),
            HumanMessage(content="用户消息1"),
            AIMessage(content="AI消息1"),
            ToolMessage(content="工具结果1", tool_call_id="1"),
            HumanMessage(content="用户消息2"),
            AIMessage(content="AI消息2"),
            ToolMessage(content="工具结果2", tool_call_id="2"),
            HumanMessage(content="用户消息3"),
            AIMessage(content="AI消息3")
        ]

        kept, discarded = trim_context_messages(messages, trigger_turns=2, keep_turns=1)

        # 3个回合(每回合可能包含多个消息) > 阈值2，保留最后1个回合
        # 最后一个回合：HumanMessage + AIMessage
        # 所以前面两回合的所有消息都被丢弃
        self.assertEqual(len(discarded), 6)  # 前两个回合加上系统消息
        self.assertEqual(len(kept), 3)       # 最后一个回合的Human + AI

    def test_turn_calculation_logic(self):
        """
        测试回合计算逻辑 — 最关键的一个测试。

        这个测试验证：多个 AIMessage + ToolMessage 在同一个回合内
        不会增加回合数。回合的边界是 HumanMessage。

        回合结构：
          回合 1 (5条): Human1, AI1a, Tool1a, AI1b, Tool1b
          回合 2 (2条): Human2, AI2
          回合 3 (4条): Human3, AI3a, Tool3a, AI3b
          总计 = 3 个回合

        trigger_turns=2 → 3 > 2 → 裁剪
        keep_turns=1 → 保留最后 1 回合 = 4 条消息
        丢弃 = 前 2 回合 = 7 条消息
        """
        messages = [
            HumanMessage(content="用户消息1"),
            AIMessage(content="AI消息1a"),
            ToolMessage(content="工具结果1a", tool_call_id="1a"),
            AIMessage(content="AI消息1b"),
            ToolMessage(content="工具结果1b", tool_call_id="1b"),
            HumanMessage(content="用户消息2"),
            AIMessage(content="AI消息2"),
            HumanMessage(content="用户消息3"),
            AIMessage(content="AI消息3a"),
            ToolMessage(content="工具结果3a", tool_call_id="3a"),
            AIMessage(content="AI消息3b")
        ]

        # 测试回合是如何计算的
        # 回合1: Human1, AI1a, Tool1a, AI1b, Tool1b
        # 回合2: Human2, AI2
        # 回合3: Human3, AI3a, Tool3a, AI3b
        # 总共3个回合

        kept, discarded = trim_context_messages(messages, trigger_turns=2, keep_turns=1)

        # 3回合 > 阈值2，保留最后1回合
        self.assertEqual(len(kept), 4)       # Human3, AI3a, Tool3a, AI3b
        self.assertEqual(len(discarded), 7)  # 前两个回合的所有消息


class TestAgentState(unittest.TestCase):
    """AgentState TypedDict 的初始化测试"""

    def test_agent_state_initialization(self):
        """
        测试 AgentState 的默认初始化。

        AgentState 是 TypedDict（非普通类），用类似字典的语法访问。
        messages 是 List[BaseMessage]，summary 是 str。
        """
        initial_state = AgentState(
            messages=[],
            summary=""
        )

        self.assertEqual(initial_state["messages"], [])
        self.assertEqual(initial_state["summary"], "")

    def test_agent_state_with_messages(self):
        """
        测试带消息和摘要的 AgentState。

        验证 TypedDict 的键值存取正确。
        """
        messages = [
            HumanMessage(content="用户消息"),
            AIMessage(content="AI消息")
        ]

        state = AgentState(
            messages=messages,
            summary="测试摘要"
        )

        self.assertEqual(len(state["messages"]), 2)
        self.assertEqual(state["summary"], "测试摘要")


if __name__ == '__main__':
    unittest.main()

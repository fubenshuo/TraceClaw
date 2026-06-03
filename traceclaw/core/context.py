"""
TraceClaw 上下文管理与记忆压缩
==============================
实现"双水位记忆系统"中的上下文裁剪逻辑。

核心概念：
  1. AgentState — LangGraph 的状态数据结构
     - messages: 对话历史（使用 add_messages reducer 追加而非覆盖）
     - summary:  被裁剪掉的旧对话的压缩摘要（字符串）

  2. trim_context_messages — 按"对话回合"裁剪上下文
     - 一个回合 = HumanMessage + 后续所有非 HumanMessage（AIMessage、ToolMessage 等）
     - 裁剪边界始终对齐回合边界——保证语义完整性，不会出现"tool_call 发了但 tool_result 被裁掉"的情况
     - 返回两个值：(保留的消息, 被丢弃的消息)

面试核心考点：
  - 为什么按"回合"而不是按"消息条数"裁剪？→ 防止拆散 tool_call 和 tool_result
  - 为什么 trigger_turns 默认 8、keep_turns 默认 4？→ 平衡上下文长度与记忆连续性
"""

from typing import Annotated, TypedDict
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage
from langgraph.graph.message import add_messages

class AgentState(TypedDict):
    """
    LangGraph StateGraph 的状态结构定义。
    在 agent → tools → agent 循环中，这个字典在节点之间传递和更新。

    messages:
      - 类型是 list[BaseMessage]，但使用了 Annotated + add_messages reducer
      - add_messages 的含义：当节点返回 {"messages": [new_msg]} 时，
        LangGraph 会自动把 new_msg 追加到现有列表末尾，而不是覆盖。
      - 这是 LangGraph 状态管理的核心机制——"累加"而非"替换"。

    summary:
      - 当对话轮次超过 trigger_turns 时，旧对话被裁剪，
        agent_node 会调用 LLM 将裁剪掉的内容压缩为一段摘要字符串存入此字段。
      - 下一次对话时，摘要会被注入系统 Prompt，让 Agent 知道"之前聊了什么"。
    """
    # 存储对话历史。
    messages: Annotated[list[BaseMessage], add_messages]

    # 摘要压缩
    summary: str

def trim_context_messages(messages: list[BaseMessage], trigger_turns: int = 8, keep_turns: int = 4) -> tuple[list[BaseMessage], list[BaseMessage]]:
    """ 
    按对话回合裁剪上下文消息。

    核心算法：
      1. 提取 SystemMessage（系统 Prompt），单独保管
      2. 将非系统消息按"回合"分组：每个回合从 HumanMessage 开始，
         到下一个 HumanMessage 之前的所有消息属于同一回合
      3. 如果总回合数 < trigger_turns → 不裁剪，全部保留
      4. 否则保留最近 keep_turns 个回合，其余裁剪后标记为丢弃

    Args:
        messages: 完整的消息列表（包含 SystemMessage）
        trigger_turns: 触发裁剪的回合数阈值，默认 8
        keep_turns:   裁剪后保留的最近回合数，默认 4

    Returns:
        (final_messages, discarded_messages):
          - final_messages: 保留的消息（SystemMessage + 最近 keep_turns 个回合）
          - discarded_messages: 被丢弃的消息（用于生成摘要后永久删除）

    设计要点：
      - SystemMessage 始终保留——丢了系统 Prompt 会导致 Agent 行为异常
      - 裁剪粒度是"回合"而非"条"——一个回合可能包含 HumanMessage + AIMessage(tool_calls) + ToolMessage + AIMessage
      - 如果一条 tool_call 的返回结果被裁掉，LLM 会看到"悬空"的工具调用——这是语义断裂
    """
    # 按照完整用户回合来裁剪上下文：即 一个会从HumanMessage开始，直到下一个HumanMessage结束，会把AIMessage、tool_calls、ToolMessage一并保留
    # ── 步骤 1：提取 SystemMessage（系统 Prompt 不参与裁剪）──
    first_system = next((m for m in messages if isinstance(m, SystemMessage)), None)
    non_system_msgs = [m for m in messages if not isinstance(m, SystemMessage)]


    # ── 步骤 2：空消息列表的边界情况 ──
    if not non_system_msgs:
        return ([first_system] if first_system else []), []

    # ── 步骤 3：按"用户回合"分组 ──
    # 规则：遇到 HumanMessage → 开启新回合；遇到其他消息 → 追加到当前回合
    turns: list[list[BaseMessage]] = []     # 存储所有回合，每个回合是一个消息列表(current_turn)
    current_turn: list[BaseMessage] = []    # 当前回合的消息列表，初始为空

    # 遍历非系统信息，按回合进行分组
    for msg in non_system_msgs:
        if isinstance(msg, HumanMessage):
            # HumanMessage 标志着一个新回合的开始
            if current_turn:
                turns.append(current_turn)   # 保存上一个回合
            current_turn = [msg]             # 开启新回合, 从当前 HumanMessage 开始
        else:
            # AIMessage、ToolMessage、tool_calls 等都属于当前回合
            if current_turn:
                current_turn.append(msg)

    # 保存最后一个回合
    if current_turn:
        turns.append(current_turn)

    total_turns = len(turns)    # 计算总回合数(按照 HumanMessage 分组的回合数)

    # ── 步骤 4：回合数未达阈值 → 全部保留，不裁剪 ──
    if total_turns < trigger_turns:
        final_messages = ([first_system] if first_system else []) + non_system_msgs
        return final_messages, []   # 全部保留，丢弃列表为空(无需生成摘要)

    # ── 步骤 5：回合数超过阈值 → 保留最近 N 个回合，丢弃旧回合 ──
    recent_turns = turns[-keep_turns:]        # 最近 keep_turns 个回合
    discarded_turns = turns[:-keep_turns]     # 前面被丢弃的回合

    # 组装保留的消息：SystemMessage 在最前面 + 最近的回合
    final_messages: list[BaseMessage] = []
    if first_system:
        final_messages.append(first_system)
    for turn in recent_turns:
        final_messages.extend(turn)

    # 组装被丢弃的消息：这些将被 agent_node 用于生成摘要，
    # 然后通过 RemoveMessage 命令从 SQLite 中永久删除。
    discarded_messages: list[BaseMessage] = []
    for turn in discarded_turns:
        discarded_messages.extend(turn)

    return final_messages, discarded_messages

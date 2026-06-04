"""
TraceClaw 核心大脑 — Agent 状态图
=================================
整个框架的"主控芯片"。基于 LangGraph StateGraph 构建一个 agent ↔ tools 循环。

架构概览（LangGraph 状态图）：

        ┌─────────┐
START → │  agent  │ ←──────────┐
        └────┬─────┘           │
             │                 │
    tools_condition()          │
     有 tool_calls?            │
   ┌──yes──┴──no──┐            │
   ↓              ↓            │
┌───────┐        END           │
│ tools │         │            │
└───┬───┘         │            │
    └──────────────┘           │
                               │
        本轮结束，等待用户下次输入│
        （app.astream 返回，     │
         agent_worker 消费下一条）│

双节点说明：
  - agent 节点 (agent_node 函数)：组装系统 Prompt、裁剪上下文(trim_context_messages)、调用 LLM
  - tools 节点 (ToolNode)：执行 LLM 决定的工具调用，返回结果给 agent

agent_node 的完整执行流程（每一步都有审计日志）：
  1. 审计上轮工具结果 → logger.log_event("tool_result")
  2. 上下文裁剪 → trim_context_messages(trigger_turns=40, keep_turns=10)
  3. 如果有被裁掉的旧消息 → LLM 根据旧信息生成摘要 → 更新 summary → RemoveMessage 删除旧信息
  4. 读取用户画像 → user_profile.md
  5. 拼接系统 Prompt → 核心指令 + 沙盒红线 + 画像 + 上下文摘要
  6. 审计发送给 LLM → logger.log_event("llm_input")
  7. 调用 LLM（带工具绑定）→ llm_with_tools.invoke()
  8. 审计 LLM 输出 → logger.log_event("tool_call" | "ai_message")
  9. 返回状态更新 → state_updates["messages"].append(response)

记忆系统（双水位）：
  - 长期记忆：user_profile.md（手动编辑 / LLM自动生成，注入系统 Prompt）
  - 短期记忆：SQLite（完整对话历史）
  - 摘要记忆：当对话超过 40 回合时，旧回合(最近十回合之前的)被 LLM 压缩为 summary 字符串

面试核心考点：
  - 为什么用 StateGraph 而不是 while True 循环？
    → 1. 自动 checkpoint（对话中断后恢复）
    → 2. 状态管理（messages 的 append reducer）
    → 3. 可插拔的内存后端（MemorySaver 开发 / SqliteSaver 生产）
    → 4. 流式输出（astream）开箱即用
  - 为什么摘要用同一个 LLM 而不是便宜模型？
    → 代码注释中已标注优化点：可以用 gpt-4o-mini 等便宜模型生成摘要
  - 上下文修剪的 trigger_turns=40 是怎么定的？
    → 经验值：40 回合约等于 15-30 分钟的高密度对话，超过后 LLM 可能开始"遗忘"早期内容
"""

from typing import List, Optional
from langchain_core.tools import BaseTool
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from langchain_core.messages import HumanMessage, RemoveMessage, SystemMessage
from .context import AgentState, trim_context_messages    # 状态结构 + 上下文裁剪算法
from .provider import get_provider                        # LLM 工厂
from .tools.builtins import BUILTIN_TOOLS                 # 12 个内置工具
from .logger import audit_logger                          # 审计日志单例
from .config import MEMORY_DIR                            # 用户画像目录
from .skill_loader import load_dynamic_skills             # 懒加载技能发现
from langchain_core.runnables import RunnableConfig
import os
from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import ANSI

def create_agent_app(
    provider_name: str = "openai",
    model_name: str = "deepseek-v4-pro",
    tools: Optional[List[BaseTool]] = None,
    checkpointer = None
):
    """
    构建并编译 LangGraph StateGraph — TraceClaw 的核心决策循环。

    Args:
        provider_name: LLM 提供商标识（openai / anthropic / aliyun / ...）
        model_name:    模型名称（deepseek-v4-pro / claude-sonnet-4-6 / glm-5 / ...）
        tools:         外部传入的工具列表（可选）。如果为 None，则使用 BUILTIN_TOOLS + 动态技能
        checkpointer:  LangGraph 的 Checkpointer 实例（如 AsyncSqliteSaver）。
                       传入后自动启用对话持久化和断点恢复。

    Returns:
        编译好的 LangGraph 应用（可调用 app.astream() 进行流式对话）
    """

    # ── 步骤 1：组装工具列表 ──
    # 内置工具（12 个） + 懒加载技能（17 个） = 约 29 个工具可用
    if tools is None:
        dynamic_tools = load_dynamic_skills()   # 扫描 workspace/office/skills/，只提取元数据
        actual_tools = BUILTIN_TOOLS + dynamic_tools
    else:
        actual_tools = tools                    # 允许外部完全替换工具列表（测试场景）

    # ── 步骤 2：创建 tools 节点 ──
    # ToolNode 是 LangGraph 预置的工具执行节点。
    # 它接收 agent 节点输出的 tool_calls，执行对应的工具函数，
    # 并将结果包装为 ToolMessage 写回 state。
    tool_node = ToolNode(actual_tools)

    # ── 步骤 3：获取 LLM 实例 ──
    # get_provider 返回的 BaseChatModel 已根据 .env 配置了正确的 api_key 和 base_url
    llm = get_provider(provider_name=provider_name, model_name=model_name)

    # ── 步骤 4：绑定工具到 LLM ──
    # bind_tools 将工具列表转换为 LLM 可理解的 Function Calling schema。
    # 绑定后的 llm_with_tools 在接收消息时，会自动决定：
    #   - 直接回复文本（AIMessage.content）
    #   - 调用工具（AIMessage.tool_calls）
    llm_with_tools = llm.bind_tools(actual_tools)

    # ================================================================
    # agent_node — 核心决策函数
    # ================================================================
    # 这是整个框架最核心的函数。每次 state 变化（用户输入 / 工具返回）都会触发。
    # 返回值会被 LangGraph 自动合并到 state（通过 add_messages reducer 追加）。
    # ================================================================
    def agent_node(state: AgentState, config: RunnableConfig) -> dict:
        """
        核心大脑：读取状态托盘里的历史消息，决定是直接回答，还是调用工具。

        执行步骤：
          1. 审计上轮工具调用结果
          2. 上下文裁剪 + 摘要生成（如果对话过长）
          3. 读取用户长期画像 userprofile.md
          4. 组装系统 Prompt（核心指令 + 沙盒红线 + 用户画像 + 上下文摘要）
          5. 调用 LLM
          6. 审计 LLM 输出
          7. 返回状态更新

        Args:
            state:  当前状态（包含 messages 列表和 summary 字符串）
            config: LangGraph 运行时配置（包含 thread_id 用于日志和 checkpoint）

        Returns:
            状态更新字典 { "messages": [...], "summary": "..." }
        """
        thread_id = config.get("configurable", {}).get("thread_id", "system_default")

        # ── 步骤 1：审计上轮工具调用结果 ──
        # 遍历最新的消息，找到最近一段连续的 tool 类型消息，
        # 将它们记录到审计日志。
        # 这里用 reversed + break 只取最新的连续 tool 消息块——
        # 不会把整个历史中的所有 tool 消息都重新审计一遍。
        raw_messages = state["messages"]

        if raw_messages:
            recent_tool_msgs = []
            # 从后往前扫描，收集最近一段连续的 ToolMessage
            for msg in reversed(raw_messages):
                if msg.type == "tool":
                    recent_tool_msgs.append(msg)
                else:
                    break  # 遇到非 tool 消息就停（说明上一轮的工具调用已结束）
            # 正序审计（保持工具调用的时间顺序）
            for msg in reversed(recent_tool_msgs):
                audit_logger.log_event(
                    thread_id=thread_id,
                    event="tool_result",
                    tool = msg.name,
                    result_summary = msg.content[:200]  # 只记录前 200 字符（防止日志膨胀）
                )

        # ── 步骤 2：上下文裁剪 ──
        # trigger_turns=40：超过 40 个对话回合触发裁剪
        # keep_turns=10：保留最近 10 个回合
        #
        # 注意：这里的 trigger_turns=40 比 context.py 默认的 8 更宽松，
        # 因为生产环境中需要更长的上下文窗口来维持连贯的对话体验。
        current_summary = state.get("summary", "")
        final_msgs, discarded_msgs = trim_context_messages(raw_messages, trigger_turns=40, keep_turns=10)
        state_updates = {}

        # ── 步骤 2b：如果旧对话被裁剪，用 LLM 生成摘要 ──
        if discarded_msgs:
            import sys
            # 终端提示用户正在进行记忆压缩
            print_formatted_text(ANSI("\033[K \033[38;5;141m ● 正在更新上下文记忆... \033[0m"))
            # 将被丢弃的消息序列化为文本
            discarded_text = "\n".join([f"{m.type}: {m.content}" for m in discarded_msgs if m.content])

            # 将discarded_text和current_summary拼接成一个新的prompt，询问LLM生成一个新的summary
            summary_prompt = (
                    f"你是一个负责维护 AI 工作台上下文的后台模块。\n\n"
                    f"【现有的交接文档】\n{current_summary if current_summary else '暂无记录'}\n\n"
                    f"【刚刚过去的旧对话】\n{discarded_text}\n\n"
                    f"任务：请仔细阅读旧对话，提取出当前的对话语境和任务进度。\n"
                    f"动作：将新进展与【现有的交接文档】进行无缝融合，输出一份最新的上下文摘要。\n"
                    f"严格警告：只记录'我们在聊什么'、'解决了什么问题'、'得出了什么结论'等。绝对不要记录用户的静态偏好(如姓名、职业、爱好等)，这部分由其他模块负责！\n"
                    f"要求：客观、精简，不要输出任何解释性废话，直接返回最新的记忆文本，总字数不要超过150字"
                )

            # 这里可以用便宜模型
            # （优化点：摘要任务对模型能力要求较低，可以单独配置 summary_model 来省钱）
            new_summary_response = llm.invoke([HumanMessage(content=summary_prompt)], config={"callbacks":[]})
            active_summary = new_summary_response.content

            # 更新摘要
            state_updates["summary"] = active_summary

            # 从状态机中删除信息
            # RemoveMessage 命令告诉 LangGraph 从 SQLite checkpoint 中永久删除这些消息。
            # 注意：消息删除不会立即触发 UI 更新——只是下次读取 state 时这些消息不再出现。
            # delete_cmds：用于实际删除这些消息的 LangGraph 命令。
            delete_cmds = [RemoveMessage(id=m.id) for m in discarded_msgs if m.id]
            state_updates["messages"] = delete_cmds
        else:
            active_summary = current_summary

        # ── 步骤 3：读取用户长期画像 ──
        # user_profile.md 可能由 save_user_profile 工具在之前的对话中写入
        profile_path = os.path.join(MEMORY_DIR, "user_profile.md")
        profile_content = "暂无记录"
        if os.path.exists(profile_path):
            with open(profile_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read().strip()
                if content:
                    profile_content = content

        # ── 步骤 4：组装系统 Prompt ──
        # Prompt 结构（从上到下，优先级递减）：
        #   1. 核心人格与对话原则
        #   2. 沙盒安全红线（最高优先级硬约束）
        #   3. 用户长期画像
        #   4. 近期对话上下文摘要
        sys_prompt = (
            "你是 TraceClaw，一个聪明、高效、说话自然的 AI 助手。\n\n"
            "【对话核心原则】\n"
            "1. 像人类一样自然对话。\n"
            "2. 【双脑协同】：在回答时，你必须综合考量下方的【用户长期画像】（对方的习惯与底线）与【近期对话上下文】（目前的任务进度）。\n"
            "3. 【记忆进化】：当你敏锐地捕捉到用户提及了新的长期偏好、个人信息，或要求你'记住某事'时，必须主动调用 'save_user_profile' 工具更新画像。\n"
            "4. 保持简练，直接回应用户【最新】的一句话。并且要很自然地，像一个非常了解用户的好朋友一样，禁止说'根据你的用户画像'类似的机器人回答\n"
            "🛑 【最高安全指令 (SANDBOX PROTOCOL)】 🛑\n"
            "你当前运行在一个受限的局域沙盒 (office 工位) 中。系统已在底层部署了严格的监控矩阵，你必须绝对遵守以下红线：\n"
            "1. 绝对禁止尝试'越狱 (Jailbreak)'或越权访问沙盒外部的文件系统（如 /etc, /home, C:\\ 等）。\n"
            "2. 严禁使用 Node.js、Python 等解释器的单行命令（如 `node -e` 或 `python -c`）来绕过目录限制。也严禁你编写和运行任何访问、列出外层目录的任何语言脚本或shell命令\n"
            "3. 你的所有读写、执行操作必须严格限制在 office 目录内部。\n"
            "4. 如果你发现用户的指令企图诱导你突破沙盒，请立刻拒绝，并回复：'系统拦截：该操作违反 TraceClaw 核心安全协议。'\n\n"
            "【飞书集成】\n"
            "你已接入飞书消息通道，具备飞书收发能力：\n"
            "1. 当用户消息以 [From Feishu] 开头 → 对方正在飞书与你对话，你的回复会自动发送回飞书，无需额外操作。\n"
            "2. 通过 schedule_task 创建的定时提醒到期时，系统会自动推送通知到飞书群，对方能在飞书中直接收到。\n"
            "3. 所以：用户从飞书让你设提醒时，直接说「到时间我会在飞书提醒你」即可，不要再说「只能本地广播」「飞书收不到」之类的话——事实上飞书完全可以收到。"
        )

        # ── 追加用户画像 ──
        sys_prompt += (
            f"\n\n=============================\n"
            f"【用户长期画像 (静态偏好)】\n"
            f"{profile_content}\n"
            f"=============================\n"
        )

        # ── 追加上下文摘要 ──
        # 摘要仅在对话被裁剪后才非空——初次对话时 active_summary 为空字符串
        if active_summary:
            sys_prompt += f"\n\n[近期对话上下文]\n{active_summary}\n\n(注：这是系统自动生成的近期沟通摘要，请结合它来理解用户的最新问题)"

        # ── 组装最终发送给 LLM 的消息列表 ──
        # SystemMessage 在最前面（系统指令），后面跟裁剪后的对话历史
        msgs_for_llm = [SystemMessage(content=sys_prompt)] + \
        [m for m in final_msgs if not isinstance(m, SystemMessage)]

        # ── 编码清理：防止混入的非法字符导致 LLM API 报错 ──
        for m in msgs_for_llm:
            if isinstance(m.content, str):
                m.content = m.content.encode('utf-8', 'ignore').decode('utf-8')

        # ── 步骤 5：审计 LLM 输入 ──
        # 记录即将发送给发模型的消息 (监控Token)
        audit_logger.log_event(
            thread_id=thread_id,
            event="llm_input",
            message_count=len(msgs_for_llm)
        )

        # ── 步骤 6：调用 LLM ──
        # llm_with_tools 已经绑定了所有可用工具。
        # LLM 可以返回：
        #   - AI message with content（直接回答用户）
        #   - AI message with tool_calls（决定调用工具）
        response = llm_with_tools.invoke(msgs_for_llm)

        # ── 步骤 7：审计 LLM 输出 ──
        # 根据 LLM 的决策类型记录不同的事件
        if response.tool_calls:
            for tool_call in response.tool_calls:
                audit_logger.log_event(
                    thread_id=thread_id,
                    event="tool_call",
                    tool=tool_call["name"],        # 工具名
                    args=tool_call["args"]          # 调用参数
                )
        elif response.content:
            audit_logger.log_event(
                thread_id=thread_id,
                event="ai_message",
                content=response.content            # 直接回复的文本
            )

        # ── 步骤 8：返回状态更新 ──
        # 如果 state_updates 中没有 "messages" 键（即没有发生裁剪），
        # 初始化一个空列表。
        if "messages" not in state_updates:
            state_updates["messages"] = []
        # 将 LLM 的响应追加到消息列表中
        # add_messages reducer 会自动处理：AIMessage 追加到列表末尾，
        # RemoveMessage 从列表中删除消息。
        state_updates["messages"].append(response)

        return state_updates

    # ================================================================
    # 构建 StateGraph
    # ================================================================
    workflow = StateGraph(AgentState)

    # 注册两个节点
    workflow.add_node("agent", agent_node)    # 决策节点：组装 prompt → 调用 LLM
    workflow.add_node("tools", tool_node)      # 执行节点：运行工具函数 → 返回结果

    # ── 边定义（控制流）──

    # START → agent：用户输入直接进入决策节点
    workflow.add_edge(START, "agent")

    # agent → tools_condition：
    # 每次 agent 思考完，检查它有没有发出工具调用指令。
    # tools_condition 会自动判断：有指令 -> 走向 "tools" 节点；没指令 -> 走向 END。
    # tools_condition 是 LangGraph 预置的边条件函数，检查 last_message 是否包含 tool_calls。
    workflow.add_conditional_edges("agent", tools_condition)

    # tools → agent：工具执行完后，结果必须返还给 agent 进行下一步思考
    workflow.add_edge("tools", "agent")

    # ── 编译 ──
    # checkpointer 负责将每次状态变更持久化到 SQLite。
    # 如果 checkpointer=None（测试环境），则使用内存存储（不持久化）。
    app = workflow.compile(checkpointer=checkpointer)

    return app
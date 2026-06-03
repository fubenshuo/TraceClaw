"""
TraceClaw 消息总线
==================
整个系统的"脊梁骨"——所有输入都通过这个队列汇入 agent_worker。

架构角色：
  用户键盘输入 ──→ task_queue.put() ──→ agent_worker 消费
  心跳闹钟任务 ──→ task_queue.put() ──→ agent_worker 消费

设计决策：
  - 使用 asyncio.Queue 而非 threading.Queue：因为整个 runtime 跑在 asyncio event loop 上
  - 单队列串行消费：天然避免了多用户并发问题
  - 代价是无法并行处理两个用户请求——但对于个人 Agent 场景，这是正确的取舍
"""

import asyncio

# ============================================================
# 全局异步队列 — 系统唯一的输入入口
# ============================================================
# 所有"需要 Agent 处理的事情"都先放入此队列：
#   - entry/main.py 中的 user_input_loop 读取用户键盘输入后 put
#   - heartbeat.py 中的 pacemaker_loop 检测到定时任务到期后 put
# agent_worker 协程阻塞在 task_queue.get() 上，串行消费每一条消息。
# ============================================================
task_queue = asyncio.Queue()

async def emit_task(content: str):
    """
    向消息总线推入一条文本内容。
    这是一个便捷封装，等同于 task_queue.put(content)。
    主要用于心跳任务等需要在异步上下文中"触发一次 Agent 对话"的场景。
    """
    await task_queue.put(content)
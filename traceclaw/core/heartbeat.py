"""
TraceClaw 心脏起搏器 — 定时任务调度引擎
========================================
后台协程，每 N 秒轮询 tasks.json，将到期的定时任务推入消息总线。

核心流程（每 10 秒一次）：
  1. 检查 tasks.json 是否存在 → 不存在则跳过
  2. 获取 tasks_lock（与 builtins.py 的 CRUD 操作互斥）
  3. 解析 JSON，遍历所有任务
  4. target_time ≤ 当前时间？→ 记录为"待触发"
     - 如果是循环任务（repeat），计算下一次触发时间并写回
     - 如果 repeat_count 耗尽，不再续期（任务自然消亡）
  5. 将被触发和未触发的任务写回文件
  6. 释放锁
  7. 将触发任务的消息推入 task_queue → agent_worker 将收到并处理

并发安全设计：
  - 使用与 builtins.py 共享的 tasks_lock（threading.Lock）
  - 用户在对话中通过 schedule_task / delete_scheduled_task 操作 tasks.json 时，
    心跳协程可能恰好在同一时刻扫描——锁保证读写串行化
  - 采用"持有锁期间完成所有 JSON 解析和文件写入"的策略（事务性）

面试要点：
  - 为什么不用 APScheduler？→ 零外部依赖，10s 精度对提醒类任务已经足够
  - 循环任务的时间计算为什么手动实现？→ 避免引入 cron 解析库，内置逻辑够用
  - 为什么用 asyncio.sleep 而不是 time.sleep？→ 不阻塞 event loop，其他协程可以继续跑
"""

import os
import json
import asyncio
import calendar
from datetime import datetime, timedelta
from .config import TASKS_FILE       # tasks.json 的绝对路径
from .tools.builtins import tasks_lock  # 与 builtins CRUD 共享的线程锁
from . import feishu                  # 飞书通知

async def pacemaker_loop(task_queue: asyncio.Queue, check_interval: int = 10):
    """
    后台心脏起搏器协程（带并发锁和循环任务续期功能）

    这是一个无限循环的异步协程，在 agent_worker 旁边并发运行。
    每 check_interval 秒醒来一次，扫描 tasks.json 中是否有到期任务。

    Args:
        task_queue:      消息总线队列，触发的任务通过 queue.put() 推入
        check_interval:  轮询间隔（秒），默认 10 秒
                         间隔越短，定时精度越高，但磁盘 I/O 越频繁

    任务数据结构（tasks.json 中每个元素的字段）：
        {
            "id": "a1b2c3d4",                # 8 位 UUID 前缀，唯一标识
            "target_time": "2026-06-01 08:00:00",  # 触发时间
            "description": "提醒用户吃早餐",        # 任务描述
            "repeat": "daily",               # 可选：循环频率（hourly/daily/weekly/monthly）
            "repeat_count": 5                # 可选：剩余触发次数（None = 无限循环）
        }
    """
    while True:
        # ── 步骤 1：休眠等待 ──
        # asyncio.sleep 而非 time.sleep——不阻塞 event loop
        await asyncio.sleep(check_interval)

        # ── 步骤 2：文件不存在则跳过 ──
        # task.json 可能因为从未有人创建任务而不存在——这是正常情况
        if not os.path.exists(TASKS_FILE):
            continue

        now = datetime.now()
        pending_tasks = []     # 写回文件的任务（未触发 + 已续期的循环任务）
        triggered_tasks = []   # 本轮触发、需要推入队列的任务

        # ── 步骤 3：加锁，原子化地读取-修改-写入 tasks.json ──
        # 线程锁，防止多线程/多协程同时读写任务文件导致的竞争条件和数据损坏
        with tasks_lock:
            # ── 步骤 3a：读取 JSON ──
            try:
                with open(TASKS_FILE, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if not content:
                        continue        # 空文件（所有任务已被删除）→ 跳过
                    tasks = json.loads(content)
            except Exception:
                continue                # JSON 损坏或权限问题 → 容错跳过，下轮重试

            if not tasks:
                continue

            # ── 步骤 3b：遍历每个任务，判断是否触发 ──
            for t in tasks:
                try:
                    # 解析目标触发时间
                    target_dt = datetime.strptime(t["target_time"], "%Y-%m-%d %H:%M:%S")
                    if now >= target_dt:
                        # === 到期：记录为需要触发 ===
                        triggered_tasks.append(t)

                        # === 循环任务续期逻辑 ===
                        repeat_freq = t.get("repeat")
                        if repeat_freq:
                            repeat_count = t.get("repeat_count")

                            # repeat_count 处理：
                            #   None → 无限循环，不递减
                            #   ≤ 1  → 最后一次，不续期，任务自然消亡
                            #   > 1  → 递减 1
                            if repeat_count is not None:
                                if repeat_count <= 1:
                                    continue     # 次数耗尽，不续期
                                else:
                                    t["repeat_count"] = repeat_count - 1

                            # 计算下一次触发时间
                            if repeat_freq == "hourly":
                                next_dt = target_dt + timedelta(hours=1)
                            elif repeat_freq == "daily":
                                next_dt = target_dt + timedelta(days=1)
                            elif repeat_freq == "weekly":
                                next_dt = target_dt + timedelta(days=7)
                            elif repeat_freq == "monthly":
                                # 月度循环需要特殊处理：
                                #   1 月 31 日的月度任务 → 2 月 28/29 日（而非 2 月 31 日）
                                #   用 calendar.monthrange 获取目标月份的实际天数，
                                #   取 min(原始日期, 月末) 避免溢出
                                month = target_dt.month + 1
                                year = target_dt.year
                                if month > 12:
                                    month = 1
                                    year += 1
                                last_day = calendar.monthrange(year, month)[1]
                                day = min(target_dt.day, last_day)
                                next_dt = target_dt.replace(year=year, month=month, day=day)
                            else:
                                continue  # 未知的循环频率 → 跳过续期

                            # 更新任务的目标时间为下一次触发时间
                            t["target_time"] = next_dt.strftime("%Y-%m-%d %H:%M:%S")
                            pending_tasks.append(t)   # 续期后加入待写回列表
                    else:
                        # === 未到期：保留在待办列表中 ===
                        pending_tasks.append(t)
                except Exception:
                    # 单个任务解析失败不中断整体流程（容错）
                    pass

            # ── 步骤 3c：写回文件（仅在触发了任务时需要） ──
            # 将还没到触发时间的任务和续期后的循环任务写回文件，覆盖原有内容
            if triggered_tasks:
                try:
                    with open(TASKS_FILE, "w", encoding="utf-8") as f:
                        json.dump(pending_tasks, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass

        # ── 步骤 4：锁已释放，将触发的任务推入消息总线 ──
        # 注意这一步在锁外面——避免在持有文件锁时进行 asyncio 操作
        for t in triggered_tasks:
            system_msg = (
                f"【系统内部心跳触发】\n"
                f"你设定的定时任务已到期，请立即主动提醒用户或执行动作。\n"
                f"任务内容：{t['description']}"
            )
            # 推入队列后，agent_worker 会将其包装为 HumanMessage 送入 LangGraph
            await task_queue.put(system_msg)

            # ── 飞书推送通知 ──
            # 优先使用任务级别的 chat_id，fallback 到全局配置
            chat_id = t.get("feishu_chat_id") or os.getenv("FEISHU_NOTIFY_CHAT_ID", "")
            feishu_on = feishu.is_enabled()
            # 无条件诊断：确认变量状态
            feishu._status(
                f"Heartbeat: chat_id={'SET' if chat_id else 'EMPTY'} "
                f"feishu_enabled={feishu_on} "
                f"task={t['description'][:20]}",
                "info" if chat_id and feishu_on else "warn",
            )
            if chat_id and feishu_on:
                notify_msg = f"[Task Alert]\n{t['description']}"
                try:
                    ok = await feishu.send_to_chat(chat_id, notify_msg)
                    if ok:
                        feishu._status(f"Heartbeat sent OK", "ok")
                    else:
                        feishu._status(f"Heartbeat send failed (API error)", "error")
                except Exception as e:
                    feishu._status(f"Heartbeat send error: {e}", "error")
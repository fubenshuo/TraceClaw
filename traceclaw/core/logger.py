"""
TraceClaw 审计日志系统
======================
实现全局单例的 JSONL（JSON Lines）事件日志记录器。

架构模式：单例 + 生产者-消费者

  主线程（生产者）                    后台线程（消费者）
  log_event()  ──→  queue.Queue  ──→  _write_loop()  ──→  logs/<thread_id>.jsonl
                      (无界内存队列)      (守护线程)

设计精华：
  1. 非阻塞写入 — 主线程调用 log_event() 只是往内存队列塞一条，立刻返回
  2. 守护线程 — daemon=True，主进程退出时自动销毁，不阻塞关机
  3. atexit 保护 — 程序正常退出时，shutdown() 会把队列里积压的日志刷完
  4. 线程安全的文件名 — safe_id 过滤掉路径注入字符（如 ../ 等）

五种日志事件类型：
  - llm_input:     发送给 LLM 的消息（记录消息数量和 token 预估）
  - tool_call:     LLM 决定调用某个工具（记录工具名和参数）
  - tool_result:   工具执行完成（记录工具名和返回内容摘要）
  - ai_message:    LLM 的文本回复（记录完整内容）
  - system_action: 系统层面的动作（如上下文裁剪、心跳触发）

面试要点：
  - 为什么用队列而不是直接写文件？→ 解耦 I/O 延迟，主线程不等待磁盘写入
  - 为什么用守护线程？→ 用户 Ctrl+C 退出时不卡住
  - 单例模式的线程安全实现？→ __new__ + threading.Lock 双重检查
"""

import os
import json
import threading
import queue
import atexit
from datetime import datetime, timezone

# ============================================================
# JSONLEventLogger — 全局审计日志记录器
# ============================================================
# 内存队列 + 守护线程
class JSONLEventLogger:
    # ── 单例模式：全局只有一个 logger 实例 ──
    # _instance 存储唯一实例
    # _lock 保证多线程环境下的创建安全（双重检查锁定的简化版）
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, log_dir: str = "logs"):
        """
        单例构造器 — 保证整个进程只有一个 JSONLEventLogger 实例。

        __new__ 在 __init__ 之前被调用。这里用 threading.Lock 保护，
        防止两个线程同时通过 _instance is None 检查，各自创建一个实例。
        """
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init_logger(log_dir)
            return cls._instance

    def _init_logger(self, log_dir: str):
        """
        初始化日志系统（仅在首次创建实例时调用一次）。

        步骤：
          1. 创建日志输出目录
          2. 创建无界内存队列（生产者-消费者缓冲区）
          3. 启动后台守护线程，死循环消费队列中的日志条目
          4. 注册 atexit 回调，确保程序退出前刷完队列中的残留日志
        """
        self.log_dir = log_dir
        os.makedirs(self.log_dir, exist_ok=True)

        # 无界内存队列，用于缓冲日志事件
        self.log_queue = queue.Queue()

        # daemon=True 意味着主线程退出时此线程自动终止，不会阻止进程关闭
        self.worker_thread = threading.Thread(target=self._write_loop, daemon=True)
        self.worker_thread.start()

        # 确保程序被关闭时，队列里的剩下日志能写完
        atexit.register(self.shutdown)

    # 后台线程的死循环：一直盯着队列，有日志就写，没日志就阻塞休眠
    def _write_loop(self):
        """
        后台消费者协程 — 死循环从队列取日志并写入磁盘。

        queue.Queue.get() 在队列为空时阻塞等待，不会空转消耗 CPU。
        None 是特殊哨兵值：当 shutdown() 往队列放入 None 时，
        循环收到此信号后 break 退出，线程自然结束。
        """
        while True:
            log_item = self.log_queue.get()   # 阻塞等待，直到有日志可写

            # ── 哨兵值检测：收到 None 则优雅退出 ──
            if log_item is None:
                self.log_queue.task_done()
                break

            try:
                # ── 安全的文件名生成 ──
                # thread_id 可能包含用户输入的任意字符，
                # 为防止路径注入（如 "../" 跳出 logs 目录），
                # 只保留字母、数字、连字符和下划线。
                thread_id = log_item.get("thread_id", "system")
                safe_id = "".join(c for c in thread_id if c.isalnum() or c in "-_") or "default"
                file_path = os.path.join(self.log_dir, f"{safe_id}.jsonl")

                # ── 追加写入 JSONL ──
                # ensure_ascii=False 保证中文不会变成 \uXXXX 转义序列
                with open(file_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(log_item, ensure_ascii=False) + "\n")
            except Exception as e:
                print(f"[Logger Error] 异步写日志失败: {e}")
            finally:
                self.log_queue.task_done()    # 标记此条日志已处理完毕

    def log_event(self, thread_id: str, event: str, **kwargs):
        """
        前台调用的埋点方法（非阻塞）。

        主线程/协程调用此方法时，日志条目被快速序列化后推入内存队列，
        不等待实际磁盘写入。后台线程异步完成 I/O。

        Args:
            thread_id: 会话标识（如 "local_geek_master"），用于生成日志文件名
            event:     事件类型（llm_input / tool_call / tool_result / ai_message / system_action）
            **kwargs:  事件相关的附加字段（如 tool=, args=, content= 等），
                       会被序列化到 JSON 中
        """
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        log_item = {
            "ts": now_utc,           # UTC 时间戳（ISO 8601 格式）
            "thread_id": thread_id,  # 会话 ID
            "event": event,          # 事件类型
            **kwargs                 # 事件载荷（工具名、参数、响应内容等）
        }

        self.log_queue.put(log_item)  # 非阻塞入队，立刻返回

    def shutdown(self):
        """
        优雅关闭：向队列发送哨兵值 None → 等待队列清空 → 后台线程退出。

        atexit 注册此方法后，Python 解释器退出时会自动调用。
        如果程序被 kill -9（SIGKILL）强杀，此方法无法执行——但守护线程的特性保证不会卡死主进程。
        """
        self.log_queue.put(None)      # 哨兵值，告诉 _write_loop "该退出了"
        self.log_queue.join()         # 等待队列中所有任务处理完毕

# ============================================================
# 全局单例实例 — 整个项目通过 import 此变量使用日志功能
# ============================================================
# 使用方式：
#   from traceclaw.core.logger import audit_logger
#   audit_logger.log_event(thread_id="xxx", event="tool_call", tool="get_time", args={})
# ============================================================
audit_logger = JSONLEventLogger()
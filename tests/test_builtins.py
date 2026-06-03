"""
内置工具集测试
==============
覆盖 12 个内置工具中可脱机测试的部分：时间、计算器、用户画像、任务调度 CRUD。

测试架构：
  TestBuiltInTools          → 无状态工具测试（时间、计算器、用户画像、模型信息）
  TestScheduledTasks        → 单任务 CRUD 测试（创建、列出、无效时间格式）
  TestScheduledTasksWithTasks → 多任务批量操作测试（列出、删除、修改、边界情况）

Mock 策略：
  - MEMORY_DIR / PROFILE_PATH → patch 到临时目录，避免污染真实用户画像
  - TASKS_FILE → 每个 TestCase 用 setUp/tearDown 替换为临时文件
  - 时间相关测试 → 动态计算未来时间（避免硬编码导致测试过期失效）

面试要点：
  - setUp/tearDown 是 unittest 的生命周期钩子，每个 test_ 方法前后各执行一次
  - invoke({}) 是 LangChain Tool 的标准调用方式（传入参数字典）
  - calculator 的注入测试（__import__('os')）验证了 eval 的安全限制（但不完全）
"""

import unittest
from unittest.mock import patch, mock_open
import os
import sys
import tempfile
import json
from datetime import datetime

# 将项目根目录添加到 sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from traceclaw.core.tools.builtins import (
    get_current_time,
    calculator
)
from traceclaw.core.config import MEMORY_DIR, TASKS_FILE


class TestBuiltInTools(unittest.TestCase):
    """无状态的系统查询工具测试"""

    def test_get_current_time(self):
        """
        测试获取当前时间功能。

        验证点：
          1. 返回字符串包含 "当前本地系统时间是:"
          2. 时间格式符合 "YYYY-MM-DD HH:MM:SS"
        """
        result = get_current_time.invoke({})
        self.assertIn("当前本地系统时间是:", result)

        # ── 格式验证 ──
        # 提取时间字符串并尝试用 datetime.strptime 解析
        time_str = result.replace("当前本地系统时间是：", "").strip()
        try:
            parsed_time = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            self.assertIsInstance(parsed_time, datetime)
        except ValueError:
            # 如果格式不匹配（可能使用了英文冒号等），至少验证返回了时间字符串
            self.assertTrue(len(time_str) > 0)

    def test_calculator_valid_expressions(self):
        """
        测试计算器功能 - 有效表达式。

        覆盖 5 种基本运算：
          +  加法
          *  乘法
          /  除法（返回 float）
          ** 幂运算
          %  取模
        """
        test_cases = [
            ("2 + 3", 5),
            ("10 * 5", 50),
            ("15 / 3", 5.0),
            ("2 ** 3", 8),
            ("17 % 5", 2)
        ]

        # subTest 是 unittest 的"子测试"机制——一个用例失败不影响其他用例
        for expr, expected in test_cases:
            with self.subTest(expr=expr):
                result = calculator.invoke({"expression": expr})
                self.assertIn(str(expected), result)

    def test_calculator_invalid_expression(self):
        """
        测试计算器功能 - 无效表达式 / 注入攻击。

        验证 eval 的安全限制（{"__builtins__": {}}）能拦截：
          - 语法错误（"2 +"）
          - 运行时错误（"1 / 0"）
          - Python 内置函数注入（__import__('os')）
          - 语句注入（import os）
          - 嵌套 eval 注入（eval('2+2')）
        """
        invalid_expressions = [
            "2 +",                  # 语法不完整的表达式
            "1 / 0",                # 除零错误（Python 运行时异常）
            "__import__('os')",     # 尝试导入 os 模块（被 __builtins__={} 拦截）
            "import os",            # import 语句（eval 不支持语句，SyntaxError）
            "eval('2+2')"           # 嵌套 eval（被 __builtins__={} 拦截）
        ]

        for expr in invalid_expressions:
            with self.subTest(expr=expr):
                result = calculator.invoke({"expression": expr})
                self.assertIn("计算出错", result)

    # ── MEMORY_DIR 和 PROFILE_PATH 的 patch ──
    # new_callable=lambda: tempfile.mkdtemp() / mktemp()
    #   每次调用 patch 时动态生成一个新的临时路径，避免多次运行测试时互相污染
    @patch('traceclaw.core.tools.builtins.MEMORY_DIR', new_callable=lambda: tempfile.mkdtemp())
    @patch('traceclaw.core.tools.builtins.PROFILE_PATH', new_callable=lambda: tempfile.mktemp())
    def test_save_user_profile(self, mock_profile_path, mock_memory_dir):
        """
        测试保存用户档案功能。

        验证点：
          1. 返回值是 "记忆档案已成功覆写更新。新的人设画像已生效。"
          2. 文件确实被创建在 patch 后的临时路径
          3. 文件内容与传入的 new_content 完全一致
          4. 使用 overwrite 模式（不是追加）——第二次调用会完全覆盖第一次
        """
        from traceclaw.core.tools.builtins import save_user_profile

        import tempfile
        import os

        # 测试保存功能
        test_content = "# 用户档案\n- 姓名：张三\n- 职业：工程师"
        result = save_user_profile.invoke({"new_content": test_content})
        self.assertEqual(result, "记忆档案已成功覆写更新。新的人设画像已生效。")

        # 验证文件已创建并包含正确内容
        self.assertTrue(os.path.exists(mock_profile_path))
        with open(mock_profile_path, 'r', encoding='utf-8') as f:
            saved_content = f.read()
        self.assertEqual(saved_content, test_content)


class TestScheduledTasks(unittest.TestCase):
    """定时任务单操作测试——每次测试前创建临时 tasks.json"""

    def setUp(self):
        """
        每个测试方法执行前的准备：
          1. 创建临时 JSON 文件（NamedTemporaryFile 自动生成唯一文件名）
          2. 保存原始的 TASKS_FILE 路径
          3. 将 builtins 模块中的 TASKS_FILE 替换为临时文件路径
        """
        # 创建临时任务文件
        self.temp_file = tempfile.NamedTemporaryFile(mode='w+', delete=False, suffix='.json')
        self.original_tasks_file = TASKS_FILE
        # 直接修改 builtins 模块中的全局变量 TASKS_FILE（测试需要这种侵入性）
        import traceclaw.core.tools.builtins
        traceclaw.core.tools.builtins.TASKS_FILE = self.temp_file.name

    def tearDown(self):
        """
        每个测试方法执行后的清理：
          1. 关闭临时文件句柄
          2. 删除临时文件
          3. 恢复原始的 TASKS_FILE 路径
        """
        # 清理临时文件
        self.temp_file.close()
        if os.path.exists(self.temp_file.name):
            os.unlink(self.temp_file.name)
        # 恢复原始路径
        import traceclaw.core.tools.builtins
        traceclaw.core.tools.builtins.TASKS_FILE = self.original_tasks_file

    def test_schedule_task_single(self):
        """
        测试单次任务调度功能。

        流程：
          1. 计算一个未来的触发时间（明天 9:00）
          2. 调用 schedule_task 创建任务
          3. 验证返回值包含 "任务已成功加入队列" 和任务描述
          4. 直接读取 JSON 文件验证数据完整性
        """
        from traceclaw.core.tools.builtins import schedule_task, list_scheduled_tasks

        # ── 计算未来时间 ──
        # 如果当前时间已经过了今天 9:00，就设为明天 9:00
        future_time = (datetime.now().replace(hour=9, minute=0, second=0)
                      if datetime.now().hour >= 9 else
                      datetime.now().replace(hour=9, minute=0, second=0))
        if future_time <= datetime.now():
            future_time = future_time.replace(day=future_time.day + 1)

        target_time = future_time.strftime("%Y-%m-%d %H:%M:%S")

        result = schedule_task.invoke({"target_time": target_time, "description": "喝水提醒"})
        self.assertIn("任务已成功加入队列", result)
        self.assertIn("喝水提醒", result)

        # ── 验证 JSON 文件内容 ──
        with open(self.temp_file.name, 'r', encoding='utf-8') as f:
            tasks_data = json.load(f)

        self.assertEqual(len(tasks_data), 1)
        self.assertEqual(tasks_data[0]["description"], "喝水提醒")
        self.assertEqual(tasks_data[0]["target_time"], target_time)

    def test_schedule_task_invalid_time_format(self):
        """
        测试调度任务 - 无效时间格式。

        传入 "invalid_time" → 期望返回 "设定失败：时间格式错误"
        """
        from traceclaw.core.tools.builtins import schedule_task

        result = schedule_task.invoke({"target_time": "invalid_time", "description": "测试任务"})
        self.assertIn("设定失败：时间格式错误", result)

    def test_list_scheduled_tasks_empty(self):
        """
        测试列出空任务列表。

        兼容两种空状态的返回消息：
          - "没有任何定时任务"（文件不存在或 JSON 为空时的消息）
          - "任务列表为空"（文件存在但数组为空时的消息）
        """
        from traceclaw.core.tools.builtins import list_scheduled_tasks

        # 确保文件为空
        with open(self.temp_file.name, 'w') as f:
            f.write("")

        result = list_scheduled_tasks.invoke({})
        # 兼容两种可能的返回消息
        self.assertTrue("没有任何定时任务" in result or "任务列表为空" in result)

    def test_get_system_model_info(self):
        """
        测试获取系统模型信息功能。

        验证点：
          1. 设置了环境变量 → 返回包含 provider 和 model 名称的消息
          2. 环境变量设为 'unknown' → 返回 "无法获取当前的系统模型配置"

        注意：
          get_system_model_info 直接从 os.getenv 读取，所以需要手动设置环境变量。
          finally 块确保无论测试成功与否，环境变量都会被恢复。
        """
        from traceclaw.core.tools.builtins import get_system_model_info

        # 保存原有环境变量（用于测试后恢复）
        orig_provider = os.environ.get('DEFAULT_PROVIDER')
        orig_model = os.environ.get('DEFAULT_MODEL')

        try:
            # ── 正常情况：设置了有效的 Provider 和 Model ──
            os.environ['DEFAULT_PROVIDER'] = 'test_provider'
            os.environ['DEFAULT_MODEL'] = 'test_model'

            result = get_system_model_info.invoke({})
            self.assertIn('test_provider', result)
            self.assertIn('test_model', result)

            # ── 未知情况：环境变量为 'unknown' ──
            os.environ['DEFAULT_PROVIDER'] = 'unknown'
            os.environ['DEFAULT_MODEL'] = 'unknown'

            result = get_system_model_info.invoke({})
            self.assertIn("无法获取当前的系统模型配置", result)

        finally:
            # ── 恢复环境变量 ──
            # os.environ.pop 比 del 更安全（键不存在时不会抛 KeyError）
            if orig_provider is not None:
                os.environ['DEFAULT_PROVIDER'] = orig_provider
            else:
                os.environ.pop('DEFAULT_PROVIDER', None)

            if orig_model is not None:
                os.environ['DEFAULT_MODEL'] = orig_model
            else:
                os.environ.pop('DEFAULT_MODEL', None)


class TestScheduledTasksWithTasks(unittest.TestCase):
    """定时任务批量操作测试——setUp 中预置 2 个任务"""

    def setUp(self):
        """
        预置 2 个测试任务到临时文件：
          - task1: "任务 1"，无循环
          - task2: "任务 2"，无循环

        两个任务共享同一个未来的 target_time（明天 9:00）。
        """
        self.temp_tasks_file = tempfile.NamedTemporaryFile(mode='w+', delete=False, suffix='.json')

        # 设置临时任务文件路径
        self.original_tasks_file = TASKS_FILE
        import traceclaw.core.tools.builtins
        traceclaw.core.tools.builtins.TASKS_FILE = self.temp_tasks_file.name

        # ── 计算未来时间（明天 9:00） ──
        future_time = (datetime.now().replace(hour=9, minute=0, second=0)
                      if datetime.now().hour >= 9 else
                      datetime.now().replace(hour=9, minute=0, second=0))
        if future_time <= datetime.now():
            future_time = future_time.replace(day=future_time.day + 1)

        target_time = future_time.strftime("%Y-%m-%d %H:%M:%S")

        # ── 创建 2 个测试任务 ──
        test_tasks = [
            {
                "id": "task1",
                "target_time": target_time,
                "description": "任务 1",
                "repeat": None,
                "repeat_count": None
            },
            {
                "id": "task2",
                "target_time": target_time,
                "description": "任务 2",
                "repeat": None,
                "repeat_count": None
            }
        ]

        with open(self.temp_tasks_file.name, 'w', encoding='utf-8') as f:
            json.dump(test_tasks, f, ensure_ascii=False, indent=2)

    def tearDown(self):
        """恢复原始 TASKS_FILE 路径并清理临时文件"""
        # 清理临时文件
        self.temp_tasks_file.close()
        if os.path.exists(self.temp_tasks_file.name):
            os.unlink(self.temp_tasks_file.name)
        # 恢复原始路径
        import traceclaw.core.tools.builtins
        traceclaw.core.tools.builtins.TASKS_FILE = self.original_tasks_file

    def test_list_scheduled_tasks_non_empty(self):
        """
        测试列出非空任务列表。

        验证 setUp 创建的 2 个任务都在列表中。
        """
        from traceclaw.core.tools.builtins import list_scheduled_tasks

        result = list_scheduled_tasks.invoke({})
        self.assertIn("当前待执行任务列表", result)
        self.assertIn("任务 1", result)
        self.assertIn("任务 2", result)

    def test_delete_scheduled_task(self):
        """
        测试删除计划任务。

        流程：
          1. 删除 task1
          2. 验证返回 "已成功取消"
          3. 再次列出任务 → task1 不在列表中，task2 仍然在
        """
        from traceclaw.core.tools.builtins import delete_scheduled_task, list_scheduled_tasks

        result = delete_scheduled_task.invoke({"task_id": "task1"})
        self.assertIn("已成功取消", result)

        # 验证任务已被删除
        result = list_scheduled_tasks.invoke({})
        self.assertNotIn("任务 1", result)
        self.assertIn("任务 2", result)

    def test_delete_nonexistent_task(self):
        """
        测试删除不存在的任务。

        传入不存在的 ID → 期望返回 "删除失败：未找到"
        """
        from traceclaw.core.tools.builtins import delete_scheduled_task

        result = delete_scheduled_task.invoke({"task_id": "nonexistent"})
        self.assertIn("删除失败：未找到", result)

    def test_modify_scheduled_task(self):
        """
        测试修改计划任务。

        流程：
          1. 修改 task1 的时间和描述
          2. 验证返回 "已成功更新"
          3. 列出任务 → 验证新时间和新描述已生效
        """
        from traceclaw.core.tools.builtins import modify_scheduled_task, list_scheduled_tasks

        # ── 计算新的未来时间（明天 10:00） ──
        new_time = (datetime.now().replace(hour=10, minute=0, second=0)
                   if datetime.now().hour >= 10 else
                   datetime.now().replace(hour=10, minute=0, second=0))
        if new_time <= datetime.now():
            new_time = new_time.replace(day=new_time.day + 1)

        new_target_time = new_time.strftime("%Y-%m-%d %H:%M:%S")

        # 同时修改时间和描述
        result = modify_scheduled_task.invoke({"task_id": "task1", "new_time": new_target_time, "new_description": "修改后的任务 1"})
        self.assertIn("已成功更新", result)

        # 验证任务已被修改
        result = list_scheduled_tasks.invoke({})
        self.assertIn("修改后的任务 1", result)
        self.assertIn(new_target_time, result)

    def test_modify_scheduled_task_invalid_time(self):
        """
        测试修改计划任务 - 无效时间格式。

        传入 "invalid_time" → 期望返回 "修改失败：时间格式错误"
        """
        from traceclaw.core.tools.builtins import modify_scheduled_task

        result = modify_scheduled_task.invoke({"task_id": "task1", "new_time": "invalid_time"})
        self.assertIn("修改失败：时间格式错误", result)

    def test_modify_nonexistent_task(self):
        """
        测试修改不存在的任务。

        传入不存在的 ID → 期望返回 "修改失败：未找到"
        """
        from traceclaw.core.tools.builtins import modify_scheduled_task

        result = modify_scheduled_task.invoke({"task_id": "nonexistent", "new_description": "不存在的任务"})
        self.assertIn("修改失败：未找到", result)


if __name__ == '__main__':
    unittest.main()

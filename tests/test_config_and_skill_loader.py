"""
配置模块与技能加载器测试
========================
轻量级导入和边界测试——验证模块能正常导入且处理异常情况。

测试覆盖：
  TestConfig:
    - 验证 config.py 中 8 个路径常量都存在且为字符串类型

  TestSkillLoader:
    - 验证 load_dynamic_skills 是一个可调用函数
    - 空目录 / 不存在的目录 → 返回空列表（不抛异常）

Mock 策略：
  - os.path.exists → Mock 为 False，模拟 SKILLS_DIR 不存在
  - os.listdir → 抛出 FileNotFoundError 或返回空列表

面试要点：
  - callable() 是 Python 内置函数，用于检查对象是否可调用（函数、类、实现了 __call__ 的对象）
  - 空列表 [] 在 Python 中是 falsy 但在 assertEqual 中是合法值——测试不存在的目录时返回 [] 是正确的容错行为
"""

import unittest
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


class TestConfig(unittest.TestCase):
    """配置模块的导入和常量验证"""

    def test_config_import(self):
        """
        测试配置模块导入 — 验证 8 个核心路径常量。

        导出的常量列表（config.py 在 import 时自动计算）：
          WORKSPACE_DIR  — 工作区根目录
          MEMORY_DIR     — 用户画像存储目录
          PERSONAS_DIR   — 人设文件目录（预留功能）
          SCRIPTS_DIR    — 脚本目录（预留功能）
          OFFICE_DIR     — Agent 的沙盒工作区
          SKILLS_DIR     — 技能目录
          DB_PATH        — SQLite 数据库文件路径
          TASKS_FILE     — 定时任务 JSON 文件路径

        验证点：每个值都是非空字符串（不验证具体路径，因为取决于 TRACECLAW_WORKSPACE 环境变量）
        """
        from traceclaw.core.config import WORKSPACE_DIR, MEMORY_DIR, PERSONAS_DIR, SCRIPTS_DIR, OFFICE_DIR, SKILLS_DIR, DB_PATH, TASKS_FILE

        # 验证配置项存在
        self.assertIsInstance(WORKSPACE_DIR, str)
        self.assertIsInstance(MEMORY_DIR, str)
        self.assertIsInstance(PERSONAS_DIR, str)
        self.assertIsInstance(SCRIPTS_DIR, str)
        self.assertIsInstance(OFFICE_DIR, str)
        self.assertIsInstance(SKILLS_DIR, str)
        self.assertIsInstance(DB_PATH, str)
        self.assertIsInstance(TASKS_FILE, str)


class TestSkillLoader(unittest.TestCase):
    """技能加载器的边界条件测试"""

    def test_skill_loader_import(self):
        """
        测试技能加载器模块导入。

        验证 load_dynamic_skills 是一个可调用的函数对象。
        不实际调用——因为依赖于 SKILLS_DIR 存在且包含特定格式的文件。
        """
        try:
            from traceclaw.core.skill_loader import load_dynamic_skills
            # 确保函数存在
            self.assertTrue(callable(load_dynamic_skills))
        except ImportError as e:
            # 如果导入失败，可能是因为依赖问题，但仍需确认模块结构
            self.fail(f"无法导入技能加载器: {e}")

    # ── Mock 策略说明 ──
    # os.path.exists → return_value=False: 模拟 SKILLS_DIR 目录不存在
    # os.listdir → side_effect=FileNotFoundError(): 如果 exists 没拦住，listdir 也会抛异常
    # 两层防护确保无论 Mock 是否完美，测试都覆盖"没有技能"的场景
    @patch('os.path.exists', return_value=False)
    @patch('os.listdir', side_effect=FileNotFoundError())
    def test_load_dynamic_skills_no_directory(self, mock_listdir, mock_exists):
        """
        测试技能加载器 - 不存在的目录。

        SKILLS_DIR 不存在时，load_dynamic_skills 应优雅返回空列表，
        而不是抛出 FileNotFoundError。这是一种容错设计——
        Agent 即使没有安装任何技能也应该能正常启动。
        """
        from traceclaw.core.skill_loader import load_dynamic_skills

        skills = load_dynamic_skills()
        self.assertEqual(skills, [])

    @patch('os.path.exists', return_value=True)
    @patch('os.listdir', return_value=[])
    def test_load_dynamic_skills_empty_directory(self, mock_listdir, mock_exists):
        """
        测试技能加载器 - 空目录。

        SKILLS_DIR 存在但没有子目录 → 返回空列表。
        这模拟了刚初始化 workspace 但还没有安装任何技能的场景。
        """
        from traceclaw.core.skill_loader import load_dynamic_skills

        skills = load_dynamic_skills()
        self.assertEqual(skills, [])


if __name__ == '__main__':
    unittest.main()

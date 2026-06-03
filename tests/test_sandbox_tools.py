"""
沙盒工具测试
============
测试 4 个沙盒工具 + _get_safe_path 防火墙的安全性和功能。

测试覆盖：
  _get_safe_path:
    - 正常路径拼接（验证与 os.path.join + abspath 等效）
    - 路径穿越攻击拦截（../../ → PermissionError）

  list_office_files:
    - 正常列出（Mock 文件系统）
    - 不存在的目录（返回 "目录不存在"）

  read_office_file:
    - 成功读取（Mock open 返回 "file content"）
    - 文件不存在（返回 "文件不存在"）

  write_office_file:
    - 成功写入（覆盖模式）
    - 无效 mode 参数（须返回错误信息）

  execute_office_shell:
    - 安全命令（Mock subprocess.run）
    - 危险命令拦截（5 条路径穿越命令逐一测试）

Mock 策略：
  - os.path.exists / os.listdir / os.path.isdir → Mock 文件系统操作
  - builtins.open → mock_open 不创建真实文件
  - subprocess.run → Mock 执行层，只验证命令是否通过了安全检查

面试要点：
  - _get_safe_path 为什么直接调用（不用 invoke）？→ 它是内部函数，不是 LangChain Tool
  - 危险命令测试验证了 5 层正则防线中的每一层都能独立拦截攻击
  - mock_open 是 unittest.mock 的标准文件 Mock 工具，不创建真实文件
"""

import unittest
from unittest.mock import patch, mock_open
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from traceclaw.core.tools.sandbox_tools import (
    list_office_files,
    read_office_file,
    write_office_file,
    execute_office_shell,
    _get_safe_path          # 内部函数，非 Tool——可以直接调用
)
from traceclaw.core.config import OFFICE_DIR


class TestSandboxTools(unittest.TestCase):
    """沙盒工具的单元测试"""

    def test_get_safe_path_normal(self):
        """
        测试正常路径连接。

        _get_safe_path('subdir/file.txt') 应等于：
          os.path.abspath(os.path.join(OFFICE_DIR, 'subdir/file.txt'))

        这验证了：
          1. 相对路径被正确拼接到 OFFICE_DIR 下
          2. abspath 正确解析了路径（拼接后的路径一定在 OFFICE_DIR 内）
        """
        # _get_safe_path 是内部函数，不受装饰器影响，可以直接调用
        # 注意：OFFICE_DIR 是模块级常量，patch 需要在导入前或使用正确的路径
        original_office_dir = OFFICE_DIR
        try:
            # 使用实际 OFFICE_DIR 测试
            result = _get_safe_path('subdir/file.txt')
            expected = os.path.abspath(os.path.join(OFFICE_DIR, 'subdir/file.txt'))
            self.assertEqual(result, expected)
        finally:
            pass

    def test_get_safe_path_traversal_attempt(self):
        """
        测试路径遍历攻击 — 最关键的沙盒安全性测试。

        '../../forbidden/file.txt' → abspath 解析后 =
          <磁盘根目录>\forbidden\file.txt

        因为 abspath 会消除 ..，最终的路径不以 OFFICE_DIR 开头 →
        PermissionError 被抛出。

        这是防范"路径穿越"攻击的核心防线。
        """
        with self.assertRaises(PermissionError):
            _get_safe_path('../../forbidden/file.txt')

    # ── list_office_files 测试 ──
    # @patch 的顺序：自下而上（mock_isdir 是最内层的参数）
    # os.path.exists → return_value=True（目录存在）
    # os.listdir → return_value=['file1.txt', 'subdir']
    # os.path.isdir → side_effect=lambda x: x.endswith('subdir')
    #   （以 'subdir' 结尾的返回 True=文件夹，否则返回 False=文件）
    @patch('traceclaw.core.tools.sandbox_tools.os.path.exists', return_value=True)
    @patch('traceclaw.core.tools.sandbox_tools.os.listdir', return_value=['file1.txt', 'subdir'])
    @patch('traceclaw.core.tools.sandbox_tools.os.path.isdir', side_effect=lambda x: x.endswith('subdir'))
    def test_list_office_files(self, mock_isdir, mock_listdir, mock_exists):
        """
        测试列出办公文件功能。

        Mock 返回 ['file1.txt', 'subdir']，其中：
          - file1.txt → 📄（文件图标）
          - subdir    → 📁（文件夹图标）

        验证点：
          1. exists 和 listdir 都被调用了一次
          2. 返回结果包含 📄 和 📁 图标
        """
        # 工具需要通过 .invoke() 调用
        result = list_office_files.invoke({"sub_dir": ""})

        # ── 验证 Mock 被正确调用 ──
        # 这确认了工具函数内部确实调用了 exists 和 listdir
        mock_exists.assert_called_once()
        mock_listdir.assert_called_once()

        # ── 检查返回结果包含预期的图标和文件名 ──
        self.assertIn("📄 file1.txt", result)
        self.assertIn("📁 subdir", result)

    @patch('traceclaw.core.tools.sandbox_tools.os.path.exists', return_value=False)
    def test_list_office_files_nonexistent_dir(self, mock_exists):
        """
        测试列出不存在目录的文件。

        当 sub_dir 指向不存在的目录 → 应返回 "目录不存在"。
        """
        result = list_office_files.invoke({"sub_dir": "nonexistent"})
        self.assertIn("目录不存在", result)

    # ── read_office_file 测试 ──
    # mock_open: 用 mock_open 替换内置的 open 函数，read_data 指定 "打开"后 read() 的返回值
    @patch('traceclaw.core.tools.sandbox_tools.os.path.exists', return_value=True)
    @patch('builtins.open', new_callable=mock_open, read_data="file content")
    def test_read_office_file_success(self, mock_file, mock_exists):
        """
        测试成功读取办公文件。

        Mock 一个包含 "file content" 的文件 → 应完整返回该内容。
        """
        result = read_office_file.invoke({"filepath": "test.txt"})
        self.assertEqual(result, "file content")
        mock_file.assert_called_once()

    @patch('traceclaw.core.tools.sandbox_tools.os.path.exists', return_value=False)
    def test_read_office_file_nonexistent(self, mock_exists):
        """
        测试读取不存在的办公文件。

        文件不存在 → 返回 "文件不存在"。
        """
        result = read_office_file.invoke({"filepath": "nonexistent.txt"})
        self.assertIn("文件不存在", result)

    # ── write_office_file 测试 ──
    @patch('builtins.open', new_callable=mock_open)
    @patch('os.makedirs')
    def test_write_office_file_success(self, mock_makedirs, mock_file):
        """
        测试成功写入办公文件（覆盖模式 mode='w'）。

        验证点：
          1. 返回包含 "成功以 覆盖/新建 模式写入文件"
          2. open 被调用了一次
          3. makedirs 被调用了一次（确保父目录存在）
        """
        result = write_office_file.invoke({"filepath": "test.txt", "content": "test content", "mode": "w"})
        self.assertIn("成功以 覆盖/新建 模式写入文件", result)
        mock_file.assert_called_once()
        mock_makedirs.assert_called_once()

    def test_write_office_file_invalid_mode(self):
        """
        测试写入办公文件 - 无效的 mode 参数。

        mode='x' 不在 [w, a] 的允许列表中 → 返回错误提示。
        这是为了防止 LLM 传入 r+/x 等非预期的文件操作模式。
        """
        result = write_office_file.invoke({"filepath": "test.txt", "content": "test content", "mode": "x"})
        self.assertIn("❌ 错误：mode 参数必须是", result)

    # ── execute_office_shell 测试 ──
    @patch('traceclaw.core.tools.sandbox_tools.subprocess.run')
    def test_execute_office_shell_safe_command(self, mock_subprocess):
        """
        测试执行安全的 shell 命令。

        Mock subprocess.run 返回成功结果（returncode=0, stdout="command output"）。
        验证命令 "ls" 通过了安全检查被正常执行。
        """
        # ── Mock subprocess 结果 ──
        mock_result = mock_subprocess.return_value
        mock_result.returncode = 0
        mock_result.stdout = "command output"
        mock_result.stderr = ""

        result = execute_office_shell.invoke({"command": "ls"})
        # 输出格式包含前缀空格和中文冒号 - 使用更宽松的匹配
        self.assertIn("ls", result)
        self.assertIn("command output", result)

    def test_execute_office_shell_dangerous_commands(self):
        r"""
        测试执行危险命令会被拦截 — 验证 5 层正则防线。

        5 条危险命令，对应 5 个正则杀招：
          cd ../        → 杀招 1: \.\.（路径穿越）
          cat /etc/passwd → 杀招 2: 绝对路径 /
          ls ~          → 杀招 3: 用户主目录 ~
          dir \         → 杀招 4: Windows 根目录 \
          type C:\...   → 杀招 5: Windows 盘符 C:

        每条命令都应该被正则匹配拦截，返回 "❌ 权限拒绝"。
        用 subTest 确保一条失败不影响其他条。
        """
        dangerous_commands = [
            "cd ../",                                            # 杀招1：路径穿越
            "cat /etc/passwd",                                   # 杀招2：Unix 绝对路径
            "ls ~",                                              # 杀招3：用户主目录
            "dir \\",                                            # 杀招4：Windows 根目录
            "type C:\\windows\\system32\\config\\sam"            # 杀招5：Windows 盘符
        ]

        for cmd in dangerous_commands:
            with self.subTest(cmd=cmd):
                result = execute_office_shell.invoke({"command": cmd})
                self.assertIn("❌ 权限拒绝", result)


if __name__ == '__main__':
    unittest.main()

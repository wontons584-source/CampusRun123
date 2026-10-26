"""测试包标记。

有 `__init__.py` 时 pytest 以 `tests.test_xxx` 导入测试模块，
`from tests.conftest import ...` 才能稳定复用到同一份 `conftest` 模块
（否则同一文件会被导入两份，`MemoryOrderRepo` 变成两个不同的类）。
"""

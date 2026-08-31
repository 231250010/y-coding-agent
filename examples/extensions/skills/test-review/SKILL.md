---
name: test-review
description: 修改 Python 代码后，定位并运行最小相关 pytest，再根据失败信息继续修复。
---

# Test review

1. 先读取 `pyproject.toml` 和相关测试，确认项目实际使用的测试入口。
2. 优先运行与修改文件直接相关的最小测试集合。
3. 最小测试通过后，再根据变更风险决定是否扩大到完整测试。
4. 不得隐藏失败、删除测试或放宽断言来制造通过结果。
5. 最终报告实际运行的命令、通过数量和仍未验证的范围。

需要复用包内入口时，先读取 `scripts/run_related_tests.py` 审查内容，再用
`run_skill_script` 执行；把 pytest 目标作为 `args` 传入。脚本执行始终需要用户确认。

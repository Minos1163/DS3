# 技能拷贝操作记录

## 操作目标
将当前仓库的 skills 文件夹内容拷贝到全局 OpenCode 技能目录（C:\Users\Huang\.opencode\skills）。

## 环境确认
- 源目录：项目根目录下的 skills/
- 目标目录：C:\Users\Huang\.opencode\skills
- 目标目录已存在（发现一个 skills 条目）

## 执行的命令
```bash
cp -r skills/* "C:\Users\Huang\.opencode\skills/"
```

## 结果
- 拷贝成功。
- 目标目录中新增了与源目录相同的内容：
  - algorithmic-art
  - brainstorming
  - brand-guidelines
  - canvas-design
  - dispatching-parallel-agents
  - doc-coauthoring
  - docx
  - executing-plans
  - finishing-a-development-branch
  - frontend-design
  - internal-comms
  - mcp-builder
  - pdf
  - pptx
  - requesting-code-review
  - skill-creator
  - slack-gif-creator
  - subagent-driven-development
  - systematic-debugging
  - test-driven-development
  - theme-factory
  - using-git-worktrees
  - using-superpowers
  - verification-before-completion
  - web-artifacts-builder
  - webapp-testing
  - writing-plans
  - writing-skills
  - xlsx

## 后续建议
- 如果你有重复技能名或需要覆盖，请告知是否需要强制覆盖。
- 可选：将此次操作添加到版本控制（只记录操作指令，不包含敏感内容）。

## 备注
- 操作在 Windows 环境下使用 `cp -r`，确保符号链接与文件结构被保留。
- 如果发生权限问题，建议以管理员权限运行或在 OpenCode 设置中重新加载技能目录。

## LSP 诊断结果（已捕获）
- 拷贝完成后检测到一些 LSP 错误，主要在 src/main.py（数据类型相关）和 src/trading/order_gateway.py，以及 tools/ 下的 wrapper 脚本（Module is not callable）。
- 这些错误不影响技能拷贝，如需修复请指示。
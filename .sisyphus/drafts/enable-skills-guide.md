# 如何在项目中启用新的 Skills（技能）——简明指南

## 概述

OpenCode 的技能加载机制：
- **全局技能目录**：`C:\Users\Huang\.opencode\skills`（存放所有可用技能）。
- **项目级配置**：项目根目录下的 `.opencode/opencode.json`（声明该项目启用的技能）。
- **生效规则**：只有同时存在于全局目录且在项目配置中显式启用的技能，才会在该项目中生效。

---

## 启用技能的步骤

### 1. 查看可用的技能

列出全局技能目录下的所有技能（在你的命令行运行）：
```powershell
# PowerShell
Get-ChildItem -Name "$env:USERPROFILE\.opencode\skills"

# 或 CMD
dir "%USERPROFILE%\.opencode\skills"
```

你会看到类似：
```
algorithmic-art
brand-guidelines
frontend-ui-ux
systematic-debugging
...
```

### 2. 决定要启用的技能

从列表中选择需要的技能。常用推荐（按功能分类）：
- **代码与工程类**：git-master, systematic-debugging, test-driven-development
- **前端/UI**：frontend-ui-ux
- **文档与写作**：writing-plans, doc-coauthoring
- **自动化与浏览器**：playwright, dev-browser
- **项目管理**：executing-plans, verification-before-completion
- **创意与设计**：brand-guidelines, canvas-design

### 3. 编辑项目配置

打开项目根目录的 `.opencode/opencode.json`，在 `"skills"` 对象中加入需要的技能并设置 `"enabled": true`。

示例配置：
```json
{
  "plugin": [
    "oh-my-opencode",
    "opencode-antigravity-auth@latest"
  ],
  "skills": {
    "git-master": { "enabled": true },
    "playwright": { "enabled": true },
    "dev-browser": { "enabled": true },
    "frontend-ui-ux": { "enabled": true },
    "systematic-debugging": { "enabled": true }
  },
  "mcp_servers": {
    "playwright": {
      "type": "playwright",
      "endpoint": "http://localhost:9222"
    }
  }
}
```

### 4. 验证配置生效

在 OpenCode 会话中使用 `/skills`（如果支持）或检查启动时的技能加载日志。
常见验证方式：
- 运行 `/git-master` 或 `/frontend-ui-ux` 确认指令可用。
- 查看启动输出中是否包含你新增的技能名。

---

## 常见问题与技巧

### Q: 技能名是否区分大小写？
- 推荐：与全局目录中的文件夹名完全一致（通常为小短横分隔：`frontend-ui-ux`）。

### Q: 是否可以覆盖全局技能的路径？
- 可以：在 `opencode.json` 的 `"skills"` 对象里用 `"path": "自定义路径"` 指定，但多数情况无需自定义。

### Q: 如果技能依赖 MCP 服务器？
- 确保 `"mcp_servers"` 中对应的服务配置正确（例如 playwright 需要配置 endpoint）。

### Q: 如何临时禁用一个技能？
- 将 `"enabled": false`，或从配置对象中暂时移除。

---

## 参考模板（可直接复制粘贴）

```json
{
  "skills": {
    "git-master": { "enabled": true },
    "playwright": { "enabled": true },
    "dev-browser": { "enabled": true },
    "frontend-ui-ux": { "enabled": true },
    "systematic-debugging": { "enabled": true },
    "writing-plans": { "enabled": true },
    "executing-plans": { "enabled": true }
  }
}
```

---

## 下一步操作建议

- 编辑 `.opencode/opencode.json` 并保存。
- 如果使用版本控制，提交修改：
  ```bash
  git add .opencode/opencode.json
  git commit -m "feat: enable additional skills for project"
  ```
- 重启 OpenCode 会话或让配置重新加载。
- 测试新技能：运行对应指令（如 `/frontend-ui-ux`）验证可用。

---

## 备注

- 每个项目可以有不同的技能清单，不影响其他项目。
- 如果全局技能目录新增了技能，需要重新加载或重启 OpenCode 才可见。
- 技能加载失败时，检查拼写、路径及 MCP 依赖是否完整。

本指南已保存为：`.sisyphus/drafts/enable-skills-guide.md`，供后续查阅。
# 项目中已配置的 Skills（技能）检查报告

## 检查目标
确认当前工程项目配置了哪些 skills，以及它们是否被正确加载到 OpenCode 中。

## 关键发现

### 1. .opencode/opencode.json 中的 skills 声明
```json
{
  "skills": {
    "git-master": { "enabled": true },
    "playwright": { "enabled": true },
    "dev-browser": { "enabled": true }
  }
}
```
- 这意味着当前项目仅在 OpenCode 配置中启用了 3 个技能（git-master、playwright、dev-browser）。
- 其他技能（即使存在于全局 skills 目录）未在此项目的配置里显式启用。

### 2. 技能文件位置
- 全局技能目录（已拷贝到）：C:\Users\Huang\.opencode\skills
  - 包含大量技能文件夹（algorithmic-art, brainstorming, brand-guidelines, ...）。
- 项目配置声明位于：.opencode/opencode.json（上述三项）。
- 文档说明：.opencode/README.md 提供了部署和修改指南。

### 3. 项目中与 skills 相关的其他引用
- skills.txt：项目根的文本文件，描述“编程AI & 交易AI 能力提升”目标与训练要点（非 OpenCode 配置，仅为说明文档）。
- scripts/generate_experiment_report.py：引用 skills.txt 用于生成报告（不影响 OpenCode 技能加载）。

## 分析结论

### 项目实际加载的技能
- git-master（启用）
- playwright（启用）
- dev-browser（启用）

### 未在项目中启用的全局技能
- 其余在 C:\Users\Huang\.opencode\skills 中的技能（如 algorithmic-art、brand-guidelines 等）**当前不会被该项目自动识别**，除非在 .opencode/opencode.json 中显式加入并设置 enabled: true。

## 推荐后续操作

### 选项 1: 启用更多技能
在 .opencode/opencode.json 的 "skills" 对象中加入所需技能，例如：
```json
{
  "skills": {
    "git-master": { "enabled": true },
    "playwright": { "enabled": true },
    "dev-browser": { "enabled": true },
    "frontend-ui-ux": { "enabled": true },
    "systematic-debugging": { "enabled": true }
  }
}
```

### 选项 2: 按需动态加载
- 使用 /load-skill <skill-name>（如果 OpenCode 支持运行时加载）动态加入技能。
- 或检查 OpenCode CLI 提供的技能管理命令。

### 选项 3: 验证当前技能是否生效
- 在 OpenCode 会话中尝试调用 /git-master 或 /playwright 确认是否可用。
- 检查 opencode skill list（如有）的输出。

## 权限与生效机制说明
- OpenCode 优先使用项目级的 .opencode/opencode.json 声明。
- 全局技能目录为默认位置，但需在项目配置里显式启用才会被加载。
- MCP 服务器与插件在独立字段配置，与 skills 字段分离。

## 下一步请求（请你选择）
- A: 我帮你把常用技能加入项目配置并 commit（请列出要启用的技能名）。
- B: 你自行修改 .opencode/opencode.json，我只记录操作步骤。
- C: 我生成一份“如何在项目中启用新技能”的简明指南（不修改文件）。
- D: 结束本次检查（当前报告已足够）。

请回复 A/B/C/D，我继续执行。
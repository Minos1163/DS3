# OpenCode 示例配置（.opencode）

此目录提供示例配置文件，便于将项目与 OpenCode / oh-my-opencode 集成。

使用说明：

1. 拷贝文件到用户配置目录（推荐）:

   - Linux/macOS: `~/.config/opencode/opencode.json` 和 `~/.config/opencode/oh-my-opencode.json`
   - Windows: `%APPDATA%\opencode\opencode.json`

2. 请勿将密钥或凭证写入这些示例文件。实际运行时请通过环境变量或 `opencode auth login` 完成认证。

3. 说明：
   - `opencode.json` 中声明了 `plugin`、`skills` 与 `mcp_servers` 的示例占位符。根据你的环境调整 `endpoint`。
   - `oh-my-opencode.json` 示例覆盖了部分 agent 的模型映射与 MCP 能力覆盖（仅示例）。

4. 部署 Playwright MCP：如果要自动化 OAuth 登录或浏览器流程，确保 Playwright MCP 可用并将 `mcp_servers.playwright.endpoint` 指向正确地址。

5. 如需帮助：运行 `opencode` 并使用 `/connect` 或 `opencode auth login` 来完成 provider 认证。

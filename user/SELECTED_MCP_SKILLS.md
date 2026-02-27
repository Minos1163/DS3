# Selected MCP Skills for Crypto Trading & Parameter Tuning

This document lists MCP-based skills repos suitable for cryptocurrency trading workflows and parameter tuning. It also includes quick-install steps to bring them into your user directory for quick experimentation.

Install base directory: D:/AIDCA/AI2/user/skills/

Recommended repositories to clone and explore:
- Microsoft/skills (Agent Skills marketplace and MCP integration)
- bobmatnyc/mcp-skillset (Dynamic RAG-powered MCP skills service)
- intellectronica/skillz (MCP server for loading skills)
- cablate/Agentic-MCP-Skill (Three-layer lazy-loading MCP client)
- srprasanna/mcp-skill-hub (Dynamic Skill Hub with MCP)
- GBSOSS/-mcp-to-skill-converter (MCP server -> Claude Skill converter)
- srprasanna/mcp-skill-hub (Dynamic Skill Hub with MCP)
- anthropics/skills (MCP/Skill framework and tooling) - optional for reference
- isdaniel/pgtuner_mcp (already used; can be extended as a “tuning skill” example)

Install commands (one-time, run in shell on your workstation):
- Microsoft Skills (CLI-based install):
  - git clone https://github.com/microsoft/skills.git D:/AIDCA/AI2/user/skills/microsoft-skills
  - cd D:/AIDCA/AI2/user/skills/microsoft-skills
  - npm install
  - npx skills add microsoft/skills

- MCP Skillset (dynamic skill discovery):
  - git clone https://github.com/bobmatnyc/mcp-skillset.git D:/AIDCA/AI2/user/skills/mcp-skillset
  - (Follow repository README for local server steps and how to mount skills)

- Skillz MCP server (skill loading):
  - git clone https://github.com/intellectronica/skillz.git D:/AIDCA/AI2/user/skills/skillz
  - (Follow README for running Skillz and loading SKILL.md files)

- Agentic-MCP-Skill:
  - git clone https://github.com/cablate/mcp-progressive-agentskill.git D:/AIDCA/AI2/user/skills/agentic-mcp-skill
  - (Review the repo for how to wrap MCPs into Claude-style skills)

- MCP Skill Hub (dynamic hub):
  - git clone https://github.com/srprasanna/mcp-skill-hub.git D:/AIDCA/AI2/user/skills/mcp-skill-hub
  - Follow the documentation to install and run the hub as an MCP server

- MCP converter (MCP -> Skill):
  - git clone https://github.com/GBSOSS/-mcp-to-skill-converter.git D:/AIDCA/AI2/user/skills/mcp-to-skill-converter
  - Use the converter to generate Claude Skills from MCP servers as needed

- Optional reference: Anthropics skills (reference for MCP design):
  - git clone https://github.com/anthropics/skills.git D:/AIDCA/AI2/user/skills/anthropics-skills

Notes and next steps
- Start with 2-3 MCP SKILL repos first to validate the workflow and ensure your MCP gateway recognizes the skills.
- Ensure you have proper Node.js, Python, and Docker environments as needed by each repo.
- If you want, I can automate the cloning into the user directory and create a master install script that fetches all of these in one go and logs results.

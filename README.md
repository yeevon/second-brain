# second-brain

A self-hosted personal knowledge system. Capture thoughts from Discord on any device, classify them automatically with an LLM, store them in a structured Obsidian vault, and query your knowledge via MCP.

## How it works

```init
Discord (any device)
      ↓
n8n on EC2 (webhook receiver + orchestrator)
      ↓
Gemini 3.5 Flash (Bouncer → Sorter)
      ↓
GitHub private vault repo (sync layer)
      ↓
Local vault (Obsidian Git auto-pull)
      ↓
MCP server → Gemini CLI (query your knowledge)
```

## Vault structure

```init
vault/
├── /People        contacts, relationships, context
├── /Projects      active work, goals, next actions
├── /Ideas         sparks, concepts, half-baked thoughts
├── /Learning      notes, summaries, references
├── /Admin         tasks, logistics, decisions
├── /_log/         traceability — one file per transaction
```

## Components

| Folder | Purpose |
|--------|---------|
| `discord/` | Bot setup and configuration |
| `ec2/` | Server setup scripts |
| `n8n/` | Workflow exports and setup guide |
| `prompts/` | Gemini prompts for Bouncer and Sorter |
| `mcp/` | Local MCP server configuration |
| `obsidian/` | Plugin config and vault setup |
| `docs/` | Step by step setup guides |

## Setup

Follow the guides in order:

1. [Discord bot setup](docs/01-discord.md)
2. [EC2 and n8n setup](docs/02-ec2-n8n.md)
3. [Gemini API config](docs/03-gemini.md)
4. [Vault and GitHub setup](docs/04-vault.md)
5. [MCP server setup](docs/05-mcp.md)
6. [Obsidian Git setup](docs/06-obsidian.md)

## Requirements

- Discord account with a private server
- AWS account (EC2 t3.small, Ubuntu 24.04)
- Gemini API key (Google AI Studio)
- GitHub account (one private repo for your vault)
- Obsidian installed locally
- Node.js 18+ on your local machine (for MCP server)

## Related

- [my-vault](~) — your private vault repo (create your own, do not fork)
- [project-memory](https://github.com/yeevon/project-memory) — per-project code memory that registers into this system

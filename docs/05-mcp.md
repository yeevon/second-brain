# 05 · MCP server setup

The MCP server runs on your local PC and gives Gemini CLI read access to your vault and any registered project memories.

## 1. Install Node.js

Requires Node.js 18+. Check with:

```bash
node --version
```

If not installed: https://nodejs.org

## 2. Install the MCP filesystem server

```bash
npm install -g @modelcontextprotocol/server-filesystem
```

## 3. Configure allowed paths

The MCP server needs to know which paths it can read. Create a config file:

```bash
mkdir -p ~/.config/second-brain
```

Create `~/.config/second-brain/mcp-paths.json`:

```json
{
  "vault": "/absolute/path/to/your/local/vault"
}
```

## 4. Configure Gemini CLI to use MCP

In your Gemini CLI config (typically `~/.gemini/config.json`):

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "npx",
      "args": [
        "@modelcontextprotocol/server-filesystem",
        "/absolute/path/to/your/local/vault"
      ]
    }
  }
}
```

## 5. Verify

Start Gemini CLI and ask:

```init
What folders exist in my vault?
```

It should list your vault folders via MCP.

Next: [Obsidian Git setup](06-obsidian.md)
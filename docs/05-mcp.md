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
  "vault": "/absolute/path/to/your/local/vault",
  "registry": "/absolute/path/to/your/local/vault/_registry/projects.md"
}
```

The MCP server reads the registry at startup and adds each registered project path automatically.

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

## 5. Add project paths dynamically

When a project registers via `/init-memory`, it appends to `_registry/projects.md`.
The MCP server reads this on each session start so new projects are available automatically.

To also give MCP access to project `.brain/` folders, add each path:

```json
{
  "mcpServers": {
    "second-brain": {
      "command": "npx",
      "args": [
        "@modelcontextprotocol/server-filesystem",
        "/path/to/vault",
        "/path/to/project-one/.brain",
        "/path/to/project-two/.brain"
      ]
    }
  }
}
```

> Tip: The `mcp/update-paths.sh` script rebuilds this config automatically from your registry file. Run it after registering a new project.

## 6. Verify

Start Gemini CLI and ask:

```init
What folders exist in my vault?
```

It should list your vault folders via MCP.

Next: [Obsidian Git setup](06-obsidian.md)
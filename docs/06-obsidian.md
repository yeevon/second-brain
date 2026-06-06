# 06 · Obsidian Git setup

## 1. Open your vault in Obsidian

1. Open Obsidian
2. Click **Open folder as vault**
3. Select your local vault folder (the one connected to your private GitHub repo)

## 2. Install the Obsidian Git plugin

1. Go to **Settings → Community plugins**
2. Turn off Safe mode if prompted
3. Click **Browse** → search for `Obsidian Git`
4. Install and enable it

## 3. Configure Obsidian Git

Go to **Settings → Obsidian Git**:

| Setting | Value |
|---------|-------|
| Vault backup interval | 0 (disable auto-push — n8n handles writes) |
| Auto pull interval | 2 (pull every 2 minutes) |
| Pull on startup | Yes |
| Merge on pull | Yes |
| Disable push | Yes (n8n writes to GitHub, Obsidian only reads) |

> Disable push is important. n8n is the only thing that should be committing to your vault repo. If Obsidian also pushes you will get merge conflicts.

## 4. Authenticate

Obsidian Git uses your system git credentials. Make sure your local git is authenticated with GitHub:

```bash
git config --global user.name "Your Name"
git config --global user.email "your@email.com"

# If using HTTPS (recommended)
git config --global credential.helper store
# Then do any git pull in the vault folder — it will prompt for credentials once
```

## 5. Verify

1. In n8n, manually trigger the test webhook with a sample message
2. Wait 2 minutes
3. Open Obsidian — the new note should appear automatically

## Full sync flow

```init
n8n writes .md → commits to GitHub private repo
      ↓ (within 2 minutes)
Obsidian Git pulls → file appears in local vault
      ↓ (instantly)
MCP server reads local vault → available to Gemini CLI
```

Setup complete. Your second brain is running.
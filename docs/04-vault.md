# 04 · Vault and GitHub setup

## 1. Create your private vault repo

1. Go to [github](https://github.com/new)
2. Name it whatever you want (e.g. `my-vault`)
3. Set to **Private**
4. Do not add a README or .gitignore — keep it empty

## 2. Create the vault folder structure locally

```bash
mkdir -p my-vault/{People,Projects,Ideas,Learning,Admin,_log}

# add gitkeep files so empty folders commit
touch my-vault/People/.gitkeep
touch my-vault/Projects/.gitkeep
touch my-vault/Ideas/.gitkeep
touch my-vault/Learning/.gitkeep
touch my-vault/Admin/.gitkeep
touch my-vault/_log/.gitkeep
```

## 3. Push to GitHub

```bash
cd my-vault
git init
git add .
git commit -m "init: vault structure"
git branch -M main
git remote add origin https://github.com/your-username/my-vault.git
git push -u origin main
```

## 4. Create a GitHub personal access token

n8n needs this to write files to your vault repo.

1. Go to [github access token](https://github.com/settings/tokens)
2. Click **Generate new token (classic)**
3. Name: `second-brain-n8n`
4. Expiration: No expiration (or set a reminder to rotate)
5. Scopes: check `repo` (full control of private repos)
6. Click **Generate token** → copy it immediately
7. Save as `GITHUB_TOKEN` in your `.env`

Also set:

```init
GITHUB_VAULT_REPO=your-username/my-vault
GITHUB_VAULT_BRANCH=main
```

## 5. Add token to n8n

In n8n:

1. Go to **Settings → Credentials**
2. Add a **GitHub** credential
3. Enter your personal access token

Next: [MCP server setup](05-mcp.md)
#!/bin/bash
# update-paths.sh
# Rebuilds Gemini CLI MCP config from your vault registry
# Run this after registering a new project with /init-memory

REGISTRY="$HOME/path/to/vault/_registry/projects.md"
GEMINI_CONFIG="$HOME/.gemini/config.json"
VAULT_PATH="$HOME/path/to/vault"

# Extract paths from registry markdown table
PATHS=("$VAULT_PATH")

while IFS='|' read -r _ _ path _; do
  path=$(echo "$path" | xargs)
  if [[ "$path" =~ ^\/ ]]; then
    PATHS+=("$path")
  fi
done < <(tail -n +4 "$REGISTRY")

# Build args array for config
ARGS="[]"
for p in "${PATHS[@]}"; do
  ARGS=$(echo "$ARGS" | jq --arg p "$p" '. + [$p]')
done

# Write config
cat > "$GEMINI_CONFIG" << EOF
{
  "mcpServers": {
    "second-brain": {
      "command": "npx",
      "args": $(echo '["@modelcontextprotocol/server-filesystem"]' | jq --argjson paths "$ARGS" '. + $paths')
    }
  }
}
EOF

echo "MCP config updated with ${#PATHS[@]} paths:"
for p in "${PATHS[@]}"; do
  echo "  $p"
done
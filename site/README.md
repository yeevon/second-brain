# Digital Jochi local reader

Further custom reader development is paused while Digital Jochi uses Obsidian directly with LLM ingestion and management. This implemented reader remains runnable. See the [project README](../README.md) for delivered work, the development and audit process, and current operational boundaries.

Local, read-only browser access to the Digital Jochi Obsidian vault. The root page always opens `80 Canvases/Digital Jochi.canvas`; selecting a file card or note reads its current Markdown directly from the configured vault.

## Requirements

- Node.js 22.12 or newer (verified with Node.js 24)
- npm 10 or newer
- A local Obsidian vault supplied through `DIGITAL_JOCHI_VAULT`, or available at the conventional sibling project location

## Install and run

From this `site` directory:

```powershell
npm ci
npm run dev
```

Open <http://127.0.0.1:4173>. The server binds only to the loopback address. A reload starts on Home with the authored home canvas and an empty search field.

To use a different or isolated vault:

```powershell
$env:DIGITAL_JOCHI_VAULT='C:\path\to\vault'
npm run dev
```

The home canvas can be overridden for an isolated error fixture with `DIGITAL_JOCHI_CANVAS`. Both values are server-side only; the API normalizes requested files, resolves symlinks/junctions, and rejects reads outside the configured vault.

## Verify and build

```powershell
npm test
npm run typecheck
npm run build
$env:NODE_ENV='production'
npm start
```

The build writes browser assets to `dist/` and server output to `dist-server/`. The reader never writes notes, canvas data, indexes, or viewport state to the vault. Hidden folders, `.obsidian`, `90 Templates`, and `99 Attachments/Implementation` are excluded from the note index. Markdown raw HTML is not executed.

## Supported in this release

- Real Markdown headings, paragraphs, bold text, lists, and safe links
- JSON Canvas file cards, authored positions/preset or hex colors, labeled and directed edges, pan, zoom, and fit-to-view
- Visible missing targets, invalid-canvas recovery, and unsupported-node diagnostics
- Wikilinks with aliases, vault-relative paths, spaces, unambiguous filenames, note Back, and unresolved-link recovery
- Case-insensitive title, summary, and body substring search with keyboard-accessible results
- Manual refresh of notes and the home canvas without changing the selected path, search, history, or canvas viewport
- One frontmatter tag filter, combined with search, with per-note metadata diagnostics
- Resolved incoming wikilinks as navigable backlinks
- An explicit **Open in Obsidian** action using the official absolute-path [`obsidian://open?path=…` URI](https://help.obsidian.md/Extending%2BObsidian/Obsidian%2BURI), plus visible exact-vault identity and a copyable relative-path fallback
- One coalesced refresh after the browser returns from another tab or app; no background polling or filesystem watcher

Obsidian remains the editor and canonical file store. The local reader is intentionally read-only.

## Deliberate limits

The reader supports a bounded frontmatter subset, not general YAML. Heading/block wikilink targets report an unsupported state. Canvas text, link, group and media nodes have diagnostics rather than full rendering; arbitrary board selection is not implemented. The current layout targets desktop screens with a minimum width of 1180px.

The test suite uses temporary synthetic vaults and local HTTP servers. It verifies parsing, link resolution, path boundaries, source-byte preservation, tag/search behavior, refresh ordering and failure isolation. Browser interactions and native Obsidian handoff require separate runtime verification; passing these tests alone does not prove them.

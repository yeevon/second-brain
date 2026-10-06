# Digital Jochi local reader

Local, read-only browser access to the Digital Jochi Obsidian vault. The root page always opens `80 Canvases/Digital Jochi.canvas`; selecting a file card or note reads its current Markdown directly from the configured vault.

## Requirements

- Node.js 22.12 or newer (verified with Node.js 24)
- npm 10 or newer
- A local Obsidian vault supplied through `DIGITAL_JOCHI_VAULT`, or available at the conventional sibling project location

## Install and run

From this `site` directory:

```powershell
npm install
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

Obsidian remains the editor and canonical file store. The local reader is intentionally read-only.

# Digital Jochi Reader

Local, read-only browser reader for a private Obsidian vault. It opens an authored home canvas, renders real Markdown notes, follows supported wikilinks, and searches note titles and content.

The Obsidian vault remains the private source of truth and is intentionally not copied into this public repository. The server reads a locally configured vault and binds only to `127.0.0.1`.

## Run

```powershell
cd site
npm install
npm run dev
```

Open <http://127.0.0.1:4173>.

See [site/README.md](site/README.md) for configuration, build, verification, and supported behavior.

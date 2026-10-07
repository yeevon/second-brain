# Digital Jochi — Second Brain

Digital Jochi is a personal second brain built around an Obsidian vault, with LLM-assisted capture and management. This repository contains its implemented local browser reader, synthetic fixtures, tests, and public development documentation. Personal notes, operational stories, skills, and implementation evidence live separately in the private workspace.

**Current direction:** Obsidian is the primary interface. Further custom reader development is paused while the native workflow is used and evaluated. The reader remains runnable and read-only; completed work is retained.

## What was created

### Local browser reader

The application opens an authored Home canvas alongside a searchable note list and Markdown reading pane. It reads files directly from a configured local vault.

- Canvas file cards preserve authored positions, colors, labels and directed connections, with pan, zoom and fit-to-view.
- Markdown reading supports ordinary formatting, supported wikilinks and aliases, note history, and visible missing or ambiguous target states.
- Search covers titles, summaries and note bodies. One frontmatter tag filter can be combined with search; unsupported metadata produces per-note diagnostics.
- Resolved incoming wikilinks appear as navigable backlinks.
- Manual refresh preserves reading context and canvas viewport. Returning from another tab or app triggers coalesced refresh work, with stale-content recovery and protection against older responses replacing newer navigation.
- **Open in Obsidian** targets the validated absolute note path, including when two vaults share a name. A visible relative path provides a copy fallback.
- Canvas failures are isolated so available notes can still be read and refreshed.

The stack is React, TypeScript, React Flow, React Markdown, Express and Vite. The server binds to `127.0.0.1`; file requests are constrained to the configured vault after path and symlink/junction resolution. Raw Markdown HTML is not executed. The reader does not write vault files.

### Native Obsidian workflow

The wider project also established the following in the private vault; these are documented outcomes, not additional features shipped by this application:

- A Home canvas and saved workspace, plus a native Bases dashboard of canonical project and story properties.
- A local Digital Jochi skill and ingestion protocol for finding existing notes, capturing sources, updating records, preserving authorship, and retrieving context.
- Bounded manual trials of capture/update/retrieval, daily cleanup and source-linked weekly reviews.
- A manual private Git backup checkpoint, independently checked against all 695 committed file hashes, with a backup runbook and recorded exclusions.

Daily cleanup and weekly reviews remain unscheduled. Automatic Git backups are disabled, and separate-folder recovery testing remains pending. Calendar and Drive integrations are future work. The project starts fresh; selected material is brought in as needed, with no planned bulk migration.

## How it was built

The work began with a Markdown/JSON Canvas vault structure and a small read-only reader. Features were delivered as numbered, independently verifiable stories rather than one large implementation request. Later slices added refresh, exact Obsidian handoff, backlinks and tag filtering.

Adversarial audits challenged the completion claims and exposed issues such as request ordering, tag interpretation, backlinks inside code, same-named vault targeting and canvas failure isolation. Separate repair stories captured those findings. Focused regression checks and a lighter follow-up audit verified the repairs.

After evaluating Obsidian's native Canvas, Bases and Workspaces, the project shifted its primary interface to Obsidian. The existing reader was preserved, and subsequent stories focused on ingestion, maintenance, evidence and backup rather than duplicating more native functionality.

As of the 2026-10-06 local project review, DJ-001–DJ-035 are recorded done in the private backlog. That milestone includes native configuration and operating procedures as well as code. It does not imply that the pending integrations, schedules or recovery test are complete.

## The validation loop

1. **Define the slice.** Write a concrete outcome, acceptance criteria, dependencies, exclusions and required proof before implementation.
2. **Record assignment.** A direct execution assignment changes the story to `approved`, with assignee, approval source and timestamps. A proposed story stays pending.
3. **Implement and retain evidence.** Save relevant before/after files, focused check results, actual runtime screenshots and file hashes. Map each criterion to its proof.
4. **Audit adversarially.** Try to disprove correctness and completeness: inspect failure paths, stale state, ambiguous inputs, boundary conditions and unsupported claims. A screenshot alone cannot establish nonvisual behavior.
5. **Repair through explicit stories.** Record actionable findings and ownership for review and assignment. Preserve the original evidence and decisions instead of rewriting failed history as success.
6. **Run a bounded follow-up.** Verify the fixes and relevant prior behavior. Close the work only when acceptance and proof pass; avoid automatic audit/repair loops. Record remaining issues as separate proposals.

Verified completion sets `done` and `completed_at`. Every record edit refreshes `updated_at`; original creation, approval and event timestamps are preserved. Implementation decisions record the choice, rationale, alternatives, affected criteria and evidence.

Native screenshots are visually inspected and retained with hashes and honest provenance. When capture or runtime access is unavailable, the story remains incomplete. Later successful proof is dated separately; missing historical evidence is never reconstructed and presented as an original observation.

## Practices used

- **One canonical store.** Markdown owns knowledge and task state; canvases arrange references and Bases displays existing properties. LLM updates use the same files and preserve user annotations.
- **Private data stays private.** Real vault content, credentials, local configuration and runtime screenshots are excluded from this public repository. Included fixtures are synthetic.
- **Behavior-focused tests.** Test observable results using isolated temporary vaults and local HTTP servers. Controlled promises exercise request races without fixed sleeps. Failure cases include traversal, escaping junctions, malformed metadata, ambiguous links and unavailable files.
- **Evidence proportional to the claim.** Use native UI proof for interactions, byte comparisons for preservation, and independent remote reads for backup. A successful push is distinct from a verified restore.
- **Bounded maintenance.** Cleanup checkpoints belong to an exact scope and run kind; synthetic trials cannot initialize ordinary coverage. Weekly reviews separate synthetic evidence from personal findings and preserve source links, review identity and user annotations.
- **Native features first.** Prefer existing Obsidian capabilities where they meet the need. Resume custom development only for a demonstrated gap and an explicit assignment.

## Run locally

Requires Node.js 22.12 or newer and npm 10 or newer. From the repository root, this example uses the included synthetic vault:

```powershell
cd site
npm ci
$env:DIGITAL_JOCHI_VAULT = (Resolve-Path './evidence-fixtures/vault').Path
npm run dev
```

Open <http://127.0.0.1:4173>. To read your own vault, set `DIGITAL_JOCHI_VAULT` to its absolute path before starting the server. See [the application README](site/README.md) for configuration, production startup and supported behavior.

## Verification

From `site/`:

```powershell
npm test
npm run build
```

Both commands include TypeScript checks. The automated suite covers reader logic and local API behavior; it does not replace browser or native Obsidian interaction checks.

For this publication, the 19 focused tests passed with no failures, skips or retries, and the production build passed. Vite reports an existing non-blocking large-bundle warning. Earlier implementation work included browser/native screenshot review and exact-file Obsidian handoff checks; those interactions were not replayed for this documentation and publication update. Private evidence is intentionally not linked or published here.

## Scope and remaining work

The reader implements a bounded subset of Markdown, frontmatter and JSON Canvas. It does not provide general board selection, full YAML parsing or full rendering of text, link, group and media canvas nodes. The layout currently targets desktop screens. Obsidian plugins do not automatically extend the reader's rendering.

The next operational milestone is recovery into a separate folder from the private checkpoint. Selective integrations and unattended routines follow only after their access, cadence and scope are defined. The private vault backup and this public application repository have separate histories and publication boundaries.

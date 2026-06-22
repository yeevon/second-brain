# Tech Debt Milestone TD-03: P1.5 Vault Auditability / Lossless Capture History

Addresses TD-P1.5-001 from `docs/TechDebt.md`.

TD-03 implements the immutable raw vault substrate so every filed capture has a lossless vault-visible raw artifact linked to the sanitized/classified note.

Today, the system only persists the sanitized/classified note in the vault. That means the original user input can be lost once the classifier, n8n workflow, or writer-service transforms it. TD-03 fixes that by writing a deterministic raw Markdown file under `00_raw/YYYY/MM/` and linking every sanitized note back to that raw file with hash metadata.

Tracked by GitHub #20.

---

## Scope

This milestone implements the raw vault substrate in the current capture-only / writer-service architecture.

TD-03 changes:

* writer-service request schema
* writer-service vault write flow
* sanitized note frontmatter
* vault auditability guarantees
* writer-service idempotency behavior
* tests around raw/sanitized linkage

TD-03 does **not** move LLM classification into writer-service and does **not** introduce a new pre-classification raw-intake pipeline.

---

## Key architecture decision

The correct long-term model is:

```text
capture received
raw artifact persisted
classification/transformation happens
sanitized note written
```

However, the current production architecture sends writer-service a post-classification request. The existing `/internal/notes/file` request already contains classification data.

TD-03 therefore implements the strongest correct substrate without a pipeline rewrite:

```text
capture-service / n8n performs classification
writer-service receives classification + raw_text
writer-service writes raw file first
writer-service writes sanitized note second
sanitized note links to raw file
```

So the TD-03 guarantee is:

```text
raw file is written before sanitized vault note generation
```

Not:

```text
raw file is written before LLM classification
```

Strict raw-before-classification requires a future architecture change: a raw-intake step or endpoint before n8n/classification. That is explicitly deferred out of TD-03.

This is intentional. The auditability gap is that the vault lacks the original input. TD-03 closes that gap now while keeping the current writer-service ownership model intact.

---

## SB-141 — Immutable raw vault substrate

**Source:** TD-P1.5-001

See [SB-141.md](SB-141.md) for the full spec.

---

## Decisions resolved in this milestone

* **Raw substrate timing:** raw-before-sanitized-note is implemented now; strict raw-before-classification is deferred.
* **Writer ownership:** writer-service remains the sole vault writer in capture-only / Docker mode.
* **API shape:** existing `FileNoteRequest` gains `raw_text`.
* **Sensitive-capture policy:** raw files may contain sensitive content. The vault is trusted private storage.
* **Raw path:** `00_raw/YYYY/MM/<capture_id>.md`.
* **Hash definition:** SHA-256 over the raw body string encoded as UTF-8, excluding frontmatter.
* **Attachment behavior:** attachment metadata is stored in raw Markdown; binary bytes are not stored in TD-03.
* **Idempotency:** same capture ID + same raw hash is safe retry; same capture ID + different hash is hard failure.
* **Git behavior:** when Git sync is enabled, raw file, sanitized note, and audit log are committed together.

---

## Do not implement in this milestone

* P2 or P3 items.
* UI changes or query tooling over raw captures.
* S3 attachment storage.
* Binary attachment storage.
* Encryption or redaction of raw vault files.
* A new pre-classification raw-intake endpoint.
* Moving LLM classification into writer-service.
* Refactoring n8n workflow ownership beyond passing `raw_text` through to writer-service.

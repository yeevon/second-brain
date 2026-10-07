import assert from 'node:assert/strict';
import { once } from 'node:events';
import { afterEach, beforeEach, test } from 'node:test';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createApi } from '../dist-server/app.js';
import { VaultAccessError, VaultReader } from '../dist-server/vault-reader.js';

let sandbox = '';
let vault = '';
let outside = '';

beforeEach(async () => {
  sandbox = await fs.mkdtemp(path.join(os.tmpdir(), 'digital-jochi-test-'));
  vault = path.join(sandbox, 'vault');
  outside = path.join(sandbox, 'outside.md');
  await fs.mkdir(path.join(vault, '00 Inbox'), { recursive: true });
  await fs.mkdir(path.join(vault, '90 Templates'), { recursive: true });
  await fs.mkdir(path.join(vault, '.obsidian'), { recursive: true });
  await fs.mkdir(path.join(vault, '99 Attachments', 'Implementation', 'DJ-001'), { recursive: true });
  await fs.mkdir(path.join(vault, '80 Canvases'), { recursive: true });
  await fs.writeFile(path.join(vault, 'Home.md'), '# Home\n\nWelcome.');
  await fs.writeFile(
    path.join(vault, '00 Inbox', 'Canvas Test Note.md'),
    '---\nsummary: "Synthetic summary"\n---\n\n# Canvas Test Note\n\nText with **bold** and [[Home|Return home]].',
  );
  await fs.writeFile(path.join(vault, '90 Templates', 'Hidden.md'), '# Hidden');
  await fs.writeFile(path.join(vault, '.obsidian', 'Hidden.md'), '# Hidden');
  await fs.writeFile(path.join(vault, '99 Attachments', 'Implementation', 'DJ-001', 'Hidden.md'), '# Hidden');
  await fs.writeFile(outside, '# Outside');
  await fs.writeFile(
    path.join(vault, '80 Canvases', 'Digital Jochi.canvas'),
    JSON.stringify({
      nodes: [
        { id: 'test', type: 'file', file: '00 Inbox/Canvas Test Note.md', x: 10, y: 20, width: 300, height: 200, color: '#ff0000' },
        { id: 'missing', type: 'file', file: '00 Inbox/Absent.md', x: 350, y: 20, width: 300, height: 200 },
      ],
      edges: [{ id: 'directed', fromNode: 'test', fromSide: 'bottom', toNode: 'missing', toSide: 'top', color: '5' }],
    }),
  );
});

afterEach(async () => {
  await fs.rm(sandbox, { recursive: true, force: true });
});

test('indexes eligible real notes and strips frontmatter from the body', async () => {
  const reader = new VaultReader(vault);
  const notes = await reader.listNotes();
  assert.deepEqual(notes.map((note) => note.path), ['00 Inbox/Canvas Test Note.md', 'Home.md']);
  assert.equal(notes[0].title, 'Canvas Test Note');
  assert.equal(notes[0].summary, 'Synthetic summary');
  assert.doesNotMatch(notes[0].body, /summary:/);
  assert.match(notes[0].body, /\*\*bold\*\*/);
});

test('rejects traversal, excluded path variants, excluded aliases, and escaping junctions', async () => {
  const reader = new VaultReader(vault);
  await assert.rejects(() => reader.readNote('../outside.md'), (error) => error instanceof VaultAccessError && error.status === 403);
  await assert.rejects(() => reader.readNote(outside), (error) => error instanceof VaultAccessError && error.status === 403);
  await assert.rejects(() => reader.readNote('90 Templates/Hidden.md'), (error) => error instanceof VaultAccessError && error.status === 403);
  await assert.rejects(() => reader.readNote('90 templates/Hidden.md'), (error) => error instanceof VaultAccessError && error.status === 403);
  const excludedAlias = path.join(vault, 'Excluded Alias');
  const eligibleAlias = path.join(vault, 'Eligible Alias');
  const outsideDirectory = path.join(sandbox, 'outside-directory');
  const escapeAlias = path.join(vault, '00 Inbox', 'Escape');
  try {
    await fs.symlink(path.join(vault, '90 Templates'), excludedAlias, 'junction');
    await assert.rejects(() => reader.readNote('Excluded Alias/Hidden.md'), (error) => error instanceof VaultAccessError && error.status === 403);
    await fs.symlink(path.join(vault, '00 Inbox'), eligibleAlias, 'junction');
    assert.equal((await reader.readNote('Eligible Alias/Canvas Test Note.md')).title, 'Canvas Test Note');
    await fs.mkdir(outsideDirectory);
    await fs.writeFile(path.join(outsideDirectory, 'Escape.md'), '# Outside through junction');
    await fs.symlink(outsideDirectory, escapeAlias, 'junction');
    await assert.rejects(() => reader.readNote('00 Inbox/Escape/Escape.md'), (error) => error instanceof VaultAccessError && error.status === 403);
  } finally {
    await Promise.all([excludedAlias, eligibleAlias, escapeAlias, outsideDirectory].map((target) => fs.rm(target, { recursive: true, force: true })));
  }
});

test('preserves canvas attributes and marks an actually missing file target', async () => {
  const reader = new VaultReader(vault);
  const canvas = await reader.readCanvas('80 Canvases/Digital Jochi.canvas');
  assert.equal(canvas.nodes.length, 2);
  assert.equal(canvas.nodes[0].x, 10);
  assert.equal(canvas.nodes[0].color, '#ff0000');
  assert.equal(canvas.nodes[0].exists, true);
  assert.equal(canvas.nodes[1].file, '00 Inbox/Absent.md');
  assert.equal(canvas.nodes[1].exists, false);
  assert.deepEqual(
    { fromSide: canvas.edges[0].fromSide, toSide: canvas.edges[0].toSide, toEnd: canvas.edges[0].toEnd, color: canvas.edges[0].color },
    { fromSide: 'bottom', toSide: 'top', toEnd: undefined, color: '5' },
  );
});

test('resolves aliases and reports missing, unsupported, and ambiguous targets', async () => {
  const reader = new VaultReader(vault);
  assert.deepEqual(await reader.resolveLink('00 Inbox/Canvas Test Note.md', 'Home'), { status: 'resolved', path: 'Home.md' });
  assert.equal((await reader.resolveLink('Home.md', 'Missing')).status, 'missing');
  assert.equal((await reader.resolveLink('Home.md', 'Home#Section')).status, 'unsupported');
  await fs.mkdir(path.join(vault, 'Area A'), { recursive: true });
  await fs.mkdir(path.join(vault, 'Area B'), { recursive: true });
  try {
    await fs.writeFile(path.join(vault, 'Area A', 'Same Name.md'), '# First duplicate');
    await fs.writeFile(path.join(vault, 'Area B', 'Same Name.md'), '# Second duplicate');
    const ambiguous = await reader.resolveLink('Home.md', 'Same Name');
    assert.equal(ambiguous.status, 'ambiguous');
    assert.deepEqual(ambiguous.candidates, ['Area A/Same Name.md', 'Area B/Same Name.md']);
  } finally {
    await fs.rm(path.join(vault, 'Area A'), { recursive: true, force: true });
    await fs.rm(path.join(vault, 'Area B'), { recursive: true, force: true });
  }
});

test('applies resolved exclusions through the HTTP API', async () => {
  const alias = path.join(vault, 'Excluded API Alias');
  await fs.symlink(path.join(vault, '90 Templates'), alias, 'junction');
  const server = createApi(new VaultReader(vault), '80 Canvases/Digital Jochi.canvas').listen(0, '127.0.0.1');
  await once(server, 'listening');
  const address = server.address();
  assert.ok(address && typeof address === 'object');
  try {
    for (const notePath of ['90 templates/Hidden.md', 'Excluded API Alias/Hidden.md']) {
      const response = await fetch(`http://127.0.0.1:${address.port}/api/note?path=${encodeURIComponent(notePath)}`);
      assert.equal(response.status, 403);
    }
    const response = await fetch(`http://127.0.0.1:${address.port}/api/notes`);
    const payload = await response.json();
    assert.deepEqual(payload.notes.map((note) => note.path), ['00 Inbox/Canvas Test Note.md', 'Home.md']);
  } finally {
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    await fs.rm(alias, { recursive: true, force: true });
  }
});

test('reading notes does not modify source bytes', async () => {
  const reader = new VaultReader(vault);
  const target = path.join(vault, '00 Inbox', 'Canvas Test Note.md');
  const beforeBytes = await fs.readFile(target);
  await reader.readNote('00 Inbox/Canvas Test Note.md');
  await reader.listNotes();
  const afterBytes = await fs.readFile(target);
  assert.deepEqual(afterBytes, beforeBytes);
});

test('reads equivalent inline and block string tags while isolating metadata diagnostics', async () => {
  await fs.writeFile(
    path.join(vault, 'Inline Tags.md'),
    '---\ntags: [topic, "Mixed Case", topic, 42]\n---\n# Inline\n\n#body-tag',
  );
  await fs.writeFile(
    path.join(vault, 'Block Tags.md'),
    '---\ntags:\n  - topic\n  - "Mixed Case"\n---\n# Block',
  );
  await fs.writeFile(path.join(vault, 'Broken Frontmatter.md'), '---\ntags: [topic]\n# Still readable');
  const reader = new VaultReader(vault);
  const inline = await reader.readNote('Inline Tags.md');
  const block = await reader.readNote('Block Tags.md');
  const broken = await reader.readNote('Broken Frontmatter.md');
  assert.deepEqual(inline.tags, ['topic', 'Mixed Case']);
  assert.deepEqual(block.tags, ['topic', 'Mixed Case']);
  assert.equal(inline.diagnostics.length, 1);
  assert.match(inline.diagnostics[0], /non-string tag value: 42/);
  assert.doesNotMatch(inline.tags.join(','), /body-tag/);
  assert.match(broken.body, /Still readable/);
  assert.match(broken.diagnostics[0], /no closing delimiter/);
});

test('preserves valid string-like tags and trims quoted tag whitespace before deduplication', async () => {
  await fs.writeFile(
    path.join(vault, 'String Tags.md'),
    '---\ntags: [true-story, falsehood, nullability, 2026-plan, " topic ", topic, true, null, 42]\n---\n# String tags',
  );
  await fs.writeFile(
    path.join(vault, 'Block String Tags.md'),
    '---\ntags:\n  - true-story\n  - falsehood\n  - nullability\n  - 2026-plan\n  - " topic "\n  - topic\n  - false\n---\n# Block string tags',
  );
  const reader = new VaultReader(vault);
  const note = await reader.readNote('String Tags.md');
  const block = await reader.readNote('Block String Tags.md');
  assert.deepEqual(note.tags, ['true-story', 'falsehood', 'nullability', '2026-plan', 'topic']);
  assert.deepEqual(block.tags, ['true-story', 'falsehood', 'nullability', '2026-plan', 'topic']);
  assert.equal(note.diagnostics.length, 3);
  assert.equal(block.diagnostics.length, 1);
});

test('backlinks reuse supported link resolution and ignore duplicates, code, literals, and ambiguous names', async () => {
  await fs.mkdir(path.join(vault, 'Area A'), { recursive: true });
  await fs.mkdir(path.join(vault, 'Area B'), { recursive: true });
  await fs.writeFile(path.join(vault, 'Area A', 'Same Name.md'), '# First duplicate');
  await fs.writeFile(path.join(vault, 'Area B', 'Same Name.md'), '# Second duplicate');
  await fs.writeFile(path.join(vault, 'Source One.md'), '# Source One\n\n[[Home]] and [[Home|again]].');
  await fs.writeFile(path.join(vault, 'Source Two.md'), '# Source Two\n\n[[Home.md|path-qualified]].');
  await fs.writeFile(
    path.join(vault, 'Ignored Examples.md'),
    '# Ignored\n\n`[[Home]]`\n\n```md\n[[Home]]\n```\n\n\\[[Home]] [[Home#Heading]] [[Missing]] [[Same Name]]',
  );
  const reader = new VaultReader(vault);
  assert.deepEqual(await reader.listBacklinks('Home.md'), [
    { path: '00 Inbox/Canvas Test Note.md', title: 'Canvas Test Note' },
    { path: 'Source One.md', title: 'Source One' },
    { path: 'Source Two.md', title: 'Source Two' },
  ]);
  await fs.writeFile(path.join(vault, 'Source Two.md'), '# Source Two\n\nLink removed.');
  assert.deepEqual(await reader.listBacklinks('Home.md'), [
    { path: '00 Inbox/Canvas Test Note.md', title: 'Canvas Test Note' },
    { path: 'Source One.md', title: 'Source One' },
  ]);
});

test('backlinks exclude indented code and respect the opening fence length', async () => {
  await fs.writeFile(
    path.join(vault, 'Code Shapes.md'),
    '# Code shapes\n\n    [[Home]]\n\n````md\n[[Home]]\n```\nstill code [[Home]]\n````\n\nOutside [[Home]].',
  );
  const backlinks = await new VaultReader(vault).listBacklinks('Home.md');
  assert.equal(backlinks.filter((item) => item.path === 'Code Shapes.md').length, 1);

  await fs.writeFile(
    path.join(vault, 'Code Shapes.md'),
    '# Code shapes\n\n    [[Home]]\n\n````md\n[[Home]]\n```\nstill code [[Home]]\n````',
  );
  const codeOnly = await new VaultReader(vault).listBacklinks('Home.md');
  assert.equal(codeOnly.some((item) => item.path === 'Code Shapes.md'), false);
});

test('constructs an exact encoded Obsidian target from the validated absolute note path', async () => {
  await fs.mkdir(path.join(vault, 'Área'), { recursive: true });
  await fs.writeFile(path.join(vault, 'Área', 'Spaced Note.md'), '# Exact target');
  const target = await new VaultReader(vault).obsidianTarget('Área/Spaced Note.md');
  const targetUrl = new URL(target.uri);
  assert.equal(targetUrl.hostname, 'open');
  assert.equal(targetUrl.searchParams.get('path'), path.join(vault, 'Área', 'Spaced Note.md').replaceAll('\\', '/'));
  assert.equal(target.vaultPath, await fs.realpath(vault));
  assert.equal(target.path, 'Área/Spaced Note.md');

  const otherVault = path.join(sandbox, 'other', 'vault');
  await fs.mkdir(path.join(otherVault, 'Área'), { recursive: true });
  await fs.writeFile(path.join(otherVault, 'Área', 'Spaced Note.md'), '# Different exact target');
  const otherTarget = await new VaultReader(otherVault).obsidianTarget('Área/Spaced Note.md');
  assert.notEqual(otherTarget.uri, target.uri);
  assert.equal(new URL(otherTarget.uri).searchParams.get('path'), path.join(otherVault, 'Área', 'Spaced Note.md').replaceAll('\\', '/'));
  await assert.rejects(
    () => new VaultReader(vault).obsidianTarget('https://example.com.md'),
    (error) => error instanceof VaultAccessError && error.status === 403,
  );
});

test('refresh snapshot returns one coherent context and a recoverable removed-note state', async () => {
  await fs.writeFile(path.join(vault, 'Source.md'), '# Source\n\n[[Home]]');
  const server = createApi(new VaultReader(vault), '80 Canvases/Digital Jochi.canvas').listen(0, '127.0.0.1');
  await once(server, 'listening');
  const address = server.address();
  assert.ok(address && typeof address === 'object');
  try {
    const url = `http://127.0.0.1:${address.port}/api/refresh?path=${encodeURIComponent('Home.md')}`;
    const first = await (await fetch(url)).json();
    assert.equal(first.context.note.path, 'Home.md');
    assert.deepEqual(first.context.backlinks, [
      { path: '00 Inbox/Canvas Test Note.md', title: 'Canvas Test Note' },
      { path: 'Source.md', title: 'Source' },
    ]);
    assert.equal(first.canvasPath, '80 Canvases/Digital Jochi.canvas');
    await fs.rm(path.join(vault, 'Home.md'));
    const removed = await (await fetch(url)).json();
    assert.equal(removed.context, null);
    assert.match(removed.selectedError, /not found: Home.md/);
    assert.equal(removed.notes.some((note) => note.path === 'Home.md'), false);
  } finally {
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
});

test('refresh isolates a broken canvas while still returning readable notes', async () => {
  const server = createApi(new VaultReader(vault), '80 Canvases/Digital Jochi.canvas').listen(0, '127.0.0.1');
  await once(server, 'listening');
  const address = server.address();
  assert.ok(address && typeof address === 'object');
  try {
    const url = `http://127.0.0.1:${address.port}/api/refresh?path=${encodeURIComponent('Home.md')}`;
    const initial = await (await fetch(url)).json();
    assert.equal(initial.canvas.nodes.length, 2);
    await fs.writeFile(path.join(vault, 'Home.md'), '# Home\n\nFresh note content after the board breaks.');
    await fs.writeFile(path.join(vault, '80 Canvases', 'Digital Jochi.canvas'), '{ broken json');
    const response = await fetch(url);
    const payload = await response.json();
    assert.equal(response.status, 200);
    assert.equal(payload.notesError, '');
    assert.equal(payload.notes.some((note) => note.path === 'Home.md'), true);
    assert.equal(payload.context.note.path, 'Home.md');
    assert.match(payload.context.note.body, /Fresh note content/);
    assert.equal(payload.canvas, null);
    assert.match(payload.canvasError, /invalid JSON/);
  } finally {
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
});

test('refresh distinguishes an unavailable vault from an isolated canvas failure', async () => {
  const missingVault = path.join(sandbox, 'missing-vault');
  const server = createApi(new VaultReader(missingVault), '80 Canvases/Digital Jochi.canvas').listen(0, '127.0.0.1');
  await once(server, 'listening');
  const address = server.address();
  assert.ok(address && typeof address === 'object');
  try {
    const response = await fetch(`http://127.0.0.1:${address.port}/api/refresh`);
    const payload = await response.json();
    assert.equal(response.status, 200);
    assert.deepEqual(payload.notes, []);
    assert.match(payload.notesError, /configured vault is unavailable/);
    assert.equal(payload.canvas, null);
    assert.match(payload.canvasError, /configured vault is unavailable/);
  } finally {
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
});

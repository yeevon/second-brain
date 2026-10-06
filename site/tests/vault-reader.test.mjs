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

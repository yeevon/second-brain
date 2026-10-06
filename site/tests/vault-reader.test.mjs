import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { VaultAccessError, VaultReader } from '../dist-server/vault-reader.js';

let sandbox = '';
let vault = '';
let outside = '';

before(async () => {
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
      nodes: [{ id: 'test', type: 'file', file: '00 Inbox/Canvas Test Note.md', x: 10, y: 20, width: 300, height: 200 }],
      edges: [],
    }),
  );
});

after(async () => {
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

test('rejects traversal, absolute paths, excluded files, and escaping junctions', async () => {
  const reader = new VaultReader(vault);
  await assert.rejects(() => reader.readNote('../outside.md'), (error) => error instanceof VaultAccessError && error.status === 403);
  await assert.rejects(() => reader.readNote(outside), (error) => error instanceof VaultAccessError && error.status === 403);
  await assert.rejects(() => reader.readNote('90 Templates/Hidden.md'), (error) => error instanceof VaultAccessError && error.status === 403);
  const outsideDirectory = path.join(sandbox, 'outside-directory');
  await fs.mkdir(outsideDirectory);
  await fs.writeFile(path.join(outsideDirectory, 'Escape.md'), '# Outside through junction');
  await fs.symlink(outsideDirectory, path.join(vault, '00 Inbox', 'Escape'), 'junction');
  await assert.rejects(() => reader.readNote('00 Inbox/Escape/Escape.md'), (error) => error instanceof VaultAccessError && error.status === 403);
});

test('reads authored canvas geometry and marks missing file targets', async () => {
  const reader = new VaultReader(vault);
  const canvas = await reader.readCanvas('80 Canvases/Digital Jochi.canvas');
  assert.equal(canvas.nodes.length, 1);
  assert.equal(canvas.nodes[0].x, 10);
  assert.equal(canvas.nodes[0].exists, true);
});

test('resolves aliases and reports missing, unsupported, and ambiguous targets', async () => {
  const reader = new VaultReader(vault);
  assert.deepEqual(await reader.resolveLink('00 Inbox/Canvas Test Note.md', 'Home'), { status: 'resolved', path: 'Home.md' });
  assert.equal((await reader.resolveLink('Home.md', 'Missing')).status, 'missing');
  assert.equal((await reader.resolveLink('Home.md', 'Home#Section')).status, 'unsupported');
  await fs.mkdir(path.join(vault, 'Area A'), { recursive: true });
  await fs.mkdir(path.join(vault, 'Area B'), { recursive: true });
  await fs.writeFile(path.join(vault, 'Area A', 'Same Name.md'), '# First duplicate');
  await fs.writeFile(path.join(vault, 'Area B', 'Same Name.md'), '# Second duplicate');
  const ambiguous = await reader.resolveLink('Home.md', 'Same Name');
  assert.equal(ambiguous.status, 'ambiguous');
  assert.deepEqual(ambiguous.candidates, ['Area A/Same Name.md', 'Area B/Same Name.md']);
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

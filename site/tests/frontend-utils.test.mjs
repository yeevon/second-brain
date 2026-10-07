import assert from 'node:assert/strict';
import { test } from 'node:test';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import ts from 'typescript';

const importTypeScript = async (relativePath) => {
  const source = await fs.readFile(path.resolve(relativePath), 'utf8');
  const output = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
    fileName: relativePath,
  }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(output).toString('base64')}`);
};

test('malformed encoded wikilinks return a visible-safe diagnostic value', async () => {
  const { decodeWikilink } = await importTypeScript('src/link-utils.ts');
  assert.deepEqual(decodeWikilink('wikilink:%FF'), { ok: false, target: '%FF' });
  assert.deepEqual(decodeWikilink('wikilink:Home%20note'), { ok: true, target: 'Home note' });
});

test('search excerpts come from the field that matched', async () => {
  const { excerptFor } = await importTypeScript('src/search.ts');
  const note = {
    path: 'Example.md',
    title: 'Canvas Test Note',
    summary: 'A summary without the body token.',
    body: 'A longer observation appears only in the note body.',
  };
  assert.match(excerptFor(note, 'ObSeRvAtIoN'), /observation/i);
  assert.match(excerptFor(note, 'summary'), /summary/i);
  assert.equal(excerptFor(note, 'canvas'), 'Title match: Canvas Test Note');
});

test('search and one frontmatter tag combine as an intersection', async () => {
  const { filterNotes } = await importTypeScript('src/search.ts');
  const notes = [
    { path: 'Alpha.md', title: 'Alpha', summary: '', body: 'needle', tags: ['topic'], diagnostics: [] },
    { path: 'Beta.md', title: 'Beta needle', summary: '', body: '', tags: ['other'], diagnostics: [] },
    { path: 'Gamma.md', title: 'Gamma', summary: '', body: 'needle', tags: ['topic'], diagnostics: [] },
  ];
  assert.deepEqual(filterNotes(notes, 'needle', 'topic').map((note) => note.path), ['Alpha.md', 'Gamma.md']);
  assert.deepEqual(filterNotes(notes, '', 'other').map((note) => note.path), ['Beta.md']);
  assert.deepEqual(filterNotes(notes, 'missing', 'topic'), []);
});

test('refresh work is coalesced while current and invalidated work cannot overwrite newer navigation', async () => {
  const { RefreshCoordinator } = await importTypeScript('src/refresh-coordinator.ts');
  const coordinator = new RefreshCoordinator();
  let release;
  let loadCount = 0;
  const applied = [];
  const held = new Promise((resolve) => { release = resolve; });
  const first = coordinator.run(() => {
    loadCount += 1;
    return held;
  }, (value) => applied.push(value), assert.fail);
  const repeated = coordinator.run(() => {
    loadCount += 1;
    return Promise.resolve('duplicate');
  }, (value) => applied.push(value), assert.fail);
  assert.equal(first, repeated);
  assert.equal(loadCount, 1);
  const navigationRevision = coordinator.invalidate();
  assert.equal(coordinator.isCurrent(navigationRevision), true);
  release('stale');
  await first;
  assert.deepEqual(applied, []);

  await coordinator.run(() => Promise.resolve('fresh'), (value) => applied.push(value), assert.fail);
  assert.deepEqual(applied, ['fresh']);
  assert.equal(coordinator.isCurrent(navigationRevision), false);

  let rejectStale;
  const staleFailure = coordinator.run(() => new Promise((_resolve, reject) => { rejectStale = reject; }), assert.fail, (error) => applied.push(error.message));
  coordinator.invalidate();
  rejectStale(new Error('older failure'));
  await staleFailure;
  assert.deepEqual(applied, ['fresh']);
});

test('a refresh requested after navigation queues behind invalidated refresh work', async () => {
  const { RefreshCoordinator } = await importTypeScript('src/refresh-coordinator.ts');
  const coordinator = new RefreshCoordinator();
  let releaseStale;
  let releaseReturned;
  const stale = new Promise((resolve) => { releaseStale = resolve; });
  const returned = new Promise((resolve) => { releaseReturned = resolve; });
  const applied = [];
  let loadCount = 0;

  const first = coordinator.run(() => {
    loadCount += 1;
    return stale;
  }, (value) => applied.push(value), assert.fail);
  coordinator.invalidate();
  const afterReturn = coordinator.run(() => {
    loadCount += 1;
    return returned;
  }, (value) => applied.push(value), assert.fail);

  assert.notEqual(first, afterReturn);
  assert.equal(loadCount, 1);
  releaseStale('superseded');
  await first;
  assert.equal(loadCount, 2);
  releaseReturned('returned fresh');
  await afterReturn;
  assert.deepEqual(applied, ['returned fresh']);
});

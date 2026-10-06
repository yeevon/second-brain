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

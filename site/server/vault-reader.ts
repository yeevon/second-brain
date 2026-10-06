import { promises as fs } from 'node:fs';
import path from 'node:path';

export type NoteSummary = {
  path: string;
  title: string;
  summary: string;
  body: string;
};

export type Note = NoteSummary & {
  raw: string;
  metadata: Record<string, string>;
};

export type CanvasNode = {
  id: string;
  type: string;
  file?: string;
  text?: string;
  url?: string;
  x: number;
  y: number;
  width: number;
  height: number;
  color?: string;
  exists?: boolean;
};

export type CanvasEdge = {
  id: string;
  fromNode: string;
  toNode: string;
  fromSide?: string;
  toSide?: string;
  fromEnd?: string;
  toEnd?: string;
  label?: string;
  color?: string;
};

export type CanvasDocument = { nodes: CanvasNode[]; edges: CanvasEdge[] };

export class VaultAccessError extends Error {
  constructor(
    message: string,
    public readonly status = 400,
  ) {
    super(message);
  }
}

const normalizeVaultPath = (value: string): string => value.replaceAll('\\', '/').replace(/^\.\//, '');

const isExcluded = (relativePath: string): boolean => {
  const normalized = normalizeVaultPath(relativePath);
  const segments = normalized.split('/');
  return (
    segments.some((segment) => segment.startsWith('.')) ||
    segments[0] === '90 Templates' ||
    normalized.startsWith('99 Attachments/Implementation/')
  );
};

const splitFrontmatter = (raw: string): { body: string; metadata: Record<string, string> } => {
  if (!raw.startsWith('---\n') && !raw.startsWith('---\r\n')) return { body: raw, metadata: {} };
  const lines = raw.split(/\r?\n/);
  const end = lines.slice(1).findIndex((line) => line.trim() === '---');
  if (end < 0) return { body: raw, metadata: {} };
  const metadata: Record<string, string> = {};
  for (const line of lines.slice(1, end + 1)) {
    const match = line.match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
    if (match) metadata[match[1]] = match[2].trim().replace(/^['"]|['"]$/g, '');
  }
  return { body: lines.slice(end + 2).join('\n').replace(/^\s+/, ''), metadata };
};

const titleFrom = (body: string, relativePath: string): string => {
  const heading = body.match(/^#\s+(.+)$/m)?.[1]?.trim();
  return heading || path.posix.basename(normalizeVaultPath(relativePath), '.md');
};

export class VaultReader {
  constructor(public readonly root: string) {}

  private async rootRealPath(): Promise<string> {
    try {
      return await fs.realpath(this.root);
    } catch {
      throw new VaultAccessError(`The configured vault is unavailable: ${this.root}`, 503);
    }
  }

  private validateRelative(relativePath: string, extensions: string[]): string {
    if (!relativePath || relativePath.includes('\0')) throw new VaultAccessError('A vault-relative path is required.');
    const normalized = normalizeVaultPath(relativePath);
    if (path.posix.isAbsolute(normalized) || /^[A-Za-z]:/.test(normalized)) {
      throw new VaultAccessError('Absolute paths are not allowed.', 403);
    }
    const segments = normalized.split('/');
    if (segments.some((segment) => segment === '..' || segment === '')) {
      throw new VaultAccessError('The requested path leaves the configured vault.', 403);
    }
    if (isExcluded(normalized)) throw new VaultAccessError('The requested path is excluded from the reader.', 403);
    if (!extensions.includes(path.posix.extname(normalized).toLowerCase())) {
      throw new VaultAccessError('This file type is not readable through the vault API.', 415);
    }
    return normalized;
  }

  private async resolveExisting(relativePath: string, extensions: string[]): Promise<{ absolute: string; relative: string }> {
    const relative = this.validateRelative(relativePath, extensions);
    const rootReal = await this.rootRealPath();
    const candidate = path.resolve(rootReal, ...relative.split('/'));
    let candidateReal: string;
    try {
      candidateReal = await fs.realpath(candidate);
    } catch {
      throw new VaultAccessError(`The requested file was not found: ${relative}`, 404);
    }
    const rootPrefix = rootReal.endsWith(path.sep) ? rootReal : `${rootReal}${path.sep}`;
    if (candidateReal !== rootReal && !candidateReal.startsWith(rootPrefix)) {
      throw new VaultAccessError('The requested file resolves outside the configured vault.', 403);
    }
    return { absolute: candidateReal, relative };
  }

  private async walk(directory: string, prefix = ''): Promise<string[]> {
    const entries = await fs.readdir(directory, { withFileTypes: true });
    const results: string[] = [];
    for (const entry of entries) {
      const relative = normalizeVaultPath(path.posix.join(prefix, entry.name));
      if (isExcluded(relative) || entry.isSymbolicLink()) continue;
      if (entry.isDirectory()) results.push(...(await this.walk(path.join(directory, entry.name), relative)));
      else if (entry.isFile() && relative.toLowerCase().endsWith('.md')) results.push(relative);
    }
    return results;
  }

  async listNotes(): Promise<NoteSummary[]> {
    const rootReal = await this.rootRealPath();
    const paths = (await this.walk(rootReal)).sort((a, b) => a.localeCompare(b));
    return Promise.all(
      paths.map(async (relativePath) => {
        const note = await this.readNote(relativePath);
        return { path: note.path, title: note.title, summary: note.summary, body: note.body };
      }),
    );
  }

  async readNote(relativePath: string): Promise<Note> {
    const resolved = await this.resolveExisting(relativePath, ['.md']);
    const raw = await fs.readFile(resolved.absolute, 'utf8');
    const { body, metadata } = splitFrontmatter(raw);
    return {
      path: resolved.relative,
      title: titleFrom(body, resolved.relative),
      summary: metadata.summary ?? '',
      body,
      raw,
      metadata,
    };
  }

  async noteExists(relativePath: string): Promise<boolean> {
    try {
      await this.resolveExisting(relativePath, ['.md']);
      return true;
    } catch {
      return false;
    }
  }

  async readCanvas(relativePath: string): Promise<CanvasDocument> {
    const resolved = await this.resolveExisting(relativePath, ['.canvas']);
    let parsed: unknown;
    try {
      parsed = JSON.parse(await fs.readFile(resolved.absolute, 'utf8'));
    } catch {
      throw new VaultAccessError('The home canvas contains invalid JSON. Fix the file, then retry.', 422);
    }
    if (!parsed || typeof parsed !== 'object' || !Array.isArray((parsed as CanvasDocument).nodes) || !Array.isArray((parsed as CanvasDocument).edges)) {
      throw new VaultAccessError('The home canvas is missing its nodes or edges collection.', 422);
    }
    const canvas = parsed as CanvasDocument;
    const nodes = await Promise.all(
      canvas.nodes.map(async (node) => ({
        ...node,
        exists: node.type === 'file' && node.file ? await this.noteExists(node.file) : undefined,
      })),
    );
    return { nodes, edges: canvas.edges };
  }

  async resolveLink(sourcePath: string, rawTarget: string): Promise<
    | { status: 'resolved'; path: string }
    | { status: 'missing' | 'ambiguous' | 'unsupported'; message: string; candidates?: string[] }
  > {
    await this.readNote(sourcePath);
    const target = rawTarget.trim();
    const hashIndex = target.indexOf('#');
    if (hashIndex >= 0) {
      return {
        status: 'unsupported',
        message: `Heading and block targets are not supported yet: ${target}`,
      };
    }
    const normalized = normalizeVaultPath(target).replace(/\.md$/i, '');
    const notes = await this.listNotes();
    let matches: NoteSummary[];
    if (normalized.includes('/')) {
      matches = notes.filter((note) => note.path.slice(0, -3).toLocaleLowerCase() === normalized.toLocaleLowerCase());
    } else {
      matches = notes.filter(
        (note) => path.posix.basename(note.path, '.md').toLocaleLowerCase() === normalized.toLocaleLowerCase(),
      );
    }
    if (matches.length === 1) return { status: 'resolved', path: matches[0].path };
    if (matches.length > 1) {
      return {
        status: 'ambiguous',
        message: `More than one note matches “${target}”. Use a vault-relative path.`,
        candidates: matches.map((note) => note.path),
      };
    }
    return { status: 'missing', message: `No note matches “${target}”.` };
  }
}

export const findDefaultVault = async (): Promise<string> => {
  if (process.env.DIGITAL_JOCHI_VAULT) return path.resolve(process.env.DIGITAL_JOCHI_VAULT);
  const candidates = [
    path.resolve(process.cwd(), '..', '..', 'digital-jochi', 'vault'),
    path.resolve(process.cwd(), '..', 'vault'),
    path.resolve(process.cwd(), 'vault'),
  ];
  for (const candidate of candidates) {
    try {
      if ((await fs.stat(candidate)).isDirectory()) return candidate;
    } catch {
      // Try the next conventional project location.
    }
  }
  return candidates[0];
};

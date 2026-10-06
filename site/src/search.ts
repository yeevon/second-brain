import type { NoteSummary } from './types';

export const plainText = (markdown: string): string =>
  markdown
    .replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, '$2 $1')
    .replace(/[*_#>`~-]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();

const matchingExcerpt = (source: string, query: string): string => {
  const index = source.toLocaleLowerCase().indexOf(query.toLocaleLowerCase());
  if (index < 0) return source.slice(0, 120);
  const start = Math.max(0, index - 42);
  const end = Math.min(source.length, index + query.length + 70);
  return `${start ? '…' : ''}${source.slice(start, end)}${end < source.length ? '…' : ''}`;
};

export const excerptFor = (note: NoteSummary, query: string): string => {
  const normalized = query.trim().toLocaleLowerCase();
  const title = plainText(note.title);
  const summary = plainText(note.summary);
  const body = plainText(note.body);

  if (!normalized) return summary || body.slice(0, 110) || title;
  if (summary.toLocaleLowerCase().includes(normalized)) return matchingExcerpt(summary, query.trim());
  if (body.toLocaleLowerCase().includes(normalized)) return matchingExcerpt(body, query.trim());
  if (title.toLocaleLowerCase().includes(normalized)) return `Title match: ${title}`;
  return summary || body.slice(0, 120) || title;
};

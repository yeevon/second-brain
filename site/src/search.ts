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

export const filterNotes = (notes: NoteSummary[], query: string, tag: string): NoteSummary[] => {
  const normalized = query.trim().toLocaleLowerCase();
  const tagged = tag ? notes.filter((note) => note.tags.includes(tag)) : notes;
  if (!normalized) return tagged;
  return tagged
    .map((note) => {
      const title = note.title.toLocaleLowerCase();
      const summary = note.summary.toLocaleLowerCase();
      const body = note.body.toLocaleLowerCase();
      const score = title.includes(normalized) ? 0 : summary.includes(normalized) ? 1 : body.includes(normalized) ? 2 : -1;
      return { note, score };
    })
    .filter((result) => result.score >= 0)
    .sort((left, right) => left.score - right.score || left.note.title.localeCompare(right.note.title))
    .map((result) => result.note);
};

import { useEffect, useMemo, useRef, useState } from 'react';
import type { NoteSummary } from '../types';

type Props = {
  notes: NoteSummary[];
  error: string;
  selectedPath: string;
  onRetry: () => void;
  onOpen: (path: string) => void;
};

const plainText = (markdown: string): string =>
  markdown
    .replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, '$2 $1')
    .replace(/[*_#>`~-]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();

const excerptFor = (note: NoteSummary, query: string): string => {
  const source = plainText(note.summary || note.body);
  if (!query) return note.summary || source.slice(0, 110);
  const index = source.toLocaleLowerCase().indexOf(query.toLocaleLowerCase());
  if (index < 0) return source.slice(0, 120);
  const start = Math.max(0, index - 42);
  const end = Math.min(source.length, index + query.length + 70);
  return `${start ? '…' : ''}${source.slice(start, end)}${end < source.length ? '…' : ''}`;
};

export function Sidebar({ notes, error, selectedPath, onRetry, onOpen }: Props) {
  const [query, setQuery] = useState('');
  const listRef = useRef<HTMLDivElement>(null);
  const normalized = query.trim().toLocaleLowerCase();
  const results = useMemo(
    () => {
      if (!normalized) return notes;
      return notes
        .map((note) => {
          const title = note.title.toLocaleLowerCase();
          const summary = note.summary.toLocaleLowerCase();
          const body = note.body.toLocaleLowerCase();
          const score = title.includes(normalized) ? 0 : summary.includes(normalized) ? 1 : body.includes(normalized) ? 2 : -1;
          return { note, score };
        })
        .filter((result) => result.score >= 0)
        .sort((a, b) => a.score - b.score || a.note.title.localeCompare(b.note.title))
        .map((result) => result.note);
    },
    [normalized, notes],
  );

  useEffect(() => {
    if (listRef.current) listRef.current.scrollTop = 0;
  }, [normalized]);

  return (
    <aside className="sidebar" aria-label="Vault notes and search">
      <div className="region-heading sidebar-heading">
        <div>
          <p className="eyebrow">Browse</p>
          <h2>{normalized ? 'Search results' : 'Vault notes'}</h2>
        </div>
        <span className="count-badge">{results.length}</span>
      </div>

      <label className="search-field">
        <span className="visually-hidden">Search note titles and content</span>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m21 21-4.35-4.35m2.35-5.15A7.5 7.5 0 1 1 4 11.5a7.5 7.5 0 0 1 15 0Z" /></svg>
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search titles and notes…"
          autoComplete="off"
        />
        {query && <kbd>esc</kbd>}
      </label>

      {error ? (
        <div className="inline-state error-state" role="alert">
          <strong>Vault unavailable</strong>
          <p>{error}</p>
          <button type="button" onClick={onRetry}>Retry</button>
        </div>
      ) : normalized && results.length === 0 ? (
        <div className="inline-state empty-search" role="status">
          <span aria-hidden="true">∅</span>
          <strong>No notes found</strong>
          <p>Nothing in the local vault matches “{query.trim()}”.</p>
        </div>
      ) : (
        <div className="note-list" aria-live="polite" ref={listRef}>
          {results.map((note) => (
            <button
              type="button"
              key={note.path}
              className={`note-list-item ${selectedPath === note.path ? 'active' : ''}`}
              onClick={() => onOpen(note.path)}
            >
              <span className="note-icon" aria-hidden="true">↗</span>
              <span className="note-copy">
                <strong>{note.title}</strong>
                <small>{note.path}</small>
                {normalized && <span className="search-excerpt">{excerptFor(note, query.trim())}</span>}
              </span>
            </button>
          ))}
        </div>
      )}
    </aside>
  );
}

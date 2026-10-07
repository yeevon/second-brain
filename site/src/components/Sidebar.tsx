import { useEffect, useMemo, useRef } from 'react';
import type { NoteSummary } from '../types';
import { excerptFor, filterNotes } from '../search';

type Props = {
  notes: NoteSummary[];
  error: string;
  selectedPath: string;
  onRetry: () => void;
  onOpen: (path: string) => void;
  query: string;
  onQueryChange: (query: string) => void;
  selectedTag: string;
  onTagChange: (tag: string) => void;
  filterNotice: string;
};

export function Sidebar({ notes, error, selectedPath, onRetry, onOpen, query, onQueryChange, selectedTag, onTagChange, filterNotice }: Props) {
  const listRef = useRef<HTMLDivElement>(null);
  const normalized = query.trim().toLocaleLowerCase();
  const tags = useMemo(
    () => [...new Set(notes.flatMap((note) => note.tags))].sort((left, right) => left.localeCompare(right)),
    [notes],
  );
  const results = useMemo(
    () => filterNotes(notes, query, selectedTag),
    [normalized, notes, selectedTag],
  );

  useEffect(() => {
    if (listRef.current) listRef.current.scrollTop = 0;
  }, [normalized, selectedTag]);

  const constrained = Boolean(normalized || selectedTag);

  return (
    <aside className="sidebar" aria-label="Vault notes and search">
      <div className="region-heading sidebar-heading">
        <div>
          <p className="eyebrow">Browse</p>
          <h2>{constrained ? 'Filtered notes' : 'Vault notes'}</h2>
        </div>
        <span className="count-badge">{results.length}</span>
      </div>

      <label className="search-field">
        <span className="visually-hidden">Search note titles and content</span>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m21 21-4.35-4.35m2.35-5.15A7.5 7.5 0 1 1 4 11.5a7.5 7.5 0 0 1 15 0Z" /></svg>
        <input
          type="search"
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          placeholder="Search titles and notes…"
          autoComplete="off"
        />
        {query && <kbd>esc</kbd>}
      </label>

      <label className="tag-filter">
        <span>Frontmatter tag</span>
        <select value={selectedTag} onChange={(event) => onTagChange(event.target.value)}>
          <option value="">All tags</option>
          {tags.map((tag) => <option value={tag} key={tag}>{tag}</option>)}
        </select>
      </label>
      <p className="filter-summary" role="status">
        {selectedTag ? `Tag: ${selectedTag}` : 'All tags'} · {results.length} {results.length === 1 ? 'result' : 'results'}
      </p>
      {filterNotice && <div className="filter-notice" role="status">{filterNotice}</div>}

      {error ? (
        <div className="inline-state error-state" role="alert">
          <strong>Vault unavailable</strong>
          <p>{error}</p>
          <button type="button" onClick={onRetry}>Retry</button>
        </div>
      ) : constrained && results.length === 0 ? (
        <div className="inline-state empty-search" role="status">
          <span aria-hidden="true">∅</span>
          <strong>No notes found</strong>
          <p>No eligible note matches the current {normalized && selectedTag ? 'search and tag filters' : selectedTag ? `“${selectedTag}” tag` : `search “${query.trim()}”`}.</p>
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
                {note.diagnostics.length > 0 && <span className="metadata-diagnostic">⚠ {note.diagnostics[0]}</span>}
              </span>
            </button>
          ))}
        </div>
      )}
    </aside>
  );
}

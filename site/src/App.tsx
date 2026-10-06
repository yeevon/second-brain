import { useCallback, useEffect, useMemo, useState } from 'react';
import { Board } from './components/Board';
import { NotePane } from './components/NotePane';
import { Sidebar } from './components/Sidebar';
import { fetchNote, fetchNotes, resolveLink } from './api';
import type { Note, NoteSummary, UnresolvedState } from './types';

type ReaderState =
  | { kind: 'empty' }
  | { kind: 'loading'; path: string }
  | { kind: 'note'; note: Note }
  | { kind: 'error'; message: string; path?: string }
  | { kind: 'unresolved'; state: UnresolvedState };

export function App() {
  const [notes, setNotes] = useState<NoteSummary[]>([]);
  const [notesError, setNotesError] = useState('');
  const [reader, setReader] = useState<ReaderState>({ kind: 'empty' });
  const [history, setHistory] = useState<string[]>([]);
  const [selectedPath, setSelectedPath] = useState('');

  const loadNotes = useCallback(async () => {
    setNotesError('');
    try {
      setNotes(await fetchNotes());
    } catch (error) {
      setNotesError(error instanceof Error ? error.message : 'The vault could not be loaded.');
    }
  }, []);

  useEffect(() => {
    void loadNotes();
  }, [loadNotes]);

  const openNote = useCallback(
    async (notePath: string, rememberCurrent = true) => {
      if (rememberCurrent && reader.kind === 'note' && reader.note.path !== notePath) {
        setHistory((current) => [...current, reader.note.path]);
      }
      setSelectedPath(notePath);
      setReader({ kind: 'loading', path: notePath });
      try {
        setReader({ kind: 'note', note: await fetchNote(notePath) });
      } catch (error) {
        setReader({
          kind: 'error',
          path: notePath,
          message: error instanceof Error ? error.message : 'The note could not be opened.',
        });
      }
    },
    [reader],
  );

  const followLink = useCallback(
    async (target: string) => {
      if (reader.kind !== 'note') return;
      const sourcePath = reader.note.path;
      try {
        const resolution = await resolveLink(sourcePath, target);
        setHistory((current) => [...current, sourcePath]);
        if (resolution.status === 'resolved') await openNote(resolution.path, false);
        else setReader({ kind: 'unresolved', state: { ...resolution, target } });
      } catch (error) {
        setReader({
          kind: 'error',
          message: error instanceof Error ? error.message : 'The link could not be resolved.',
        });
      }
    },
    [openNote, reader],
  );

  const goBack = useCallback(() => {
    const prior = history.at(-1);
    if (!prior) return;
    setHistory((current) => current.slice(0, -1));
    void openNote(prior, false);
  }, [history, openNote]);

  const retryReader = useCallback(() => {
    if (reader.kind === 'error' && reader.path) void openNote(reader.path, false);
    else void loadNotes();
  }, [loadNotes, openNote, reader]);

  const statusText = useMemo(() => {
    if (notesError) return 'Vault unavailable';
    if (notes.length) return `${notes.length} notes indexed locally`;
    return 'Reading local vault…';
  }, [notes.length, notesError]);

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-lockup">
          <div className="brand-mark" aria-hidden="true">DJ</div>
          <div>
            <p className="eyebrow">Local vault reader</p>
            <h1>Digital Jochi</h1>
          </div>
        </div>
        <div className="home-indicator" aria-label="Current view: Home">
          <span className="home-dot" />
          Home
        </div>
        <p className="index-status">{statusText}</p>
      </header>

      <main className="workspace">
        <Sidebar notes={notes} error={notesError} onRetry={loadNotes} onOpen={openNote} selectedPath={selectedPath} />
        <section className="board-region" aria-label="Home board region">
          <div className="region-heading">
            <div>
              <p className="eyebrow">Visual map</p>
              <h2>Home board</h2>
            </div>
            <span className="read-only-badge">Read only</span>
          </div>
          <Board selectedPath={selectedPath} onOpen={openNote} />
        </section>
        <NotePane state={reader} canGoBack={history.length > 0} onBack={goBack} onFollowLink={followLink} onRetry={retryReader} />
      </main>
    </div>
  );
}

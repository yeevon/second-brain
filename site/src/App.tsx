import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Board } from './components/Board';
import { NotePane } from './components/NotePane';
import { Sidebar } from './components/Sidebar';
import { fetchNote, fetchNotes, resolveLink } from './api';
import type { Note, NoteSummary, UnresolvedState } from './types';

type ReaderState =
  | { kind: 'empty' }
  | { kind: 'loading'; path: string }
  | { kind: 'note'; note: Note }
  | { kind: 'error'; message: string; path?: string; recovery?: { sourcePath: string; target: string } }
  | { kind: 'unresolved'; state: UnresolvedState };

export function App() {
  const [notes, setNotes] = useState<NoteSummary[]>([]);
  const [notesError, setNotesError] = useState('');
  const [reader, setReader] = useState<ReaderState>({ kind: 'empty' });
  const [history, setHistory] = useState<string[]>([]);
  const [selectedPath, setSelectedPath] = useState('');
  const readerRef = useRef<ReaderState>(reader);
  const historyRef = useRef<string[]>(history);
  const navigationRef = useRef(0);

  const updateReader = useCallback((next: ReaderState) => {
    readerRef.current = next;
    setReader(next);
  }, []);

  const updateHistory = useCallback((next: string[]) => {
    historyRef.current = next;
    setHistory(next);
  }, []);

  const pushHistory = useCallback((notePath: string) => {
    if (historyRef.current.at(-1) === notePath) return;
    updateHistory([...historyRef.current, notePath]);
  }, [updateHistory]);

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

  const loadNote = useCallback(
    async (notePath: string, navigationId: number, rememberCurrent: boolean) => {
      const current = readerRef.current;
      if (rememberCurrent && current.kind === 'note' && current.note.path !== notePath) pushHistory(current.note.path);
      setSelectedPath(notePath);
      updateReader({ kind: 'loading', path: notePath });
      try {
        const note = await fetchNote(notePath);
        if (navigationId === navigationRef.current) updateReader({ kind: 'note', note });
      } catch (error) {
        if (navigationId === navigationRef.current) {
          updateReader({
            kind: 'error',
            path: notePath,
            message: error instanceof Error ? error.message : 'The note could not be opened.',
          });
        }
      }
    },
    [pushHistory, updateReader],
  );

  const openNote = useCallback((notePath: string, rememberCurrent = true) => {
    const navigationId = ++navigationRef.current;
    void loadNote(notePath, navigationId, rememberCurrent);
  }, [loadNote]);

  const resolveAndOpenLink = useCallback(async (sourcePath: string, target: string) => {
    const navigationId = ++navigationRef.current;
    try {
      const resolution = await resolveLink(sourcePath, target);
      if (navigationId !== navigationRef.current) return;
      pushHistory(sourcePath);
      if (resolution.status === 'resolved') await loadNote(resolution.path, navigationId, false);
      else updateReader({ kind: 'unresolved', state: { ...resolution, target } });
    } catch (error) {
      if (navigationId !== navigationRef.current) return;
      updateReader({
        kind: 'error',
        message: error instanceof Error ? error.message : 'The link could not be resolved.',
        recovery: { sourcePath, target },
      });
    }
  }, [loadNote, pushHistory, updateReader]);

  const followLink = useCallback(
    (target: string) => {
      const current = readerRef.current;
      if (current.kind === 'note') void resolveAndOpenLink(current.note.path, target);
    },
    [resolveAndOpenLink],
  );

  const goBack = useCallback(() => {
    const prior = historyRef.current.at(-1);
    if (!prior) return;
    updateHistory(historyRef.current.slice(0, -1));
    openNote(prior, false);
  }, [openNote, updateHistory]);

  const retryReader = useCallback(() => {
    const current = readerRef.current;
    if (current.kind === 'error' && current.recovery) void resolveAndOpenLink(current.recovery.sourcePath, current.recovery.target);
    else if (current.kind === 'error' && current.path) openNote(current.path, false);
    else void loadNotes();
  }, [loadNotes, openNote, resolveAndOpenLink]);

  const returnToSource = useCallback(() => {
    const current = readerRef.current;
    if (current.kind === 'error' && current.recovery) openNote(current.recovery.sourcePath, false);
  }, [openNote]);

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
        <NotePane state={reader} canGoBack={history.length > 0} onBack={goBack} onFollowLink={followLink} onRetry={retryReader} onReturnToSource={returnToSource} />
      </main>
    </div>
  );
}

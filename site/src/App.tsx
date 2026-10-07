import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Board } from './components/Board';
import { NotePane } from './components/NotePane';
import { Sidebar } from './components/Sidebar';
import { fetchNoteContext, fetchRefreshSnapshot, resolveLink } from './api';
import { RefreshCoordinator } from './refresh-coordinator';
import type { NoteContext, NoteSummary, RefreshSnapshot, UnresolvedState, VaultCanvas } from './types';

type ReaderState =
  | { kind: 'empty' }
  | { kind: 'loading'; path: string }
  | ({ kind: 'note' } & NoteContext)
  | { kind: 'error'; message: string; path?: string; recovery?: { sourcePath: string; target: string } }
  | { kind: 'unresolved'; state: UnresolvedState };

export function App() {
  const [notes, setNotes] = useState<NoteSummary[]>([]);
  const [canvas, setCanvas] = useState<VaultCanvas>();
  const [canvasPath, setCanvasPath] = useState('80 Canvases/Digital Jochi.canvas');
  const [notesError, setNotesError] = useState('');
  const [canvasError, setCanvasError] = useState('');
  const [refreshError, setRefreshError] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [lastRefreshAt, setLastRefreshAt] = useState<Date>();
  const [reader, setReader] = useState<ReaderState>({ kind: 'empty' });
  const [history, setHistory] = useState<string[]>([]);
  const [selectedPath, setSelectedPath] = useState('');
  const [query, setQuery] = useState('');
  const [selectedTag, setSelectedTag] = useState('');
  const [filterNotice, setFilterNotice] = useState('');
  const readerRef = useRef<ReaderState>(reader);
  const historyRef = useRef<string[]>(history);
  const selectedPathRef = useRef('');
  const selectedTagRef = useRef('');
  const navigationRef = useRef(0);
  const refreshCoordinator = useRef(new RefreshCoordinator<RefreshSnapshot>());
  const loadedOnceRef = useRef(false);
  const awayRef = useRef(false);

  const updateReader = useCallback((next: ReaderState) => {
    readerRef.current = next;
    setReader(next);
  }, []);

  const updateHistory = useCallback((next: string[]) => {
    historyRef.current = next;
    setHistory(next);
  }, []);

  const updateSelectedPath = useCallback((path: string) => {
    selectedPathRef.current = path;
    setSelectedPath(path);
  }, []);

  const updateSelectedTag = useCallback((tag: string) => {
    selectedTagRef.current = tag;
    setSelectedTag(tag);
    setFilterNotice('');
  }, []);

  const pushHistory = useCallback((notePath: string) => {
    if (historyRef.current.at(-1) === notePath) return;
    updateHistory([...historyRef.current, notePath]);
  }, [updateHistory]);

  const applySnapshot = useCallback((snapshot: RefreshSnapshot, requestedPath: string) => {
    if (!snapshot.notesError) setNotes(snapshot.notes);
    if (snapshot.canvas) setCanvas(snapshot.canvas);
    setCanvasPath(snapshot.canvasPath);
    setNotesError(snapshot.notesError);
    setCanvasError(snapshot.canvasError);
    if (!snapshot.notesError) {
      const availableTags = new Set(snapshot.notes.flatMap((note) => note.tags));
      const currentTag = selectedTagRef.current;
      if (currentTag && !availableTags.has(currentTag)) {
        selectedTagRef.current = '';
        setSelectedTag('');
        setFilterNotice(`The “${currentTag}” tag is no longer available, so the filter was cleared.`);
      }
    }
    if (snapshot.notesError) {
      if (loadedOnceRef.current) setRefreshError(`${snapshot.notesError} Showing the last loaded content as stale.`);
    } else if (requestedPath) {
      if (snapshot.context) {
        updateReader({ kind: 'note', ...snapshot.context });
        setRefreshError('');
      } else {
        setRefreshError(`${snapshot.selectedError || `The selected note was removed: ${requestedPath}`} Showing the last loaded content as stale.`);
      }
    } else setRefreshError('');
    if (!snapshot.notesError) {
      setLastRefreshAt(new Date());
      loadedOnceRef.current = true;
    }
  }, [updateReader]);

  const refresh = useCallback(() => {
    let requestedPath = '';
    setRefreshing(true);
    const work = refreshCoordinator.current.run(
      () => {
        requestedPath = selectedPathRef.current;
        return fetchRefreshSnapshot(requestedPath);
      },
      (snapshot) => applySnapshot(snapshot, requestedPath),
      (error) => {
        const message = error instanceof Error ? error.message : 'The vault could not be refreshed.';
        if (loadedOnceRef.current) setRefreshError(`${message} Showing the last loaded content as stale.`);
        else setNotesError(message);
      },
    );
    void work.finally(() => {
      if (!refreshCoordinator.current.isBusy()) setRefreshing(false);
    });
    return work;
  }, [applySnapshot]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const loadNote = useCallback(
    async (notePath: string, navigationId: number, rememberCurrent: boolean) => {
      const requestRevision = refreshCoordinator.current.invalidate();
      const current = readerRef.current;
      if (rememberCurrent && current.kind === 'note' && current.note.path !== notePath) pushHistory(current.note.path);
      updateSelectedPath(notePath);
      updateReader({ kind: 'loading', path: notePath });
      try {
        const context = await fetchNoteContext(notePath);
        if (navigationId === navigationRef.current && refreshCoordinator.current.isCurrent(requestRevision)) {
          updateReader({ kind: 'note', ...context });
          setRefreshError('');
        }
      } catch (error) {
        if (navigationId === navigationRef.current && refreshCoordinator.current.isCurrent(requestRevision)) {
          updateReader({
            kind: 'error',
            path: notePath,
            message: error instanceof Error ? error.message : 'The note could not be opened.',
          });
        }
      }
    },
    [pushHistory, updateReader, updateSelectedPath],
  );

  const openNote = useCallback((notePath: string, rememberCurrent = true) => {
    const navigationId = ++navigationRef.current;
    void loadNote(notePath, navigationId, rememberCurrent);
  }, [loadNote]);

  const resolveAndOpenLink = useCallback(async (sourcePath: string, target: string) => {
    const requestRevision = refreshCoordinator.current.invalidate();
    const navigationId = ++navigationRef.current;
    try {
      const resolution = await resolveLink(sourcePath, target);
      if (navigationId !== navigationRef.current || !refreshCoordinator.current.isCurrent(requestRevision)) return;
      pushHistory(sourcePath);
      if (resolution.status === 'resolved') await loadNote(resolution.path, navigationId, false);
      else updateReader({ kind: 'unresolved', state: { ...resolution, target } });
    } catch (error) {
      if (navigationId !== navigationRef.current || !refreshCoordinator.current.isCurrent(requestRevision)) return;
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
    else void refresh();
  }, [openNote, refresh, resolveAndOpenLink]);

  const returnToSource = useCallback(() => {
    const current = readerRef.current;
    if (current.kind === 'error' && current.recovery) openNote(current.recovery.sourcePath, false);
  }, [openNote]);

  useEffect(() => {
    const markAway = () => { awayRef.current = true; };
    const refreshAfterReturn = () => {
      if (!loadedOnceRef.current || !awayRef.current || document.visibilityState === 'hidden') return;
      awayRef.current = false;
      void refresh();
    };
    const visibilityChanged = () => {
      if (document.visibilityState === 'hidden') markAway();
      else refreshAfterReturn();
    };
    window.addEventListener('blur', markAway);
    window.addEventListener('focus', refreshAfterReturn);
    document.addEventListener('visibilitychange', visibilityChanged);
    return () => {
      window.removeEventListener('blur', markAway);
      window.removeEventListener('focus', refreshAfterReturn);
      document.removeEventListener('visibilitychange', visibilityChanged);
    };
  }, [refresh]);

  const statusText = useMemo(() => {
    if (notesError) return 'Vault unavailable';
    if (refreshing) return 'Refreshing local vault…';
    if (refreshError) return 'Refresh failed · stale content';
    if (canvasError) return 'Notes current · home board unavailable';
    if (notes.length) return `${notes.length} notes indexed locally`;
    return 'Reading local vault…';
  }, [canvasError, notes.length, notesError, refreshError, refreshing]);

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
        <div className="refresh-lockup">
          <p className="index-status">{statusText}</p>
          <button className="refresh-button" type="button" onClick={() => void refresh()} disabled={refreshing}>
            {refreshing ? 'Refreshing…' : '↻ Refresh'}
          </button>
        </div>
      </header>

      {refreshError && (
        <div className="refresh-alert" role="alert">
          <strong>Refresh did not complete.</strong> {refreshError}
          <button type="button" onClick={() => void refresh()} disabled={refreshing}>Retry</button>
        </div>
      )}
      {!refreshError && lastRefreshAt && <p className="visually-hidden" role="status">Vault refreshed at {lastRefreshAt.toLocaleTimeString()}.</p>}

      <main className="workspace">
        <Sidebar
          notes={notes}
          error={notesError}
          onRetry={() => void refresh()}
          onOpen={openNote}
          selectedPath={selectedPath}
          query={query}
          onQueryChange={setQuery}
          selectedTag={selectedTag}
          onTagChange={updateSelectedTag}
          filterNotice={filterNotice}
        />
        <section className="board-region" aria-label="Home board region">
          <div className="region-heading">
            <div>
              <p className="eyebrow">Visual map</p>
              <h2>Home board</h2>
            </div>
            <span className="read-only-badge">Read only</span>
          </div>
          <Board canvas={canvas} canvasPath={canvasPath} selectedPath={selectedPath} onOpen={openNote} error={canvasError} onRetry={() => void refresh()} />
        </section>
        <NotePane
          state={reader}
          stale={Boolean(refreshError)}
          canGoBack={history.length > 0}
          onBack={goBack}
          onFollowLink={followLink}
          onOpenBacklink={openNote}
          onRetry={retryReader}
          onReturnToSource={returnToSource}
        />
      </main>
    </div>
  );
}

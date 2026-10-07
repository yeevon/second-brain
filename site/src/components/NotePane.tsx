import { Component, useEffect, useState, type ErrorInfo, type ReactNode } from 'react';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { decodeWikilink } from '../link-utils';
import type { NoteContext } from '../types';

type ReaderState =
  | { kind: 'empty' }
  | { kind: 'loading'; path: string }
  | ({ kind: 'note' } & NoteContext)
  | { kind: 'error'; message: string; path?: string; recovery?: { sourcePath: string; target: string } }
  | { kind: 'unresolved'; state: { status: string; target: string; message: string; candidates?: string[] } };

type Props = {
  state: ReaderState;
  stale: boolean;
  canGoBack: boolean;
  onBack: () => void;
  onFollowLink: (target: string) => void;
  onOpenBacklink: (path: string) => void;
  onRetry: () => void;
  onReturnToSource: () => void;
};

const encodeWikilinks = (markdown: string): string =>
  markdown.replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, (_match, target: string, alias?: string) => {
    const label = (alias || target).replaceAll('[', '\\[').replaceAll(']', '\\]');
    return `[${label}](wikilink:${encodeURIComponent(target)})`;
  });

class MarkdownBoundary extends Component<{ resetKey: string; children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(_error: Error, _info: ErrorInfo) {
    // The visible diagnostic below keeps the rest of the reader usable.
  }

  componentDidUpdate(previous: Readonly<{ resetKey: string; children: ReactNode }>) {
    if (previous.resetKey !== this.props.resetKey && this.state.failed) this.setState({ failed: false });
  }

  render() {
    if (this.state.failed) {
      return (
        <div className="inline-state error-state markdown-error" role="alert">
          <strong>Note content could not be rendered</strong>
          <p>The note remains unchanged. Select another note or correct the malformed Markdown in Obsidian.</p>
        </div>
      );
    }
    return this.props.children;
  }
}

export function NotePane({ state, stale, canGoBack, onBack, onFollowLink, onOpenBacklink, onRetry, onReturnToSource }: Props) {
  const note = state.kind === 'note' ? state.note : undefined;
  const context = state.kind === 'note' ? state : undefined;
  const [handoffStatus, setHandoffStatus] = useState('');

  useEffect(() => {
    setHandoffStatus('');
  }, [note?.path]);

  const copyPath = async () => {
    if (!note) return;
    try {
      await navigator.clipboard.writeText(note.path);
      setHandoffStatus('Vault-relative path copied.');
    } catch {
      setHandoffStatus('Copy was unavailable. Select the path below and copy it manually.');
    }
  };
  return (
    <article className="reader-pane" aria-label="Note reading pane">
      <div className="region-heading reader-heading">
        <div>
          <p className="eyebrow">Reading pane</p>
          <h2>{note?.title ?? 'Select a note'}</h2>
        </div>
        {canGoBack && <button className="back-button" type="button" onClick={onBack}>← Back</button>}
      </div>

      {state.kind === 'empty' && (
        <div className="reader-empty">
          <div className="reader-orbit" aria-hidden="true"><span>↗</span></div>
          <strong>Choose a card or note</strong>
          <p>Selected Markdown will appear here exactly as it lives in the local vault.</p>
        </div>
      )}
      {state.kind === 'loading' && <div className="reader-empty"><span className="spinner" />Opening {state.path}…</div>}
      {state.kind === 'error' && (
        <div className="inline-state error-state reader-error" role="alert">
          <strong>Note unavailable</strong>
          <p>{state.message}</p>
          <div className="error-actions">
            <button type="button" onClick={onRetry}>Retry</button>
            {state.recovery && <button type="button" className="secondary-action" onClick={onReturnToSource}>Return to source note</button>}
          </div>
        </div>
      )}
      {state.kind === 'unresolved' && (
        <div className="inline-state unresolved-state" role="alert">
          <span className="error-glyph" aria-hidden="true">?</span>
          <strong>{state.state.status === 'unsupported' ? 'Unsupported link target' : 'Unresolved note link'}</strong>
          <code>{state.state.target}</code>
          <p>{state.state.message}</p>
          {state.state.candidates?.length ? (
            <ul>{state.state.candidates.map((candidate) => <li key={candidate}>{candidate}</li>)}</ul>
          ) : null}
          <p className="recovery-copy">Use Back to return to the source note.</p>
        </div>
      )}
      {note && (
        <>
          {stale && <div className="stale-note" role="status">Stale content — the latest refresh failed. Your last loaded note is preserved.</div>}
          <div className="note-path">{note.path}</div>
          <div className="obsidian-actions">
            <a
              className="obsidian-button"
              href={context?.obsidian.uri}
              onClick={() => setHandoffStatus('Handoff requested. The browser cannot confirm whether Obsidian opened.')}
            >
              Open in Obsidian
            </a>
            <button type="button" className="copy-path-button" onClick={() => void copyPath()}>Copy path</button>
            <input
              className="copyable-path"
              aria-label="Vault-relative note path fallback"
              readOnly
              value={note.path}
              onFocus={(event) => event.currentTarget.select()}
            />
            {context?.obsidian.vaultPath && (
              <p className="obsidian-vault" title={context.obsidian.vaultPath}>
                Exact vault: <code>{context.obsidian.vaultPath}</code>
              </p>
            )}
            {handoffStatus && <p className="handoff-status" role="status">{handoffStatus}</p>}
          </div>
          <MarkdownBoundary resetKey={`${note.path}:${note.raw}`}>
            <div className="markdown-body">
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                urlTransform={(url) => (url.startsWith('wikilink:') ? url : defaultUrlTransform(url))}
                components={{
                  a: ({ href, children }) => {
                    if (href?.startsWith('wikilink:')) {
                      const decoded = decodeWikilink(href);
                      if (!decoded.ok) {
                        return <span className="invalid-wikilink" role="alert" title={decoded.target}>Invalid note link</span>;
                      }
                      return <button type="button" className="wikilink" onClick={() => onFollowLink(decoded.target)}>{children}</button>;
                    }
                    return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
                  },
                }}
              >
                {encodeWikilinks(note.body)}
              </ReactMarkdown>
            </div>
          </MarkdownBoundary>
          <section className="backlinks" aria-labelledby="backlinks-heading">
            <div className="backlinks-heading">
              <h3 id="backlinks-heading">Backlinks</h3>
              <span>{context?.backlinks.length ?? 0}</span>
            </div>
            {context?.backlinks.length ? (
              <ul>
                {context.backlinks.map((backlink) => (
                  <li key={backlink.path}>
                    <button type="button" onClick={() => onOpenBacklink(backlink.path)}>
                      <strong>{backlink.title}</strong>
                      <small>{backlink.path}</small>
                    </button>
                  </li>
                ))}
              </ul>
            ) : <p className="backlinks-empty">No eligible notes link to this note.</p>}
          </section>
        </>
      )}
    </article>
  );
}

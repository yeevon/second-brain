import { Component, type ErrorInfo, type ReactNode } from 'react';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { decodeWikilink } from '../link-utils';
import type { Note } from '../types';

type ReaderState =
  | { kind: 'empty' }
  | { kind: 'loading'; path: string }
  | { kind: 'note'; note: Note }
  | { kind: 'error'; message: string; path?: string; recovery?: { sourcePath: string; target: string } }
  | { kind: 'unresolved'; state: { status: string; target: string; message: string; candidates?: string[] } };

type Props = {
  state: ReaderState;
  canGoBack: boolean;
  onBack: () => void;
  onFollowLink: (target: string) => void;
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

export function NotePane({ state, canGoBack, onBack, onFollowLink, onRetry, onReturnToSource }: Props) {
  const note = state.kind === 'note' ? state.note : undefined;
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
          <div className="note-path">{note.path}</div>
          <MarkdownBoundary resetKey={note.path}>
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
        </>
      )}
    </article>
  );
}

import type { Note, NoteSummary, UnresolvedState, VaultCanvas } from './types';

const getJson = async <T>(url: string): Promise<T> => {
  const response = await fetch(url);
  const data = (await response.json()) as T & { error?: string };
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
};

export const fetchNotes = async (): Promise<NoteSummary[]> =>
  (await getJson<{ notes: NoteSummary[] }>('/api/notes')).notes;

export const fetchNote = async (notePath: string): Promise<Note> =>
  (await getJson<{ note: Note }>(`/api/note?path=${encodeURIComponent(notePath)}`)).note;

export const fetchCanvas = async (): Promise<{ canvas: VaultCanvas; path: string }> =>
  getJson<{ canvas: VaultCanvas; path: string }>('/api/canvas');

export const resolveLink = async (
  source: string,
  target: string,
): Promise<{ status: 'resolved'; path: string } | UnresolvedState> =>
  (
    await getJson<{ resolution: { status: 'resolved'; path: string } | UnresolvedState }>(
      `/api/resolve?source=${encodeURIComponent(source)}&target=${encodeURIComponent(target)}`,
    )
  ).resolution;

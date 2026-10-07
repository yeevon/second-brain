export type NoteSummary = {
  path: string;
  title: string;
  summary: string;
  body: string;
  tags: string[];
  diagnostics: string[];
};

export type Note = NoteSummary & {
  raw: string;
  metadata: Record<string, string>;
};

export type Backlink = {
  path: string;
  title: string;
};

export type ObsidianTarget = {
  uri: string;
  vault: string;
  vaultPath: string;
  path: string;
};

export type VaultCanvasNode = {
  id: string;
  type: string;
  file?: string;
  subpath?: string;
  text?: string;
  url?: string;
  x: number;
  y: number;
  width: number;
  height: number;
  color?: string;
  exists?: boolean;
};

export type VaultCanvasEdge = {
  id: string;
  fromNode: string;
  toNode: string;
  fromSide?: string;
  toSide?: string;
  fromEnd?: string;
  toEnd?: string;
  label?: string;
  color?: string;
};

export type VaultCanvas = { nodes: VaultCanvasNode[]; edges: VaultCanvasEdge[] };

export type NoteContext = {
  note: Note;
  backlinks: Backlink[];
  obsidian: ObsidianTarget;
};

export type RefreshSnapshot = {
  notes: NoteSummary[];
  notesError: string;
  canvas: VaultCanvas | null;
  canvasError: string;
  canvasPath: string;
  context: NoteContext | null;
  selectedError: string;
};

export type UnresolvedState = {
  status: 'missing' | 'ambiguous' | 'unsupported';
  target: string;
  message: string;
  candidates?: string[];
};

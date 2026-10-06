export type NoteSummary = {
  path: string;
  title: string;
  summary: string;
  body: string;
};

export type Note = NoteSummary & {
  raw: string;
  metadata: Record<string, string>;
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

export type UnresolvedState = {
  status: 'missing' | 'ambiguous' | 'unsupported';
  target: string;
  message: string;
  candidates?: string[];
};

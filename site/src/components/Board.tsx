import { memo, useCallback, useEffect, useMemo, useState } from 'react';
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react';
import { fetchCanvas } from '../api';
import type { VaultCanvasEdge, VaultCanvasNode } from '../types';

type Props = { selectedPath: string; onOpen: (path: string) => void };
type CardData = { source: VaultCanvasNode; label: string; selected: boolean; onOpen: (path: string) => void };

const colorClass: Record<string, string> = {
  '1': 'canvas-red',
  '2': 'canvas-orange',
  '3': 'canvas-yellow',
  '4': 'canvas-green',
  '5': 'canvas-blue',
  '6': 'canvas-purple',
};

const colorValue: Record<string, string> = {
  '1': '#d76b59',
  '2': '#d69a4a',
  '3': '#c6aa38',
  '4': '#5f9c6b',
  '5': '#5797a3',
  '6': '#8b6da6',
};

const canvasColor = (value: string | undefined, fallback: string): string => {
  if (value && /^#[0-9a-f]{6}$/i.test(value)) return value;
  return colorValue[value ?? ''] ?? fallback;
};

const sidePosition = {
  top: Position.Top,
  right: Position.Right,
  bottom: Position.Bottom,
  left: Position.Left,
} as const;

const edgeHandle = (side: string | undefined, fallback: keyof typeof sidePosition): keyof typeof sidePosition =>
  side && side in sidePosition ? side as keyof typeof sidePosition : fallback;

const adaptEdge = (edge: VaultCanvasEdge): Edge => {
  const stroke = canvasColor(edge.color, '#81786d');
  return {
    id: edge.id,
    source: edge.fromNode,
    target: edge.toNode,
    sourceHandle: edgeHandle(edge.fromSide, 'right'),
    targetHandle: edgeHandle(edge.toSide, 'left'),
    label: edge.label,
    type: 'smoothstep',
    className: 'canvas-edge',
    style: { stroke },
    markerStart: edge.fromEnd === 'arrow' ? { type: MarkerType.ArrowClosed, color: stroke } : undefined,
    markerEnd: edge.toEnd === 'none' ? undefined : { type: MarkerType.ArrowClosed, color: stroke },
    labelStyle: { fill: '#625d55', fontSize: 12, fontWeight: 650 },
    labelBgStyle: { fill: '#f4f0e8', fillOpacity: 0.94 },
    labelBgPadding: [6, 4],
    labelBgBorderRadius: 4,
  };
};

const CanvasCard = memo(({ data }: NodeProps<Node<CardData>>) => {
  const { source } = data;
  const unsupportedSubpath = source.type === 'file' && Boolean(source.file && source.subpath);
  const supported = source.type === 'file' && source.file && !unsupportedSubpath;
  const customColor = source.color && /^#[0-9a-f]{6}$/i.test(source.color) ? source.color : undefined;
  return (
    <button
      type="button"
      className={`canvas-card ${colorClass[source.color ?? ''] ?? ''} ${data.selected ? 'selected' : ''} ${source.exists === false ? 'missing' : ''} ${!supported ? 'unsupported' : ''}`}
      style={customColor ? { backgroundColor: customColor } : undefined}
      onClick={() => supported && data.onOpen(source.file!)}
      disabled={!supported}
      aria-label={supported ? `Open ${data.label}` : unsupportedSubpath ? `Unsupported subpath ${source.subpath}` : `Unsupported ${source.type} canvas node`}
    >
      {Object.entries(sidePosition).map(([side, position]) => (
        <span key={side}>
          <Handle id={side} type="target" position={position} className="card-handle" />
          <Handle id={side} type="source" position={position} className="card-handle" />
        </span>
      ))}
      <span className="card-kicker">{unsupportedSubpath ? 'Unsupported link target' : supported ? 'Note' : `Unsupported: ${source.type}`}</span>
      <strong>{data.label}</strong>
      <span className="card-path">{unsupportedSubpath ? `${source.file}${source.subpath}` : source.file ?? 'This node type is not available in the reader.'}</span>
      {source.exists === false && <span className="missing-label">Missing target</span>}
    </button>
  );
});

CanvasCard.displayName = 'CanvasCard';
const nodeTypes = { canvasCard: CanvasCard };

export function Board({ selectedPath, onOpen }: Props) {
  const [sourceNodes, setSourceNodes] = useState<VaultCanvasNode[]>([]);
  const [sourceEdges, setSourceEdges] = useState<Edge[]>([]);
  const [canvasPath, setCanvasPath] = useState('80 Canvases/Digital Jochi.canvas');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const result = await fetchCanvas();
      setCanvasPath(result.path);
      setSourceNodes(result.canvas.nodes);
      setSourceEdges(
        result.canvas.edges.map(adaptEdge),
      );
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'The home board could not be loaded.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const nodes = useMemo<Node<CardData>[]>(
    () =>
      sourceNodes.map((source) => ({
        id: source.id,
        type: 'canvasCard',
        position: { x: source.x, y: source.y },
        style: { width: source.width, height: source.height },
        draggable: false,
        selectable: true,
        data: {
          source,
          label: source.file ? source.file.split('/').at(-1)!.replace(/\.md$/i, '') : source.text?.slice(0, 60) || source.type,
          selected: source.file === selectedPath,
          onOpen,
        },
      })),
    [onOpen, selectedPath, sourceNodes],
  );

  if (loading) return <div className="board-state"><span className="spinner" />Reading the authored home canvas…</div>;
  if (error) {
    return (
      <div className="board-state board-error" role="alert">
        <span className="error-glyph" aria-hidden="true">!</span>
        <strong>Home board unavailable</strong>
        <p>{error}</p>
        <button type="button" onClick={load}>Retry board</button>
      </div>
    );
  }
  return (
    <div className="board-wrap">
      <ReactFlow
        nodes={nodes}
        edges={sourceEdges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.15, maxZoom: 0.75 }}
        minZoom={0.15}
        maxZoom={1.5}
        nodesDraggable={false}
        nodesConnectable={false}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={24} size={1.2} color="#c9c1b4" />
        <Controls showInteractive={false} position="bottom-right" />
      </ReactFlow>
      <div className="canvas-source" title={canvasPath}>{sourceNodes.length} cards · {sourceEdges.length} links</div>
    </div>
  );
}

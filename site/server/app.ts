import express, { type NextFunction, type Request, type Response } from 'express';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { VaultAccessError, VaultReader } from './vault-reader.js';

const moduleDirectory = path.dirname(fileURLToPath(import.meta.url));

export const createApi = (reader: VaultReader, canvasPath: string) => {
  const app = express();
  app.disable('x-powered-by');
  app.use(express.json({ limit: '16kb' }));

  app.get('/api/health', (_request, response) => {
    response.json({ ok: true, vault: reader.root, canvas: canvasPath });
  });
  app.get('/api/notes', async (_request, response, next) => {
    try {
      response.json({ notes: await reader.listNotes() });
    } catch (error) {
      next(error);
    }
  });
  app.get('/api/note', async (request, response, next) => {
    try {
      response.json({ note: await reader.readNote(String(request.query.path ?? '')) });
    } catch (error) {
      next(error);
    }
  });
  app.get('/api/context', async (request, response, next) => {
    try {
      const notePath = String(request.query.path ?? '');
      const notes = await reader.listNotes();
      const [note, backlinks, obsidian] = await Promise.all([
        reader.readNote(notePath),
        reader.listBacklinks(notePath, notes),
        reader.obsidianTarget(notePath),
      ]);
      response.json({ note, backlinks, obsidian });
    } catch (error) {
      next(error);
    }
  });
  app.get('/api/canvas', async (_request, response, next) => {
    try {
      response.json({ canvas: await reader.readCanvas(canvasPath), path: canvasPath });
    } catch (error) {
      next(error);
    }
  });
  app.get('/api/resolve', async (request, response, next) => {
    try {
      response.json({
        resolution: await reader.resolveLink(String(request.query.source ?? ''), String(request.query.target ?? '')),
      });
    } catch (error) {
      next(error);
    }
  });
  app.get('/api/refresh', async (request, response, next) => {
    try {
      const selectedPath = String(request.query.path ?? '');
      const [notesResult, canvasResult] = await Promise.allSettled([reader.listNotes(), reader.readCanvas(canvasPath)]);
      const notes = notesResult.status === 'fulfilled' ? notesResult.value : [];
      const canvas = canvasResult.status === 'fulfilled' ? canvasResult.value : null;
      const notesError = notesResult.status === 'rejected'
        ? notesResult.reason instanceof Error ? notesResult.reason.message : 'The vault notes are unavailable.'
        : '';
      const canvasError = canvasResult.status === 'rejected'
        ? canvasResult.reason instanceof Error ? canvasResult.reason.message : 'The home board is unavailable.'
        : '';
      let context = null;
      let selectedError = '';
      if (selectedPath && !notesError) {
        try {
          const [note, backlinks, obsidian] = await Promise.all([
            reader.readNote(selectedPath),
            reader.listBacklinks(selectedPath, notes),
            reader.obsidianTarget(selectedPath),
          ]);
          context = { note, backlinks, obsidian };
        } catch (error) {
          if (error instanceof VaultAccessError && error.status === 404) selectedError = error.message;
          else throw error;
        }
      } else if (selectedPath) {
        selectedError = notesError;
      }
      response.json({ notes, notesError, canvas, canvasError, canvasPath, context, selectedError });
    } catch (error) {
      next(error);
    }
  });

  app.use((error: unknown, _request: Request, response: Response, _next: NextFunction) => {
    const status = error instanceof VaultAccessError ? error.status : 500;
    const message = error instanceof Error ? error.message : 'The local reader encountered an unexpected error.';
    response.status(status).json({ error: message });
  });
  return app;
};

export const productionAssets = path.resolve(moduleDirectory, '..', 'dist');

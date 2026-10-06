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

  app.use((error: unknown, _request: Request, response: Response, _next: NextFunction) => {
    const status = error instanceof VaultAccessError ? error.status : 500;
    const message = error instanceof Error ? error.message : 'The local reader encountered an unexpected error.';
    response.status(status).json({ error: message });
  });
  return app;
};

export const productionAssets = path.resolve(moduleDirectory, '..', 'dist');

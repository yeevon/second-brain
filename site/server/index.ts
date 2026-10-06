import express from 'express';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createServer as createViteServer } from 'vite';
import { createApi, productionAssets } from './app.js';
import { findDefaultVault, VaultReader } from './vault-reader.js';

const host = '127.0.0.1';
const port = Number(process.env.PORT ?? 4173);
const canvasPath = process.env.DIGITAL_JOCHI_CANVAS ?? '80 Canvases/Digital Jochi.canvas';
const vaultPath = await findDefaultVault();
const app = express();

app.use(createApi(new VaultReader(vaultPath), canvasPath));

const isProduction = process.env.NODE_ENV === 'production' || fileURLToPath(import.meta.url).includes('dist-server');
if (isProduction) {
  app.use(express.static(productionAssets));
  app.get('{*path}', (_request, response) => response.sendFile(path.join(productionAssets, 'index.html')));
} else {
  const vite = await createViteServer({
    server: { middlewareMode: true },
    appType: 'spa',
  });
  app.use(vite.middlewares);
}

const server = app.listen(port, host, () => {
  console.log(`Digital Jochi is running at http://${host}:${port}`);
  console.log(`Vault: ${vaultPath}`);
  console.log(`Home canvas: ${canvasPath}`);
});

server.on('error', (error) => {
  console.error('Digital Jochi could not start:', error);
  process.exitCode = 1;
});

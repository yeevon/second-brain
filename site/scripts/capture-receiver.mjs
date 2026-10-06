import http from 'node:http';
import { promises as fs } from 'node:fs';
import path from 'node:path';

const host = '127.0.0.1';
const port = Number(process.env.CAPTURE_PORT ?? 4174);
const outputRoot = path.resolve(process.env.CAPTURE_OUTPUT ?? 'evidence/runtime');
await fs.mkdir(outputRoot, { recursive: true });

const server = http.createServer(async (request, response) => {
  if (request.method !== 'POST' || request.url !== '/capture') {
    response.writeHead(404).end('Not found');
    return;
  }
  const requestedName = String(request.headers['x-capture-name'] ?? 'capture.jpg');
  const name = path.basename(requestedName).replace(/[^a-zA-Z0-9._-]/g, '-');
  if (!name.toLowerCase().endsWith('.jpg')) {
    response.writeHead(400).end('JPEG filenames only');
    return;
  }
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 20 * 1024 * 1024) {
      response.writeHead(413).end('Capture too large');
      return;
    }
    chunks.push(chunk);
  }
  await fs.writeFile(path.join(outputRoot, name), Buffer.concat(chunks));
  response.writeHead(201, { 'content-type': 'text/plain' }).end(name);
});

server.listen(port, host, () => {
  console.log(`Capture receiver: http://${host}:${port}/capture`);
  console.log(`Output: ${outputRoot}`);
});

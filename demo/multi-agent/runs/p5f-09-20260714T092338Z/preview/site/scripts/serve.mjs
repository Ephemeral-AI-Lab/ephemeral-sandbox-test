import { createReadStream, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { extname, resolve, sep } from 'node:path';

const root = resolve(process.cwd());
const port = Number(process.env.PORT || 4173);
const host = process.env.HOST || '0.0.0.0';
const types = { '.css': 'text/css; charset=utf-8', '.html': 'text/html; charset=utf-8', '.js': 'application/javascript; charset=utf-8', '.json': 'application/json; charset=utf-8', '.svg': 'image/svg+xml' };
const server = createServer((request, response) => {
  const pathname = decodeURIComponent(new URL(request.url, 'http://flashcart.local').pathname);
  const file = resolve(root, pathname === '/' ? 'index.html' : '.' + pathname);
  if (!file.startsWith(root + sep) && file !== root) return response.writeHead(403).end('forbidden');
  try {
    if (!statSync(file).isFile()) throw new Error('not file');
    response.writeHead(200, { 'Content-Type': types[extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' });
    createReadStream(file).pipe(response);
  } catch { response.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' }).end('not found'); }
});
process.stdin.setEncoding('utf8');
process.stdin.on('data', (value) => {
  if (value.trim() === 'stop') server.close(() => process.exit(0));
});
server.listen(port, host, () => console.log(`FLASHCART_READY ${host}:${port}`));

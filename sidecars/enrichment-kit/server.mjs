// Starts the enrichment sidecar on 127.0.0.1 ONLY. Reads just the keys it needs from the
// project .env (see lib.mjs ALLOWED_ENV). Logs one line per request: method, path, status,
// duration - never bodies, emails, names or keys.
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createApp, loadKit, readConfig } from './lib.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const cfg = readConfig({ envFile: path.resolve(here, '..', '..', '.env') });
const kitInfo = loadKit(path.join(here, 'vendor', 'enrichment-kit'));
const app = createApp({ cfg, kitInfo });

const server = http.createServer((req, res) => {
  const started = Date.now();
  const chunks = [];
  let size = 0;
  req.on('data', (c) => {
    size += c.length;
    if (size <= 64 * 1024) chunks.push(c);
  });
  req.on('end', async () => {
    const [status, body] = await app.handle(req.method, req.url ?? '/', req.headers, Buffer.concat(chunks).toString('utf8'));
    res.writeHead(status, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(body));
    console.log(`${new Date().toISOString()} ${req.method} ${(req.url ?? '').split('?')[0]} ${status} ${Date.now() - started}ms`);
  });
});

// Default: loopback only. A container on a PRIVATE network sets ENRICHMENT_KIT_BIND_HOST=0.0.0.0
// (no port is published); that is refused unless a bearer token protects every route.
const bindHost = process.env.ENRICHMENT_KIT_BIND_HOST || '127.0.0.1';
if (bindHost !== '127.0.0.1' && !cfg.token) {
  console.error('refusing a non-loopback bind without ENRICHMENT_KIT_API_TOKEN');
  process.exit(1);
}
for (const signal of ['SIGTERM', 'SIGINT']) {
  process.on(signal, () => server.close(() => process.exit(0)));
}

server.listen(cfg.port, bindHost, () => {
  console.log(
    `enrichment sidecar on http://${bindHost}:${cfg.port} | enrichment-kit ${kitInfo.version}@${kitInfo.commit.slice(0, 10)} | ` +
      `hunter=${Boolean(cfg.hunterKey)}(+${cfg.hunterBackups.length} backup) apollo=${Boolean(cfg.apolloKey)}(+${cfg.apolloBackups.length} backup) auth=${Boolean(cfg.token)} callLimit=${cfg.maxProviderCalls}`,
  );
});

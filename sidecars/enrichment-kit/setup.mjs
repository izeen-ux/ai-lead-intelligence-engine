// One-time setup: clone enrichment-kit at a PINNED commit into ./vendor and build it.
// enrichment-kit is not published on npm and ships no dist/ in git, so it is built locally.
// Upstream code is not modified. Install scripts are disabled (--ignore-scripts).
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const UPSTREAM_REPO = 'https://github.com/masteranime/enrichment-kit.git';
export const UPSTREAM_COMMIT = '6998898c038a1f61c900aa8d96290b93049d0b84'; // tag v0.1.1, 2026-08-29

const here = path.dirname(fileURLToPath(import.meta.url));
const vendor = path.join(here, 'vendor', 'enrichment-kit');

function run(cmd, args, cwd) {
  console.log(`> ${cmd} ${args.join(' ')}`);
  // npm is a .cmd shim on Windows and needs a shell; its args never contain paths. git runs
  // without a shell so the project path (spaces, apostrophe) is never re-parsed.
  const shell = cmd === 'npm' && process.platform === 'win32';
  const [file, argv] = shell ? [`${cmd} ${args.join(' ')}`, []] : [cmd, args];
  return execFileSync(file, argv, { cwd, stdio: ['ignore', 'pipe', 'inherit'], shell })
    .toString()
    .trim();
}

if (!existsSync(path.join(vendor, '.git'))) {
  mkdirSync(path.dirname(vendor), { recursive: true });
  run('git', ['clone', '--quiet', UPSTREAM_REPO, vendor], here);
}
run('git', ['fetch', '--quiet', 'origin'], vendor);
run('git', ['checkout', '--quiet', '--detach', UPSTREAM_COMMIT], vendor);
const head = run('git', ['rev-parse', 'HEAD'], vendor);
if (head !== UPSTREAM_COMMIT) {
  throw new Error(`vendor HEAD ${head} != pinned ${UPSTREAM_COMMIT}`);
}
run('npm', ['install', '--no-audit', '--no-fund', '--ignore-scripts'], vendor);
// Library-only build. Upstream `npm run build` fails type-checking src/cli/index.ts (TS18047
// with TypeScript 5.9); the sidecar never uses the CLI, so it is excluded via a generated
// tsconfig (upstream files are not edited).
writeFileSync(
  path.join(vendor, 'tsconfig.sidecar.json'),
  JSON.stringify({ extends: './tsconfig.json', exclude: ['node_modules', 'dist', 'tests', 'examples', 'src/cli'] }, null, 2),
);
rmSync(path.join(vendor, 'dist'), { recursive: true, force: true });
run('npm', ['exec', '--', 'tsc', '-p', 'tsconfig.sidecar.json'], vendor);
const pkg = JSON.parse(readFileSync(path.join(vendor, 'package.json'), 'utf8'));
writeFileSync(
  path.join(here, 'vendor', 'INSTALLED.json'),
  JSON.stringify({ repo: UPSTREAM_REPO, commit: head, version: pkg.version, builtAt: new Date().toISOString() }, null, 2),
);
console.log(`enrichment-kit ${pkg.version} @ ${head.slice(0, 10)} built into vendor/enrichment-kit/dist`);

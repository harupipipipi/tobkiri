import { spawnSync } from 'node:child_process';

// The launcher is a client-only SPA and does not enable React Router's RSC
// action/server-action mode. Keep the latest patched 7.x release while the
// RSC-only advisory has no non-vulnerable stable release, but continue to fail
// on every other high or critical advisory.
const allowedAdvisories = new Set([
  'https://github.com/advisories/GHSA-qwww-vcr4-c8h2',
]);

const result = spawnSync('npm', ['audit', '--json'], {
  encoding: 'utf8',
  shell: process.platform === 'win32',
});

let report;
try {
  report = JSON.parse(result.stdout || '{}');
} catch {
  process.stderr.write(result.stdout || result.stderr || 'npm audit returned invalid JSON\n');
  process.exit(1);
}

const vulnerabilities = Object.values(report.vulnerabilities || {});
const directAdvisories = vulnerabilities.flatMap((entry) =>
  Array.isArray(entry.via) ? entry.via.filter((item) => item && typeof item === 'object') : [],
);
const blocking = directAdvisories.filter(
  (advisory) =>
    ['high', 'critical'].includes(String(advisory.severity || '').toLowerCase()) &&
    !allowedAdvisories.has(String(advisory.url || '')),
);

if (blocking.length > 0) {
  for (const advisory of blocking) {
    process.stderr.write(`${advisory.severity}: ${advisory.title} (${advisory.url})\n`);
  }
  process.exit(1);
}

for (const advisory of directAdvisories) {
  if (allowedAdvisories.has(String(advisory.url || ''))) {
    process.stdout.write(`allowed (not applicable to client-only SPA): ${advisory.url}\n`);
  }
}
process.stdout.write('No applicable high or critical vulnerabilities found.\n');

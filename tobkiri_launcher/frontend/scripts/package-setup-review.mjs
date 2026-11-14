import {createHash} from 'node:crypto';
import {readdir, readFile, writeFile} from 'node:fs/promises';

const output = new URL('../../setup-review-ui/', import.meta.url);
const index = await readFile(new URL('index.html', output), 'utf8');
const entry = /<script type="module" crossorigin src="(\.\/assets\/[A-Za-z0-9_-]+\.js)"><\/script>/g;
const stylesheet = /<link rel="stylesheet" crossorigin href="(\.\/assets\/[A-Za-z0-9_-]+\.css)">/g;
const csp = "default-src 'none'; script-src 'self'; style-src 'self'; base-uri 'none'; form-action 'none'; frame-src 'none'";
const [scripts, styles] = [[...index.matchAll(entry)], [...index.matchAll(stylesheet)]];
const assets = await readdir(new URL('assets/', output), {withFileTypes: true});
if (scripts.length !== 1 || styles.length !== 1 || assets.length !== 2
    || assets.some((asset) => !asset.isFile())
    || !index.includes(csp)) {
  throw new Error('Setup review build must contain one local script and stylesheet');
}

const [[scriptTag, scriptPath]] = scripts;
const [[styleTag, stylePath]] = styles;
const [script, style] = await Promise.all([
  readFile(new URL(scriptPath, output), 'utf8'),
  readFile(new URL(stylePath, output), 'utf8'),
]);
// The document bytes are consumed verbatim by WebKit. Reject inputs that could
// leave either raw-text element or whose newlines would be normalized by HTML.
if (/<\/script[\t\n\f />]|[\r\0]/i.test(script) || /<\/style[\t\n\f />]|[\r\0]/i.test(style)
    || Buffer.byteLength(script) + Buffer.byteLength(style) > 512 * 1024) {
  throw new Error('Setup review script or stylesheet cannot be embedded safely');
}

function hash(value) {
  return `'sha256-${createHash('sha256').update(value).digest('base64')}'`;
}

const document = index.replace(csp,
  `default-src 'none'; script-src ${hash(script)}; style-src ${hash(style)}; base-uri 'none'; form-action 'none'; frame-src 'none'`)
  .replace(scriptTag, () => `<script type="module">${script}</script>`)
  .replace(styleTag, () => `<style>${style}</style>`);
await writeFile(new URL('native.html', output), document);

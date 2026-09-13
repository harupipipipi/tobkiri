import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const WEBAPP_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const indexCss = readFileSync(path.join(WEBAPP_ROOT, "src/index.css"), "utf8");

test("defaultspack CSS does not import remote runtime fonts", () => {
  assert.doesNotMatch(indexCss, /fonts\.(?:googleapis|gstatic)\.com/i);
  assert.doesNotMatch(indexCss, /@import\s+url\(\s*["']?https?:/i);
});

test("chat structured content owns horizontal scrolling", () => {
  assert.match(indexCss, /\.rumi-message-content\s*\{[^}]*overflow-x:\s*auto;/s);
  assert.match(indexCss, /\.rumi-log-card-body\s*\{[^}]*overflow-x:\s*auto;[^}]*white-space:\s*pre;/s);
  assert.match(indexCss, /\.markdown-body pre\s*\{[^}]*overflow-x:\s*auto;[^}]*white-space:\s*pre;/s);
  assert.match(indexCss, /\.markdown-body pre code\s*\{[^}]*width:\s*max-content;[^}]*white-space:\s*pre;/s);
  assert.match(indexCss, /\.markdown-body table\s*\{[^}]*overflow-x:\s*auto;/s);
});

test("application shell owns the viewport while panes own scrolling", () => {
  assert.match(indexCss, /html,\s*body,\s*#root\s*\{[^}]*height:\s*100%;[^}]*min-height:\s*0;/s);
  assert.match(indexCss, /body\s*\{[^}]*overflow-x:\s*hidden;[^}]*overflow-y:\s*auto;/s);
  assert.match(indexCss, /\.rumi-app-shell\s*\{[^}]*height:\s*100dvh;[^}]*min-height:\s*0;[^}]*overflow:\s*hidden;/s);
  assert.match(
    indexCss,
    /\.rumi-composer-new \.rumi-composer-main-panel\s*\{[^}]*min-height:\s*clamp\(6\.75rem,\s*14dvh,\s*8\.25rem\)/s,
  );
  assert.match(
    indexCss,
    /\.rumi-new-chat-stage\s*\{[^}]*overflow-x:\s*clip;/s,
  );
  assert.match(
    indexCss,
    /@media\s*\(max-width:\s*1360px\)[\s\S]*?\.rumi-right-sidebar\s*\{[^}]*width:\s*auto;[\s\S]*?\.rumi-right-sidebar-panel\s*\{[^}]*position:\s*relative;[^}]*inset:\s*auto;[^}]*max-width:\s*min\(20rem,\s*34vw\)/s,
  );
  assert.match(
    indexCss,
    /@container\s*\(max-width:\s*560px\)[\s\S]*?\.rumi-composer-model-dock\s*\{[^}]*display:\s*flex;[\s\S]*?\.rumi-composer-model-dock \.rumi-composer-widget:not\(\[data-composer-widget="send"\]\)\s*\{[^}]*display:\s*none;/s,
  );
});

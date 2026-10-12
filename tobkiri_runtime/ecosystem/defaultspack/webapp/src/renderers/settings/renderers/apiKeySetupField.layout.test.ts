import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test, { after, before } from "node:test";
import { fileURLToPath } from "node:url";
import { compile } from "@tailwindcss/node";
import { chromium, type Browser } from "@playwright/test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { BuiltinApiKeySetupRenderer } from "./apiKeySetupField";

// Geometry-only renderer fixture: no application, credentials, or backend calls.
const webappRoot = fileURLToPath(new URL("../../../../", import.meta.url));
let browser: Browser;
let markup: string;
let css: string;

before(async () => {
  markup = renderToStaticMarkup(createElement(BuiltinApiKeySetupRenderer, {
    sectionId: "models",
    field: { id: "api_keys", label: "API接続", type: "api_key_setup", provider_id: "openai" },
    value: [],
    sectionValues: {},
    onChange: () => assert.fail("layout fixture cannot save settings"),
  }));
  const classes = [...new Set([...markup.matchAll(/class="([^"]+)"/g)]
    .flatMap((match) => match[1].split(/\s+/)))];
  const compiler = await compile(
    await readFile(path.join(webappRoot, "src/index.css"), "utf8"),
    { base: path.join(webappRoot, "src"), onDependency: () => undefined },
  );
  css = compiler.build(classes);
  browser = await chromium.launch({
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH?.trim() || undefined,
    headless: true,
  });
});

after(async () => { await browser?.close(); });

for (const [width, expectedColumns] of [[480, 1], [800, 2], [1040, 4], [1440, 4]]) {
  test(`API connection controls and Save fit a ${width}px window with a 190px sidebar`, async () => {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    try {
      await page.route("**/*", (route) => route.abort());
      await page.setContent(`<meta name="viewport" content="width=device-width, initial-scale=1"><style>${css}</style><div style="display:grid;grid-template-columns:190px minmax(0,1fr)"><aside>Settings</aside><main style="min-width:0;padding:24px">${markup}</main></div>`);
      const row = page.locator("[data-api-connection-fields]");
      // Long provider names and the approval-pending label also stay inside.
      await row.locator("[data-settings-renderer=provider_select] span.truncate").first()
        .evaluate((element) => { element.textContent = "Provider with a very long registered connection name"; });
      for (const saveLabel of ["Save", "承認・保存結果を確認中"]) {
        await row.locator("button").last().evaluate((button, label) => { button.textContent = label; }, saveLabel);
        const geometry = await row.evaluate((element) => {
          const rect = element.getBoundingClientRect();
          return {
            left: rect.left,
            right: rect.right,
            columns: getComputedStyle(element).gridTemplateColumns.split(" ").length,
            controls: [...element.children].map((child) => {
              const bounds = child.getBoundingClientRect();
              return { left: bounds.left, right: bounds.right, width: bounds.width };
            }),
            overflow: document.documentElement.scrollWidth > innerWidth,
          };
        });
        assert.equal(geometry.columns, expectedColumns, JSON.stringify(geometry));
        assert.equal(geometry.overflow, false, JSON.stringify(geometry));
        for (const control of geometry.controls) {
          assert.ok(control.width > 0, JSON.stringify(geometry));
          assert.ok(control.left >= geometry.left - 1, JSON.stringify(geometry));
          assert.ok(control.right <= geometry.right + 1, JSON.stringify(geometry));
        }
      }
    } finally {
      await page.close();
    }
  });
}

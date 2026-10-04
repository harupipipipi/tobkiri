import assert from "node:assert/strict";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import test, { after, before } from "node:test";
import { fileURLToPath } from "node:url";
import { compile } from "@tailwindcss/node";
import { chromium, type Browser, type Page } from "@playwright/test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ChatMessagesRenderer } from "./ChatMessagesRenderer";
import type { ChatUiMessage } from "./types";

// A standalone geometry check with real renderer markup and source CSS.
// It does not open the application, authenticate, or invoke a model.
const webappRoot = fileURLToPath(new URL("../../", import.meta.url));
const measurements: unknown[] = [];
let browser: Browser;

before(async () => {
  browser = await chromium.launch({
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH?.trim() || undefined,
    headless: true,
  });
});

after(async () => {
  await browser?.close();
  const outputPath = process.env.CHAT_LAYOUT_MEASUREMENTS_PATH;
  if (outputPath) {
    await writeFile(outputPath, `${JSON.stringify(measurements, null, 2)}\n`);
  }
});

function transcript(pending = false): ChatUiMessage[] {
  return [
    "Respond only with the following text: GUIDANCE_OK",
    "GUIDANCE_OK",
    "hello",
    "GUIDANCE_OK",
  ].map((text, index) => ({
    id: `message-${index}`,
    role: index % 2 ? "agent" : "user",
    content: [{ type: "text", text }],
    rawText: text,
    createdAt: Date.UTC(2026, 9, 4, 4, 20 + index),
    metadata: pending && index === 2 ? { deliveryState: "pending" } : undefined,
  }));
}

async function mount(page: Page, messages: ChatUiMessage[]): Promise<void> {
  const markup = renderToStaticMarkup(createElement(ChatMessagesRenderer, {
    error: null, isMessagesRegionVisible: true, isLoading: false,
    isNewConversation: false, isGenerating: false, messages,
    messagesEndRef: { current: null }, unknownBlockStrategy: "hidden",
    showActivityInMessages: true, showWidgets: true,
    onSuggestionClick: () => undefined,
  }));
  const classes = [...new Set([...markup.matchAll(/class="([^"]+)"/g)]
    .flatMap((match) => match[1].split(/\s+/)))];
  const compiler = await compile(
    await readFile(path.join(webappRoot, "src/index.css"), "utf8"),
    { base: path.join(webappRoot, "src"), onDependency: () => undefined },
  );
  await page.route("**/*", (route) => route.abort());
  await page.setContent(`<meta name="viewport" content="width=device-width, initial-scale=1"><style>${compiler.build(classes)}</style><main style="display:flex;flex-direction:column;height:900px">${markup}</main>`);
}

type LayoutRect = {
  left: number;
  right: number;
  top: number;
  bottom: number;
  height: number;
  width: number;
};

type LayoutGeometry = {
  column: LayoutRect;
  rows: Array<{ role: string; row: LayoutRect; bubble: LayoutRect; text: LayoutRect; actions: LayoutRect; copy: LayoutRect }>;
  overflow: boolean;
};

async function geometry(page: Page): Promise<LayoutGeometry> {
  // Keep this browser-realm function free of the tsx naming helper.
  return page.evaluate<LayoutGeometry>(String.raw`(() => {
    const rect = (element) => {
      const value = element.getBoundingClientRect();
      return { left: value.left, right: value.right, top: value.top, bottom: value.bottom, height: value.height, width: value.width };
    };
    const column = document.querySelector(".rumi-message-column");
    const rows = [...document.querySelectorAll(".rumi-message-row")].map((row) => ({
      role: row.getAttribute("data-message-role"),
      row: rect(row),
      bubble: rect(row.querySelector(".rumi-message-bubble")),
      text: rect(row.querySelector(".rumi-message-content")),
      actions: rect(row.querySelector(".rumi-message-actions")),
      copy: rect(row.querySelector('[data-copy-action="message"]')),
    }));
    return { column: rect(column), rows, overflow: document.documentElement.scrollWidth > window.innerWidth };
  })()`);
}

for (const device of [
  { name: "desktop", width: 1440, height: 900, hasTouch: false, isMobile: false, controlSize: 24 },
  { name: "touch", width: 390, height: 844, hasTouch: true, isMobile: true, controlSize: 32 },
]) {
  test(`${device.name} rows share their column edges with compact non-overlapping spacing`, async () => {
    const page = await browser.newPage({ viewport: device, hasTouch: device.hasTouch, isMobile: device.isMobile, reducedMotion: "reduce" });
    try {
      await mount(page, transcript());
      const measured = await geometry(page);
      const { rows, column } = measured;
      assert.equal(rows.length, 4);
      assert.equal(measured.overflow, false);
      for (const row of rows) {
        if (row.role === "user") assert.ok(Math.abs(row.bubble.right - column.right) < 1);
        else assert.ok(Math.abs(row.text.left - column.left) < 1);
        assert.equal(row.copy.height, device.controlSize);
        assert.equal(row.copy.width, device.controlSize);
      }
      for (let index = 0; index < rows.length - 1; index += 1) {
        assert.ok(rows[index].actions.bottom <= rows[index + 1].row.top);
      }
      const toolbarOpacity = await page.locator(".rumi-message-actions").first()
        .evaluate((element) => getComputedStyle(element).opacity);
      assert.equal(toolbarOpacity, device.hasTouch ? "1" : "0");
      const firstReplyGap = rows[1].text.top - rows[0].bubble.bottom;
      assert.ok(firstReplyGap >= 12 && firstReplyGap <= 48);
      measurements.push({ device: device.name, browser: browser.version(), viewport: { width: device.width, height: device.height }, ...measured, firstReplyGap });
    } finally {
      await page.close();
    }
  });
}

test("hover, keyboard focus, and a pending label preserve stable message positions", async () => {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, reducedMotion: "reduce" });
  try {
    const messages = transcript();
    // Consecutive rows also leave the toolbar clear of the next bubble.
    messages[1].role = "user";
    await mount(page, messages);
    const beforeHover = await geometry(page);
    await page.locator(".rumi-message-bubble").first().hover();
    await page.waitForFunction(() => getComputedStyle(document.querySelector(".rumi-message-actions")!).opacity === "1");
    assert.deepEqual(await geometry(page), beforeHover);
    await page.mouse.move(0, 0);
    await page.locator('[data-copy-action="message"]').first().focus();
    await page.waitForFunction(() => getComputedStyle(document.querySelector(".rumi-message-actions")!).opacity === "1");
    assert.deepEqual(await geometry(page), beforeHover);
    for (let index = 0; index < beforeHover.rows.length - 1; index += 1) {
      assert.ok(beforeHover.rows[index].actions.bottom <= beforeHover.rows[index + 1].bubble.top);
    }

    await mount(page, transcript(true));
    assert.equal(await page.locator('[data-chat-delivery-state="pending"]').count(), 1);
    assert.equal(await page.locator('[data-message-id="message-2"] .rumi-message-content').innerText(), "hello");
    const pending = await geometry(page);
    assert.ok(Math.abs(pending.rows[0].bubble.right - pending.rows[2].bubble.right) < 1);
    assert.ok(pending.rows[2].actions.bottom <= pending.rows[3].row.top);
    measurements.push({ device: "desktop-pending", browser: browser.version(), ...pending });
  } finally {
    await page.close();
  }
});

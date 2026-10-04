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

async function mount(
  page: Page,
  messages: ChatUiMessage[],
  { pauseAnimations = false }: { pauseAnimations?: boolean } = {},
): Promise<void> {
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
  const pausedAnimationStyle = pauseAnimations
    ? "<style>* { animation-play-state: paused !important; }</style>"
    : "";
  await page.setContent(`<meta name="viewport" content="width=device-width, initial-scale=1"><style>${compiler.build(classes)}</style>${pausedAnimationStyle}<main style="display:flex;flex-direction:column;height:900px">${markup}</main>`);
}

async function mountWelcomeStage(page: Page): Promise<void> {
  const markup = [
    '<section class="rumi-new-chat-stage">',
    '<h1 class="rumi-greeting">Welcome to Tobkiri</h1>',
    '<div class="rumi-composer-new">Message composer</div>',
    '</section>',
  ].join("");
  const classes = [...new Set([...markup.matchAll(/class="([^"]+)"/g)]
    .flatMap((match) => match[1].split(/\s+/)))];
  const compiler = await compile(
    await readFile(path.join(webappRoot, "src/index.css"), "utf8"),
    { base: path.join(webappRoot, "src"), onDependency: () => undefined },
  );
  await page.route("**/*", (route) => route.abort());
  await page.setContent(`<meta name="viewport" content="width=device-width, initial-scale=1"><style>${compiler.build(classes)}</style><style>* { animation-play-state: paused !important; }</style>${markup}`);
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

test("message rows remain paint-visible when browser animations are paused", async () => {
  const page = await browser.newPage({
    viewport: { width: 1440, height: 900 },
    reducedMotion: "no-preference",
  });
  try {
    await mount(page, transcript(), { pauseAnimations: true });
    const rows = await page.locator(".rumi-message-row").evaluateAll((elements) => (
      elements.map((element) => {
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return {
          opacity: style.opacity,
          text: element.textContent?.trim() ?? "",
          visibility: style.visibility,
          width: rect.width,
        };
      })
    ));
    assert.equal(rows.length, 4);
    for (const row of rows) {
      assert.equal(row.opacity, "1");
      assert.equal(row.visibility, "visible");
      assert.ok(row.width > 0);
      assert.ok(row.text.length > 0);
    }
  } finally {
    await page.close();
  }
});

test("welcome and its composer remain paint-visible when animation frames do not advance", async () => {
  const page = await browser.newPage({
    viewport: { width: 1440, height: 900 },
    reducedMotion: "no-preference",
  });
  try {
    await mountWelcomeStage(page);
    const mandatoryContent = await page.locator(
      ".rumi-new-chat-stage, .rumi-greeting, .rumi-composer-new",
    ).evaluateAll((elements) => elements.map((element) => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return {
        animationName: style.animationName,
        opacity: style.opacity,
        text: element.textContent?.trim() ?? "",
        visibility: style.visibility,
        width: rect.width,
      };
    }));
    assert.equal(mandatoryContent.length, 3);
    for (const element of mandatoryContent) {
      assert.equal(element.animationName, "none");
      assert.equal(element.opacity, "1");
      assert.equal(element.visibility, "visible");
      assert.ok(element.width > 0);
      assert.ok(element.text.length > 0);
    }
  } finally {
    await page.close();
  }
});

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

test("hover, keyboard focus, and inline delivery indicators preserve stable message positions", async () => {
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
    assert.equal(await page.locator('[data-chat-delivery-indicator="pending"]').count(), 1);
    assert.match(
      await page.locator('[data-message-id="message-2"] .rumi-message-content').innerText(),
      /^hello\s+送信中$/,
    );
    const inlineDeliveryGeometry = await page.locator('[data-message-id="message-2"] .rumi-message-content').evaluate((content) => {
      const body = content.querySelector(':scope > div')?.getBoundingClientRect();
      const indicator = content.querySelector('[data-chat-delivery-indicator]')?.getBoundingClientRect();
      if (!body || !indicator) throw new Error("pending delivery indicator geometry is missing");
      return {
        bodyRight: body.right,
        bodyBottom: body.bottom,
        indicatorLeft: indicator.left,
        indicatorBottom: indicator.bottom,
        indicatorHeight: indicator.height,
      };
    });
    assert.ok(inlineDeliveryGeometry.indicatorLeft >= inlineDeliveryGeometry.bodyRight);
    assert.ok(Math.abs(inlineDeliveryGeometry.indicatorBottom - inlineDeliveryGeometry.bodyBottom) < 1);
    assert.ok(inlineDeliveryGeometry.indicatorHeight <= 12);
    const pending = await geometry(page);
    assert.equal(pending.rows[2].bubble.height, beforeHover.rows[2].bubble.height);
    assert.ok(Math.abs(pending.rows[0].bubble.right - pending.rows[2].bubble.right) < 1);
    assert.ok(pending.rows[2].actions.bottom <= pending.rows[3].row.top);
    measurements.push({ device: "desktop-pending", browser: browser.version(), ...pending });
  } finally {
    await page.close();
  }
});

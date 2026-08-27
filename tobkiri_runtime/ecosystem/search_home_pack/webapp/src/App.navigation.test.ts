import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const appSource = readFileSync(new URL("./App.tsx", import.meta.url), "utf8");
const reviewSource = readFileSync(new URL("./NavigationReview.tsx", import.meta.url), "utf8");
const localeSource = readFileSync(new URL("./searchHomeLocale.ts", import.meta.url), "utf8");

test("Search Home never schedules automatic destination navigation", () => {
  assert.doesNotMatch(appSource, /AUTO_NAV_DELAY_MS/);
  assert.doesNotMatch(appSource, /scheduleNavigation/);
  assert.match(appSource, /<NavigationReview/);
  assert.match(reviewSource, /searchHomeCopy\.review\.guidance/);
});

test("Search Home validates every destination immediately before navigation", () => {
  assert.match(appSource, /reviewRouteDestination\(rawDestination\)/);
  assert.match(appSource, /window\.location\.assign\(destination\.url\)/);
  assert.doesNotMatch(appSource, /window\.location\.assign\(rawDestination\)/);
});

test("explicit unsafe URL input is blocked before resolver, answer, or Google search", () => {
  assert.match(appSource, /evaluateExplicitDestinationInput\(query\)/);
  assert.match(appSource, /explicitDestination\?\.verdict === "block"/);
  assert.match(appSource, /setInput\(""\)/);
  assert.match(appSource, /input_policy_blocked: true/);
});

test("route shortcuts are not installed globally and sensitive route data is not wildcard-posted", () => {
  assert.doesNotMatch(appSource, /routeHotkeyActionFromKeyboardEvent/);
  assert.doesNotMatch(appSource, /routeNavigationForHotkey/);
  assert.doesNotMatch(appSource, /postMessage\([\s\S]*?,\s*["']\*["']\s*\)/);
  assert.match(appSource, /window\.location\.origin/);
});

test("390px layout wraps the heading, input actions, and action descriptions", () => {
  const styles = readFileSync(new URL("./styles.css", import.meta.url), "utf8");
  const headingRules = [...styles.matchAll(/\.hero-header h1 \{([^}]*)\}/g)].map(
    ([, declarations]) => declarations,
  );
  const mobileHeadingRule = headingRules[headingRules.length - 1] ?? "";

  assert.equal(headingRules.length, 3);
  assert.doesNotMatch(headingRules.join("\n"), /font-size:\s*(?:clamp\(|[^;]*vw)/);
  assert.match(mobileHeadingRule, /font-size:\s*2\.25rem/);
  assert.match(mobileHeadingRule, /overflow:\s*visible/);
  assert.match(mobileHeadingRule, /overflow-wrap:\s*anywhere/);
  assert.match(mobileHeadingRule, /text-overflow:\s*clip/);
  assert.match(mobileHeadingRule, /white-space:\s*normal/);
  assert.match(styles, /\.search-row \{[\s\S]*?minmax\(0, 1fr\)/);
  assert.match(styles, /@media \(max-width: 640px\)[\s\S]*?\.action-row small \{[\s\S]*?white-space: normal/);
});

test("blocked destinations retain a safe copy-details action", () => {
  assert.match(reviewSource, /searchHomeCopy\.review\.copyBlocked/);
  assert.match(appSource, /searchHomeCopy\.review\.blockedClipboard/);
  assert.doesNotMatch(appSource, /blocked destination:.*destination\.input/);
});

test("AI answers are committed to explicit accessible memory-only result states", () => {
  assert.match(appSource, /normalizeAnswerResponse\(payload\)/);
  assert.match(appSource, /aria-labelledby="search-answer-title"/);
  assert.match(appSource, /searchHomeCopy\.answer\.privacyNote/);
  assert.match(appSource, /searchHomeCopy\.answer\.openConversation/);
  assert.match(appSource, /searchHomeCopy\.answer\.retry/);
  assert.doesNotMatch(appSource, /localStorage.*answer|sessionStorage.*answer/i);
  assert.equal(appSource.includes("dangerouslySet" + "InnerHTML"), false);
});

test("visible Search Home copy comes from the Japanese locale catalog", () => {
  for (const source of [appSource, reviewSource]) {
    assert.doesNotMatch(
      source,
      /Smart Resolve|AI Answer|Open Best URL|Default model|default routing|Working|Filter models|No matching models|defaultspack node/,
    );
  }
  assert.match(appSource, /searchHomeCopy\.productName/);
  assert.match(appSource, /SEARCH_HOME_ACTIONS/);
  assert.match(appSource, /model-technical-details/);
  assert.match(localeSource, /Tobkiri Search Home/);
  assert.match(localeSource, /技術情報/);
});

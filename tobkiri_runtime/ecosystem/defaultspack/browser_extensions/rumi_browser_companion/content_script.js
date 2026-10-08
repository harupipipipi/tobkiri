(function () {
  const ELEMENT_ATTR = "data-rumi-element-id";
  const HIGHLIGHT_LAYER_ID = "rumi-browser-companion-highlight-layer";
  const SEARCH_HOME_MESSAGE_SOURCE = "rumi-search-home";
  let sequence = 0;
  let highlightTimer = null;
  let searchHomeRouteStateExpiresAt = 0;

  refreshSearchHomeRouteState();
  window.addEventListener("focus", () => {
    refreshSearchHomeRouteState();
  });

  window.addEventListener("message", (event) => {
    if (event.source !== window) {
      return;
    }
    const message = event.data;
    if (!message || typeof message !== "object") {
      return;
    }
    if (message.type === "rumi:search-home:set-route-state" && message.source === SEARCH_HOME_MESSAGE_SOURCE && event.origin === window.location.origin) {
      searchHomeRouteStateExpiresAt = 0;
      const routeMessage = {
        type: "rumi:search-home:set-route-state",
        payload: message.payload || {},
        source_origin: event.origin
      };
      try {
        const maybePromise = chrome.runtime.sendMessage(routeMessage, updateSearchHomeRouteStateExpiry);
        if (maybePromise && typeof maybePromise.then === "function") {
          maybePromise.then(updateSearchHomeRouteStateExpiry).catch(() => {
            searchHomeRouteStateExpiresAt = 0;
          });
        }
      } catch (_error) {
        searchHomeRouteStateExpiresAt = 0;
      }
    }
  });

  function refreshSearchHomeRouteState() {
    try {
      const maybePromise = chrome.runtime.sendMessage(
        { type: "rumi:search-home:get-route-state" },
        (response) => {
          updateSearchHomeRouteStateExpiry(response);
        }
      );
      if (maybePromise && typeof maybePromise.then === "function") {
        maybePromise.then(updateSearchHomeRouteStateExpiry).catch(() => {});
      }
    } catch (_error) {
      searchHomeRouteStateExpiresAt = 0;
    }
  }

  function updateSearchHomeRouteStateExpiry(response) {
    if (!response || response.ok !== true || response.active !== true) {
      searchHomeRouteStateExpiresAt = 0;
      return;
    }
    const expiresAt = Number(response.expires_at);
    searchHomeRouteStateExpiresAt = Number.isFinite(expiresAt) ? expiresAt : 0;
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || !message.type) {
      return false;
    }

    (async () => {
      if (message.type === "rumi:dom-snapshot") {
        const viewport = buildViewportMetadata();
        const nodes = collectSnapshot(Number(message.maxNodes) || 300, message.options || message);
        const clientProfile = normalizeClientProfile(message.clientProfile || message.profile || {});
        sendResponse({
          ok: true,
          schema_id: "rumi.browser.semantic_dom_v2",
          schema_version: "semantic_dom_v2",
          url: location.href,
          title: document.title,
          viewport,
          snapshot_metadata: {
            source: "rumi_browser_companion",
            schema_id: "rumi.browser.semantic_dom_v2",
            schema_version: "semantic_dom_v2",
            captured_at: new Date().toISOString(),
            node_count: nodes.length,
            browser_profile_id: clientProfile.browser_profile_id || "",
            profile_label: clientProfile.profile_label || "",
            installation_id: clientProfile.installation_id || ""
          },
          client_profile: clientProfile,
          browser_profile_id: clientProfile.browser_profile_id || "",
          profile_label: clientProfile.profile_label || "",
          installation_id: clientProfile.installation_id || "",
          elements: nodes,
          nodes
        });
        return;
      }

      if (message.type === "rumi:element-command") {
        const result = await executeElementCommand(message.command || {});
        sendResponse({ ok: true, ...result });
        return;
      }

      sendResponse({ ok: false, error: `Unknown message type: ${message.type}` });
    })().catch((error) => {
      sendResponse({ ok: false, error: String(error && error.message ? error.message : error) });
    });

    return true;
  });

  function buildViewportMetadata() {
    return {
      width: window.innerWidth,
      height: window.innerHeight,
      scrollX: window.scrollX,
      scrollY: window.scrollY
    };
  }

  function normalizeClientProfile(value) {
    const profile = value && typeof value === "object" ? value : {};
    return {
      browser_profile_id: String(profile.browser_profile_id || ""),
      profile_label: String(profile.profile_label || ""),
      installation_id: String(profile.installation_id || ""),
      extension_id: String(profile.extension_id || ""),
      browser_name: String(profile.browser_name || ""),
      browser_version: String(profile.browser_version || "")
    };
  }

  function collectSnapshot(maxNodes, options) {
    const nodes = [];
    const roots = [document.body || document.documentElement];
    const includeHidden = Boolean(options && options.includeHidden);

    while (roots.length > 0 && nodes.length < maxNodes) {
      const root = roots.shift();
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);

      while (walker.nextNode() && nodes.length < maxNodes) {
        const element = walker.currentNode;
        if (!(element instanceof HTMLElement)) {
          continue;
        }
        if (element.shadowRoot) {
          roots.push(element.shadowRoot);
        }
        const rect = element.getBoundingClientRect();
        const computedStyle = window.getComputedStyle(element);
        const isVisible =
          rect.width > 0 &&
          rect.height > 0 &&
          computedStyle.visibility !== "hidden" &&
          computedStyle.display !== "none" &&
          Number(computedStyle.opacity || 1) > 0;
        if (!includeHidden && !isVisible) {
          continue;
        }

        const elementId = ensureElementId(element);
        const role = element.getAttribute("role") || inferRole(element);
        const labels = extractLabels(element);
        const text = extractAccessibleText(element, labels);
        const interactive = isInteractiveElement(element, role);
        const actionHints = buildActionHints(element, role, interactive);
        const geometry = buildGeometry(rect);
        const selectorHints = buildSelectorHints(element, role, text);

        nodes.push({
          index: nodes.length,
          element_id: elementId,
          semantic_id: buildSemanticId(element, role, text),
          tag: element.tagName.toLowerCase(),
          id: element.id || "",
          role: role || "",
          name: text,
          accessible_name: text,
          text,
          labels,
          nearby_text: extractNearbyText(element),
          rect: geometry.rect,
          viewport_center: geometry.viewport_center,
          viewport_coordinates: geometry.viewport_center,
          page_rect: geometry.page_rect,
          page_center: geometry.page_center,
          page_coordinates: geometry.page_center,
          is_visible: isVisible,
          is_in_viewport: isInViewport(rect),
          interactive,
          flags: {
            clickable: interactive.clickable,
            editable: interactive.editable,
            focusable: interactive.focusable,
            scrollable: interactive.scrollable
          },
          action_hints: actionHints,
          attributes: collectSafeAttributes(element),
          recognition_confidence: recognitionConfidence(element, role, text, labels, interactive),
          selector_hint: selectorHints[0] || buildSelectorHint(element),
          selector_hints: selectorHints,
          xpath_hint: buildXPathHint(element)
        });
      }
    }

    return nodes;
  }

  async function executeElementCommand(command) {
    const target = resolveTarget(command);
    if (target instanceof Element) {
      ensureElementId(target);
    }
    const action = String(command.action || command.type || "");
    switch (action) {
      case "page.click":
      ensureTarget(target, "click");
      focusElement(target);
      target.click();
      return { action: "click", element_id: ensureElementId(target) };
      case "page.type":
      ensureTarget(target, "type");
      return typeIntoElement(target, typedTextValue(command));
      case "page.press":
      return pressKeys(target, command);
      case "page.scroll":
      return scrollTarget(target, command);
      case "page.extract":
      ensureTarget(target, "extract");
      return extractFromElement(target, command);
      case "page.highlight":
      ensureTarget(target, "highlight");
      return highlightElement(target, command);
      case "page.clear_highlight":
      return clearHighlights();
      default:
        throw new Error(`Unsupported content command: ${action || command.type}`);
    }
  }

  function resolveTarget(command) {
    const action = String(command.action || command.type || "");
    if (command.element_id) {
      const found = document.querySelector(`[${ELEMENT_ATTR}="${cssEscape(command.element_id)}"]`);
      if (found) {
        return found;
      }
    }
    if (command.selector) {
      return document.querySelector(command.selector);
    }
    if (Array.isArray(command.selectors)) {
      for (const selector of command.selectors) {
        const found = querySelectorMaybe(selector);
        if (found) {
          return found;
        }
      }
    }
    if (isSemanticTargetAction(action)) {
      const semanticTarget = findSemanticTarget(command, action);
      if (semanticTarget) {
        return semanticTarget;
      }
    }
    if (action === "page.press") {
      return document.activeElement || document.body || document.documentElement;
    }
    if (action === "page.scroll") {
      return document.scrollingElement || document.documentElement || document.body;
    }
    if (action === "page.clear_highlight") {
      return document.body || document.documentElement;
    }
    return null;
  }

  function querySelectorMaybe(selector) {
    const value = String(selector || "").trim();
    if (!value) {
      return null;
    }
    try {
      return document.querySelector(value);
    } catch (_error) {
      return null;
    }
  }

  function isSemanticTargetAction(action) {
    return (
      action === "page.click" ||
      action === "page.type" ||
      action === "page.extract" ||
      action === "page.highlight"
    );
  }

  function findSemanticTarget(command, action) {
    const criteria = semanticTargetCriteria(command, action);
    if (!criteria.hasCriteria) {
      return null;
    }
    let best = null;
    let bestScore = -1;
    const candidates = Array.from(document.querySelectorAll("*"));
    for (const element of candidates) {
      if (!(element instanceof HTMLElement)) {
        continue;
      }
      const score = semanticTargetScore(element, criteria, action);
      if (score > bestScore || (score === bestScore && isBetterSemanticTarget(element, best, criteria, action))) {
        best = element;
        bestScore = score;
      }
    }
    return bestScore >= 0 ? best : null;
  }

  function semanticTargetCriteria(command, action) {
    const hasExplicitTypedValue =
      hasOwnValue(command, "value") ||
      hasOwnValue(command, "input_text") ||
      hasOwnValue(command, "inputText");
    const textQuery =
      firstString(command.text_query, command.textQuery) ||
      (action !== "page.type" || hasExplicitTypedValue ? firstString(command.text) : "");
    const criteria = {
      text_query: textQuery,
      accessible_name: firstString(command.accessible_name, command.accessibleName, command.name),
      role: firstString(command.role),
      semantic_id: firstString(command.semantic_id, command.semanticId),
      nearby_text: firstString(command.nearby_text, command.nearbyText)
    };
    criteria.hasCriteria = Boolean(
      criteria.text_query ||
        criteria.accessible_name ||
        criteria.role ||
        criteria.semantic_id ||
        criteria.nearby_text
    );
    return criteria;
  }

  function semanticTargetScore(element, criteria, action) {
    const rect = element.getBoundingClientRect();
    const computedStyle = window.getComputedStyle(element);
    const isVisible =
      rect.width > 0 &&
      rect.height > 0 &&
      computedStyle.visibility !== "hidden" &&
      computedStyle.display !== "none" &&
      Number(computedStyle.opacity || 1) > 0;
    if (!isVisible) {
      return -1;
    }

    const role = element.getAttribute("role") || inferRole(element);
    const labels = extractLabels(element);
    const accessibleName = extractAccessibleText(element, labels);
    const nearbyText = extractNearbyText(element);
    const semanticId = buildSemanticId(element, role, accessibleName);
    const visibleText = (element.innerText || element.textContent || "").replace(/\s+/g, " ").trim();
    const attributes = collectSafeAttributes(element);
    let score = 0;

    if (criteria.semantic_id) {
      if (semanticId !== criteria.semantic_id) {
        return -1;
      }
      score += 100;
    }
    if (criteria.role) {
      if (normalizeSearchText(role) !== normalizeSearchText(criteria.role)) {
        return -1;
      }
      score += 20;
    }
    if (criteria.accessible_name) {
      const match = textMatchScore([accessibleName, labels.join(" ")].join(" "), criteria.accessible_name);
      if (match <= 0) {
        return -1;
      }
      score += match + 30;
    }
    if (criteria.text_query) {
      const haystack = [
        accessibleName,
        visibleText,
        labels.join(" "),
        attributes.placeholder || "",
        attributes.title || "",
        "value" in element ? element.value : ""
      ].join(" ");
      const match = textMatchScore(haystack, criteria.text_query);
      if (match <= 0) {
        return -1;
      }
      score += match + 20;
    }
    if (criteria.nearby_text) {
      const match = textMatchScore(nearbyText, criteria.nearby_text);
      if (match <= 0) {
        return -1;
      }
      score += match + 15;
    }
    if (usesTextLikeSemanticCriteria(criteria) && isReadOnlySemanticTargetAction(action)) {
      score += semanticTargetSpecificityScore(element, criteria, {
        accessibleName,
        visibleText,
        rect
      });
    }

    const interactive = isInteractiveElement(element, role);
    if (action === "page.type" && interactive.editable) {
      score += 25;
    } else if (action === "page.click" && interactive.clickable) {
      score += 20;
    } else if (action === "page.highlight" && interactive.focusable) {
      score += 8;
    }
    if (isInViewport(rect)) {
      score += 6;
    }
    if (element.id || element.getAttribute("data-testid") || element.getAttribute("data-test") || element.getAttribute("name")) {
      score += 4;
    }
    return score;
  }

  function usesTextLikeSemanticCriteria(criteria) {
    return Boolean(criteria.text_query || criteria.accessible_name);
  }

  function isReadOnlySemanticTargetAction(action) {
    return action === "page.extract" || action === "page.highlight";
  }

  function semanticTargetSpecificityScore(element, criteria, context) {
    const targetText = criteria.accessible_name || criteria.text_query;
    const normalizedTarget = normalizeSearchText(targetText);
    if (!normalizedTarget) {
      return 0;
    }

    const accessibleName = normalizeSearchText(context.accessibleName);
    const visibleText = normalizeSearchText(context.visibleText);
    const directText = normalizeSearchText(directElementText(element));
    let score = 0;

    if (accessibleName === normalizedTarget) {
      score += 24;
    }
    if (directText === normalizedTarget) {
      score += 18;
    } else if (directText && directText.includes(normalizedTarget)) {
      score += 10;
    }
    if (visibleText === normalizedTarget) {
      score += 12;
    }
    if (!directText && element.children.length > 0 && visibleText.includes(normalizedTarget)) {
      score -= 8;
    }
    if (isBroadSemanticContainer(element, visibleText, normalizedTarget, context.rect)) {
      score -= 32;
    }
    return score;
  }

  function directElementText(element) {
    const values = [];
    for (const node of element.childNodes) {
      if (node.nodeType === Node.TEXT_NODE) {
        values.push(node.textContent || "");
      }
    }
    return values.join(" ").replace(/\s+/g, " ").trim();
  }

  function isBroadSemanticContainer(element, visibleText, targetText, rect) {
    const tag = element.tagName.toLowerCase();
    if (tag === "html" || tag === "body") {
      return true;
    }
    if (!element.children.length) {
      return false;
    }
    const containerTags = new Set(["div", "main", "section", "article", "form", "ul", "ol", "nav", "header", "footer", "aside"]);
    if (!containerTags.has(tag)) {
      return false;
    }
    const muchLongerText = visibleText.length > Math.max(targetText.length * 3, targetText.length + 80);
    const viewportArea = Math.max(1, window.innerWidth * window.innerHeight);
    const largeArea = rect.width * rect.height > viewportArea * 0.35;
    return muchLongerText || largeArea;
  }

  function isBetterSemanticTarget(candidate, current, criteria, action) {
    if (!current || !usesTextLikeSemanticCriteria(criteria) || !isReadOnlySemanticTargetAction(action)) {
      return false;
    }
    const candidateBroad = isBroadTieBreakTarget(candidate);
    const currentBroad = isBroadTieBreakTarget(current);
    if (candidateBroad !== currentBroad) {
      return !candidateBroad;
    }
    return semanticTargetArea(candidate) < semanticTargetArea(current);
  }

  function isBroadTieBreakTarget(element) {
    const tag = element.tagName.toLowerCase();
    return tag === "html" || tag === "body";
  }

  function semanticTargetArea(element) {
    const rect = element.getBoundingClientRect();
    return Math.max(0, rect.width) * Math.max(0, rect.height);
  }

  function ensureTarget(target, action) {
    if (!target) {
      throw new Error(`${action} target not found`);
    }
    if (!(target instanceof Element)) {
      throw new Error(`${action} target is not an element`);
    }
  }

  function focusElement(target) {
    if (target instanceof HTMLElement) {
      target.focus({ preventScroll: false });
    }
  }

  function typeIntoElement(target, text) {
    focusElement(target);
    if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement) {
      target.value = text;
    } else if (target instanceof HTMLSelectElement) {
      target.value = text;
    } else if (target instanceof HTMLElement && target.isContentEditable) {
      target.textContent = text;
    } else {
      throw new Error("type target is not editable");
    }
    target.dispatchEvent(new Event("input", { bubbles: true }));
    target.dispatchEvent(new Event("change", { bubbles: true }));
    return { action: "type", element_id: ensureElementId(target), text_length: text.length };
  }

  function typedTextValue(command) {
    if (hasOwnValue(command, "input_text")) {
      return String(command.input_text);
    }
    if (hasOwnValue(command, "inputText")) {
      return String(command.inputText);
    }
    if (hasOwnValue(command, "value")) {
      return String(command.value);
    }
    return String(command.text ?? "");
  }

  function hasOwnValue(object, key) {
    return Boolean(
      object &&
        Object.prototype.hasOwnProperty.call(object, key) &&
        object[key] !== undefined &&
        object[key] !== null
    );
  }

  function pressKeys(target, command) {
    const key = String(command.key || "Enter");
    const code = String(command.code || key);
    const repeat = Boolean(command.repeat);
    const modifiers = normalizeModifiers(command.modifiers);
    const dispatchTarget = target instanceof EventTarget ? target : document;
    const eventInit = {
      key,
      code,
      repeat,
      bubbles: true,
      cancelable: true,
      altKey: modifiers.altKey,
      ctrlKey: modifiers.ctrlKey,
      metaKey: modifiers.metaKey,
      shiftKey: modifiers.shiftKey
    };
    dispatchTarget.dispatchEvent(new KeyboardEvent("keydown", eventInit));
    dispatchTarget.dispatchEvent(new KeyboardEvent("keypress", eventInit));
    dispatchTarget.dispatchEvent(new KeyboardEvent("keyup", eventInit));
    return { action: "press", key, code, modifiers };
  }

  function scrollTarget(target, command) {
    const behavior = command.behavior === "smooth" ? "smooth" : "auto";
    const top = Number(command.top);
    const left = Number(command.left);
    const deltaX = Number(command.delta_x);
    const deltaY = Number(command.delta_y);
    const direction = String(command.direction || "").toLowerCase();
    const amount = Number(command.amount);
    const options = {
      behavior
    };

    if (Number.isFinite(top) || Number.isFinite(left)) {
      if (Number.isFinite(top)) {
        options.top = top;
      }
      if (Number.isFinite(left)) {
        options.left = left;
      }
    } else if (Number.isFinite(deltaX) || Number.isFinite(deltaY)) {
      options.top = Number.isFinite(deltaY) ? deltaY : 0;
      options.left = Number.isFinite(deltaX) ? deltaX : 0;
    } else {
      const step = Number.isFinite(amount) && amount !== 0 ? amount : 600;
      if (direction === "left") {
        options.left = -step;
      } else if (direction === "right") {
        options.left = step;
      } else if (direction === "up") {
        options.top = -step;
      } else {
        options.top = step;
      }
    }

    if (target instanceof Element && typeof target.scrollBy === "function" && command.element_id) {
      if (Number.isFinite(top) || Number.isFinite(left)) {
        target.scrollTo(options);
      } else {
        target.scrollBy(options);
      }
    } else {
      if (Number.isFinite(top) || Number.isFinite(left)) {
        window.scrollTo(options);
      } else {
        window.scrollBy(options);
      }
    }

    return {
      action: "scroll",
      target: command.element_id || "window",
      scrollX: window.scrollX,
      scrollY: window.scrollY
    };
  }

  function extractFromElement(target, command) {
    const mode = String(command.mode || "text");
    const elementId = ensureElementId(target);
    if (mode === "html") {
      return { action: "extract", element_id: elementId, mode, value: target.outerHTML };
    }
    if (mode === "value") {
      const value = "value" in target ? target.value : "";
      return { action: "extract", element_id: elementId, mode, value };
    }
    if (mode === "attributes") {
      const attributes = {};
      const allowedNames = Array.isArray(command.attribute_names)
        ? new Set(command.attribute_names.map((name) => String(name)))
        : null;
      for (const attribute of target.attributes) {
        if (allowedNames && !allowedNames.has(attribute.name)) {
          continue;
        }
        attributes[attribute.name] = attribute.value;
      }
      return { action: "extract", element_id: elementId, mode, value: attributes };
    }
    return { action: "extract", element_id: elementId, mode: "text", value: extractAccessibleText(target) };
  }

  function ensureElementId(element) {
    let existing = element.getAttribute(ELEMENT_ATTR);
    if (existing) {
      return existing;
    }
    sequence += 1;
    existing = `rumi-el-${Date.now().toString(36)}-${sequence.toString(36)}`;
    element.setAttribute(ELEMENT_ATTR, existing);
    return existing;
  }

  function inferRole(element) {
    const tag = element.tagName.toLowerCase();
    if (tag === "a" && element.getAttribute("href")) {
      return "link";
    }
    if (tag === "button") {
      return "button";
    }
    if (tag === "input") {
      const type = (element.getAttribute("type") || "text").toLowerCase();
      return type === "checkbox" ? "checkbox" : type === "radio" ? "radio" : "textbox";
    }
    if (tag === "textarea") {
      return "textbox";
    }
    if (tag === "select") {
      return "combobox";
    }
    return "";
  }

  function buildGeometry(rect) {
    const viewportCenter = {
      x: round2(rect.x + rect.width / 2),
      y: round2(rect.y + rect.height / 2)
    };
    const pageRect = {
      x: round2(rect.x + window.scrollX),
      y: round2(rect.y + window.scrollY),
      width: round2(rect.width),
      height: round2(rect.height)
    };
    const pageCenter = {
      x: round2(pageRect.x + pageRect.width / 2),
      y: round2(pageRect.y + pageRect.height / 2)
    };
    return {
      rect: {
        x: round2(rect.x),
        y: round2(rect.y),
        width: round2(rect.width),
        height: round2(rect.height)
      },
      viewport_center: viewportCenter,
      page_rect: pageRect,
      page_center: pageCenter
    };
  }

  function extractAccessibleText(element, labels) {
    const ariaLabel = element.getAttribute("aria-label");
    const alt = element.getAttribute("alt");
    const placeholder = element.getAttribute("placeholder");
    const value = "value" in element ? element.value : "";
    const labelText = Array.isArray(labels) ? labels.join(" ") : "";
    const text = (element.innerText || element.textContent || "").replace(/\s+/g, " ").trim();
    return (ariaLabel || labelText || alt || placeholder || value || text || "").slice(0, 500);
  }

  function extractLabels(element) {
    const labels = [];
    const ariaLabel = element.getAttribute("aria-label");
    if (ariaLabel) {
      labels.push(ariaLabel);
    }
    const labelledBy = element.getAttribute("aria-labelledby");
    if (labelledBy) {
      for (const id of labelledBy.split(/\s+/)) {
        const ref = document.getElementById(id);
        if (ref) {
          labels.push(ref.innerText || ref.textContent || "");
        }
      }
    }
    if ("labels" in element && element.labels) {
      for (const label of element.labels) {
        labels.push(label.innerText || label.textContent || "");
      }
    }
    if (element.id) {
      const explicit = document.querySelector(`label[for="${cssEscape(element.id)}"]`);
      if (explicit) {
        labels.push(explicit.innerText || explicit.textContent || "");
      }
    }
    const wrappingLabel = element.closest("label");
    if (wrappingLabel) {
      labels.push(wrappingLabel.innerText || wrappingLabel.textContent || "");
    }
    return uniqueCleanText(labels, 120).slice(0, 6);
  }

  function extractNearbyText(element) {
    const candidates = [];
    const previous = element.previousElementSibling;
    const parent = element.parentElement;
    if (previous) {
      candidates.push(previous.innerText || previous.textContent || "");
    }
    if (parent) {
      candidates.push(parent.innerText || parent.textContent || "");
    }
    return uniqueCleanText(candidates, 240).join(" | ").slice(0, 500);
  }

  function isInteractiveElement(element, role) {
    const tag = element.tagName.toLowerCase();
    const tabindex = element.getAttribute("tabindex");
    const editable = Boolean(
      element instanceof HTMLInputElement ||
        element instanceof HTMLTextAreaElement ||
        element instanceof HTMLSelectElement ||
        element.isContentEditable
    );
    const clickableRoles = new Set(["button", "link", "checkbox", "menuitem", "option", "radio", "switch", "tab"]);
    const clickableTags = new Set(["a", "button", "input", "label", "option", "select", "summary", "textarea"]);
    const clickable = clickableRoles.has(role) || clickableTags.has(tag) || typeof element.onclick === "function";
    const focusable =
      editable ||
      clickable ||
      tabindex !== null ||
      element.matches("audio[controls], video[controls], iframe");
    const scrollable = element.scrollHeight > element.clientHeight || element.scrollWidth > element.clientWidth;
    return {
      clickable,
      editable,
      focusable,
      scrollable
    };
  }

  function buildSelectorHints(element, role, text) {
    const hints = [];
    const testId = element.getAttribute("data-testid");
    if (testId) {
      hints.push(`[data-testid="${cssStringEscape(testId)}"]`);
    }
    const dataTest = element.getAttribute("data-test");
    if (dataTest) {
      hints.push(`[data-test="${cssStringEscape(dataTest)}"]`);
    }
    if (element.id) {
      hints.push(`#${cssEscape(element.id)}`);
    }
    const name = element.getAttribute("name");
    if (name) {
      hints.push(`${element.tagName.toLowerCase()}[name="${cssStringEscape(name)}"]`);
    }
    const ariaLabel = element.getAttribute("aria-label");
    if (ariaLabel) {
      hints.push(`${element.tagName.toLowerCase()}[aria-label="${cssStringEscape(ariaLabel)}"]`);
    }
    if (role && text) {
      hints.push(`[role="${cssStringEscape(role)}"]`);
    }
    hints.push(buildSelectorHint(element));
    return Array.from(new Set(hints.filter(Boolean))).slice(0, 8);
  }

  function buildSelectorHint(element) {
    if (element.id) {
      return `#${cssEscape(element.id)}`;
    }
    const parts = [];
    let current = element;
    while (current instanceof Element && parts.length < 4) {
      let part = current.tagName.toLowerCase();
      if (current.classList.length > 0) {
        part += `.${Array.from(current.classList).slice(0, 2).join(".")}`;
      }
      const parent = current.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter((child) => child.tagName === current.tagName);
        if (siblings.length > 1) {
          part += `:nth-of-type(${siblings.indexOf(current) + 1})`;
        }
      }
      parts.unshift(part);
      current = current.parentElement;
    }
    return parts.join(" > ");
  }

  function buildXPathHint(element) {
    const parts = [];
    let current = element;
    while (current instanceof Element && current.nodeType === Node.ELEMENT_NODE && parts.length < 6) {
      let index = 1;
      let sibling = current.previousElementSibling;
      while (sibling) {
        if (sibling.tagName === current.tagName) {
          index += 1;
        }
        sibling = sibling.previousElementSibling;
      }
      parts.unshift(`${current.tagName.toLowerCase()}[${index}]`);
      current = current.parentElement;
    }
    return parts.length ? `/${parts.join("/")}` : "";
  }

  function buildSemanticId(element, role, text) {
    const tag = element.tagName.toLowerCase();
    const stable =
      element.id ||
      element.getAttribute("data-testid") ||
      element.getAttribute("data-test") ||
      element.getAttribute("name") ||
      text ||
      "";
    return [tag, role || "node", slugify(stable).slice(0, 48)].filter(Boolean).join(":");
  }

  function collectSafeAttributes(element) {
    const allowed = [
      "aria-label",
      "aria-labelledby",
      "data-testid",
      "data-test",
      "href",
      "name",
      "placeholder",
      "role",
      "title",
      "type"
    ];
    const attributes = {};
    for (const name of allowed) {
      const value = element.getAttribute(name);
      if (!value) {
        continue;
      }
      attributes[name] = String(value).slice(0, 240);
    }
    return attributes;
  }

  function buildActionHints(element, role, interactive) {
    const hints = ["extract"];
    if (interactive.clickable) {
      hints.push("click");
    }
    if (interactive.editable) {
      hints.push(element instanceof HTMLSelectElement ? "select" : "type");
    }
    if (interactive.focusable) {
      hints.push("press");
    }
    if (interactive.scrollable) {
      hints.push("scroll");
    }
    if (role === "link") {
      hints.push("open");
    }
    return Array.from(new Set(hints));
  }

  function recognitionConfidence(element, role, text, labels, interactive) {
    let score = 0.25;
    if (interactive.clickable || interactive.editable || interactive.focusable) {
      score += 0.25;
    }
    if (role) {
      score += 0.15;
    }
    if (text) {
      score += 0.15;
    }
    if (labels.length > 0) {
      score += 0.15;
    }
    if (element.id || element.getAttribute("data-testid") || element.getAttribute("name")) {
      score += 0.05;
    }
    return round2(Math.min(score, 0.99));
  }

  function isInViewport(rect) {
    return rect.bottom >= 0 && rect.right >= 0 && rect.top <= window.innerHeight && rect.left <= window.innerWidth;
  }

  function highlightElement(target, command) {
    const elementId = ensureElementId(target);
    const rect = target.getBoundingClientRect();
    const layer = ensureHighlightLayer(Boolean(command.clear_existing ?? true));
    const color = String(command.color || "#2563eb");
    const label = String(command.label || elementId || "Rumi").slice(0, 80);
    const overlay = document.createElement("div");
    overlay.style.position = "fixed";
    overlay.style.left = `${rect.left}px`;
    overlay.style.top = `${rect.top}px`;
    overlay.style.width = `${rect.width}px`;
    overlay.style.height = `${rect.height}px`;
    overlay.style.border = `2px solid ${color}`;
    overlay.style.boxShadow = `0 0 0 3px ${hexToRgba(color, 0.18)}`;
    overlay.style.borderRadius = "6px";
    overlay.style.pointerEvents = "none";
    overlay.style.zIndex = "2147483647";

    const badge = document.createElement("div");
    badge.textContent = label;
    badge.style.position = "absolute";
    badge.style.left = "0";
    badge.style.top = "-22px";
    badge.style.maxWidth = "320px";
    badge.style.overflow = "hidden";
    badge.style.textOverflow = "ellipsis";
    badge.style.whiteSpace = "nowrap";
    badge.style.background = color;
    badge.style.color = "white";
    badge.style.font = "12px/18px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
    badge.style.padding = "1px 6px";
    badge.style.borderRadius = "4px";
    overlay.appendChild(badge);
    layer.appendChild(overlay);

    const durationMs = Number(command.duration_ms);
    if (Number.isFinite(durationMs) && durationMs > 0) {
      if (highlightTimer) {
        clearTimeout(highlightTimer);
      }
      highlightTimer = setTimeout(() => clearHighlights(), Math.min(durationMs, 30000));
    }

    return {
      action: "highlight",
      element_id: elementId,
      rect: {
        x: round2(rect.x),
        y: round2(rect.y),
        width: round2(rect.width),
        height: round2(rect.height)
      }
    };
  }

  function ensureHighlightLayer(clearExisting) {
    let layer = document.getElementById(HIGHLIGHT_LAYER_ID);
    if (!layer) {
      layer = document.createElement("div");
      layer.id = HIGHLIGHT_LAYER_ID;
      layer.style.position = "fixed";
      layer.style.inset = "0";
      layer.style.pointerEvents = "none";
      layer.style.zIndex = "2147483647";
      document.documentElement.appendChild(layer);
    }
    if (clearExisting) {
      layer.textContent = "";
    }
    return layer;
  }

  function clearHighlights() {
    if (highlightTimer) {
      clearTimeout(highlightTimer);
      highlightTimer = null;
    }
    const layer = document.getElementById(HIGHLIGHT_LAYER_ID);
    if (layer) {
      layer.remove();
    }
    return { action: "clear_highlight" };
  }

  function uniqueCleanText(values, maxLength) {
    const seen = new Set();
    const result = [];
    for (const value of values) {
      const text = String(value || "").replace(/\s+/g, " ").trim().slice(0, maxLength);
      if (!text || seen.has(text)) {
        continue;
      }
      seen.add(text);
      result.push(text);
    }
    return result;
  }

  function firstString(...values) {
    for (const value of values) {
      if (value === undefined || value === null) {
        continue;
      }
      const text = String(value).trim();
      if (text) {
        return text;
      }
    }
    return "";
  }

  function normalizeSearchText(value) {
    return String(value || "")
      .replace(/\s+/g, " ")
      .trim()
      .toLowerCase();
  }

  function textMatchScore(haystack, needle) {
    const hay = normalizeSearchText(haystack);
    const need = normalizeSearchText(needle);
    if (!hay || !need) {
      return 0;
    }
    if (hay === need) {
      return 40;
    }
    if (hay.includes(need)) {
      return 30;
    }
    if (need.includes(hay) && hay.length >= 3) {
      return 18;
    }
    const tokens = need.split(" ").filter(Boolean);
    if (tokens.length > 0 && tokens.every((token) => hay.includes(token))) {
      return 14;
    }
    return 0;
  }

  function slugify(value) {
    return String(value || "")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "-")
      .replace(/^-+|-+$/g, "");
  }

  function hexToRgba(value, alpha) {
    const match = /^#?([a-f0-9]{6})$/i.exec(value);
    if (!match) {
      return `rgba(37, 99, 235, ${alpha})`;
    }
    const intValue = parseInt(match[1], 16);
    const r = (intValue >> 16) & 255;
    const g = (intValue >> 8) & 255;
    const b = intValue & 255;
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }

  function normalizeModifiers(modifiers) {
    const list = Array.isArray(modifiers) ? modifiers.map((item) => String(item).toLowerCase()) : [];
    return {
      altKey: list.includes("alt"),
      ctrlKey: list.includes("ctrl") || list.includes("control"),
      metaKey: list.includes("meta") || list.includes("cmd") || list.includes("command"),
      shiftKey: list.includes("shift")
    };
  }

  function cssEscape(value) {
    if (window.CSS && typeof window.CSS.escape === "function") {
      return window.CSS.escape(value);
    }
    return String(value).replace(/["\\]/g, "\\$&");
  }

  function cssStringEscape(value) {
    return String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  }

  function round2(value) {
    return Math.round(value * 100) / 100;
  }
})();

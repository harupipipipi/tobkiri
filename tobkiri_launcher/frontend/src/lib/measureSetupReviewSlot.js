// This fixed expression is evaluated by the native owner in the parent page.
// The result is presentation geometry. It contains no listing or input event.
(() => {
  if (window.location.pathname !== '/panel/setup' || document.visibilityState !== 'visible') {
    return null;
  }
  const viewport = window.visualViewport;
  if (!viewport || viewport.scale !== 1 || viewport.offsetLeft !== 0 || viewport.offsetTop !== 0) {
    return null;
  }
  const slots = document.querySelectorAll('[data-native-setup-review-slot]');
  if (slots.length !== 1 || document.querySelector('[aria-modal="true"], dialog[open]')) {
    return null;
  }
  const slot = slots[0];
  const {left, top, right, bottom} = slot.getBoundingClientRect();
  const full = {left, top, right, bottom};
  const clip = {
    left: Math.max(0, left),
    top: Math.max(0, top),
    right: Math.min(document.documentElement.clientWidth, right),
    bottom: Math.min(document.documentElement.clientHeight, bottom),
  };
  for (let node = slot; node; node = node.parentElement) {
    const style = window.getComputedStyle(node);
    if (style.visibility !== 'visible' || Number(style.opacity || 1) === 0 || style.display === 'none') {
      return null;
    }
    if (node === slot || node === document.documentElement || node === document.body) continue;
    const box = node.getBoundingClientRect();
    const x = box.left + node.clientLeft;
    const y = box.top + node.clientTop;
    if (['auto', 'scroll', 'hidden', 'clip'].includes(style.overflowX)) {
      clip.left = Math.max(clip.left, x);
      clip.right = Math.min(clip.right, x + node.clientWidth);
    }
    if (['auto', 'scroll', 'hidden', 'clip'].includes(style.overflowY)) {
      clip.top = Math.max(clip.top, y);
      clip.bottom = Math.min(clip.bottom, y + node.clientHeight);
    }
  }
  if (clip.left >= clip.right || clip.top >= clip.bottom) return null;
  return JSON.stringify({
    full,
    clip,
    viewportWidth: window.innerWidth,
    viewportHeight: window.innerHeight,
  });
})()

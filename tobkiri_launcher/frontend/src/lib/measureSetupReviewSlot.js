// Run in the owner's isolated JavaScript world in the parent main frame.
// The result identifies a presentation anchor and its geometry.
(() => {
  if (window.location.pathname !== '/panel/setup') {
    globalThis.tobkiriSetupReviewAnchor = undefined;
    return null;
  }
  const anchors = document.querySelectorAll('[data-native-setup-review-anchor]');
  if (anchors.length !== 1) {
    globalThis.tobkiriSetupReviewAnchor = undefined;
    return null;
  }
  const anchor = anchors[0];
  const slot = anchor.parentElement;
  const confirmation = anchor.getAttribute('data-native-setup-review-anchor');
  if (!slot || !/^sha256:[a-f0-9]{64}$/.test(confirmation)) {
    globalThis.tobkiriSetupReviewAnchor = undefined;
    return null;
  }
  const previous = globalThis.tobkiriSetupReviewAnchor;
  if (previous?.element !== anchor || previous.confirmation !== confirmation) {
    const random = crypto.getRandomValues(new Uint8Array(16));
    const id = Array.from(random, (byte) => byte.toString(16).padStart(2, '0')).join('');
    globalThis.tobkiriSetupReviewAnchor = {element: anchor, confirmation, id};
  }
  const sample = {anchorId: globalThis.tobkiriSetupReviewAnchor.id, confirmation, geometry: null};
  const viewport = window.visualViewport;
  if (document.visibilityState !== 'visible'
      || !viewport || viewport.scale !== 1 || viewport.offsetLeft !== 0 || viewport.offsetTop !== 0
      || document.querySelector('[aria-modal="true"], dialog[open]')) {
    return JSON.stringify(sample);
  }
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
      return JSON.stringify(sample);
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
  if (clip.left < clip.right && clip.top < clip.bottom) {
    sample.geometry = {full, clip, viewportWidth: window.innerWidth, viewportHeight: window.innerHeight};
  }
  return JSON.stringify(sample);
})()

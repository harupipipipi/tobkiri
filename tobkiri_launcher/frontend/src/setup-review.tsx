import {createRoot} from 'react-dom/client';
import {applyAppearanceToRoot, normalizeColorMode, normalizeTheme} from './lib/appearance';
import {NativeSetupReview} from './pages/NativeSetupReview';
import './setup-review.css';

const root = document.getElementById('root');
const payload = document.getElementById('setup-review-listing');
const interactive = document.getElementById('setup-review-native-input') !== null;
if (root && payload instanceof HTMLTemplateElement) {
  const appearance = document.documentElement.dataset;
  applyAppearanceToRoot(document.documentElement, {
    theme: normalizeTheme(appearance.theme),
    colorMode: normalizeColorMode(appearance.colorMode),
  });

  let listing: unknown = null;
  // The owner can embed only base64, so the JSON cannot terminate its HTML node.
  const encoded = payload.content.textContent ?? '';
  payload.remove();
  try {
    const maximum = 4 * 1024 * 1024;
    if (!encoded || encoded.length > Math.ceil(maximum / 3) * 4
        || /[^A-Za-z0-9+/=]/.test(encoded)) throw new Error('invalid Setup listing encoding');
    const bytes = Uint8Array.from(atob(encoded), (byte) => byte.charCodeAt(0));
    if (bytes.length > maximum) throw new Error('invalid Setup listing length');
    const raw = new TextDecoder('utf-8', {fatal: true}).decode(bytes);
    listing = JSON.parse(raw);
  } catch {
    // An absent/invalid listing renders the same closed error state.
  }
  createRoot(root).render(<NativeSetupReview listing={listing} interactive={interactive} />);
}

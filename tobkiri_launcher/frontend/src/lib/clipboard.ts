/**
 * Copy text to the system clipboard without making error recovery depend on a
 * particular WebView clipboard implementation.
 *
 * Tauri's WebView normally exposes `navigator.clipboard`, but that API can be
 * unavailable while the app is recovering from a startup failure. The legacy
 * selection fallback keeps diagnostic text copyable in that case.
 */
export async function copyTextToClipboard(
  text: string,
  clipboard: Pick<Clipboard, 'writeText'> | undefined = typeof navigator === 'undefined'
    ? undefined
    : navigator.clipboard,
  documentObject: Document | undefined = typeof document === 'undefined'
    ? undefined
    : document,
): Promise<boolean> {
  try {
    if (clipboard) {
      await clipboard.writeText(text);
      return true;
    }
  } catch {
    // The WebView may deny Clipboard API access. Fall through to selection.
  }

  if (!documentObject?.body || typeof documentObject.execCommand !== 'function') {
    return false;
  }

  const selection = documentObject.createElement('textarea');
  selection.value = text;
  selection.setAttribute('readonly', '');
  selection.setAttribute('aria-hidden', 'true');
  selection.style.position = 'fixed';
  selection.style.opacity = '0';
  selection.style.pointerEvents = 'none';
  documentObject.body.appendChild(selection);
  selection.select();

  try {
    return documentObject.execCommand('copy');
  } catch {
    return false;
  } finally {
    selection.remove();
  }
}

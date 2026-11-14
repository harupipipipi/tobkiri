// Injected into a separate WebKit content world of the native review only.
(() => {
  let reviewed = null;
  document.addEventListener('click', (event) => {
    if (!event.isTrusted || !(event.target instanceof Element)) return;

    const checkbox = document.querySelector('input[type="checkbox"][data-native-setup-reviewed]');
    if (event.target === checkbox) {
      reviewed = checkbox.checked && !checkbox.disabled ? checkbox : null;
      return;
    }

    const button = event.target.closest('button[data-native-setup-confirm]');
    if (!button || button.disabled || !reviewed || reviewed !== checkbox
        || !checkbox.checked || checkbox.disabled) return;

    // The child can report a gesture. Only Rust knows the candidate identity.
    reviewed = null;
    button.disabled = true;
    checkbox.disabled = true;
    window.webkit.messageHandlers.tobkiriSetupConfirmation.postMessage('confirm');
  }, {capture: true});
})()

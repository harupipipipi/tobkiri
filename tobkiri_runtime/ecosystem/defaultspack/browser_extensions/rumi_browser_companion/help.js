document.getElementById("open-settings").addEventListener("click", () => {
  void chrome.runtime.openOptionsPage();
});

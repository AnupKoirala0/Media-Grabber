(() => {
  const toggle = document.getElementById("toggle");
  const state = document.getElementById("state");
  const description = document.getElementById("description");
  const error = document.getElementById("error");
  let enabled = true;

  function render() {
    toggle.setAttribute("aria-checked", String(enabled));
    state.textContent = enabled ? "On" : "Off";
    description.textContent = enabled
      ? "Download controls are enabled."
      : "Download controls are disabled.";
  }

  function showStorageError() {
    if (chrome.runtime.lastError) {
      error.textContent = chrome.runtime.lastError.message;
      return true;
    }
    error.textContent = "";
    return false;
  }

  chrome.storage.local.get({ enabled: true }, settings => {
    if (!showStorageError()) {
      enabled = settings.enabled !== false;
      render();
    }
  });

  toggle.addEventListener("click", () => {
    const nextEnabled = !enabled;
    chrome.storage.local.set({ enabled: nextEnabled }, () => {
      if (!showStorageError()) {
        enabled = nextEnabled;
        render();
      }
    });
  });
})();

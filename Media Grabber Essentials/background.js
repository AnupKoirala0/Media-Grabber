const SERVER = "http://127.0.0.1:8765";
const ALLOWED_PATHS = new Set(["/formats", "/download"]);

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type !== "LOCAL_SERVER_REQUEST") return;

  if (!sender.tab || !ALLOWED_PATHS.has(message.path)) {
    sendResponse({ ok: false, error: "Request not allowed" });
    return;
  }

  fetch(`${SERVER}${message.path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(message.payload || {})
  })
    .then(async response => {
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        sendResponse({
          ok: false,
          source: "server",
          error: data.error || `Server error (${response.status})`
        });
        return;
      }
      sendResponse({ ok: true, data });
    })
    .catch(error => sendResponse({
      ok: false,
      source: "connection",
      error: error.message
    }));

  return true;
});

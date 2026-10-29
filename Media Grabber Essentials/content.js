(() => {
  const BUTTON_CLASS = "__lvfd_download_button";
  const PANEL_CLASS = "__lvfd_formats_panel";
  const TOAST_CLASS = "__lvfd_toast";
  let injected = new WeakMap();
  const controlHosts = new Map();
  let enabled = true;
  let checkedUrl = null;
  let supportCheckPending = false;
  let pageSupported = null;
  let pageSupportError = null;
  let pageGeneration = 0;
  let pageControlsDismissed = false;

  function toast(message) {
    document.querySelectorAll("." + TOAST_CLASS).forEach(el => el.remove());
    const el = document.createElement("div");
    el.className = TOAST_CLASS;
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    el.textContent = message;
    document.documentElement.appendChild(el);
    setTimeout(() => el.remove(), 3000);
  }

  function createDownloadIcon() {
    const namespace = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(namespace, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(namespace, "path");
    path.setAttribute(
      "d",
      "M12 2a1 1 0 00-1 1v11.586l-4.293-4.293a1 1 0 10-1.414 1.414L12 18.414l6.707-6.707a1 1 0 00-1.414-1.414L13 14.586V3a1 1 0 00-1-1Zm7 18H5a1 1 0 000 2h14a1 1 0 000-2Z"
    );
    svg.appendChild(path);
    return svg;
  }

  async function post(path, payload) {
    const result = await chrome.runtime.sendMessage({
      type: "LOCAL_SERVER_REQUEST",
      path,
      payload
    });
    if (!result?.ok) {
      const error = new Error(result?.error || "Local server request failed");
      error.source = result?.source;
      throw error;
    }
    return result.data;
  }

  async function showDownloadOptions(url, button, panel) {
    panel.replaceChildren();
    panel.classList.add("open");
    const closeButton = document.createElement("button");
    closeButton.type = "button";
    closeButton.className = "__lvfd_panel_close";
    closeButton.setAttribute("aria-label", "Close download options");
    closeButton.title = "Close";
    closeButton.textContent = "×";
    closeButton.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
      panel.classList.remove("open");
    });
    panel.appendChild(closeButton);

    const addSection = (title, choices) => {
      const heading = document.createElement("div");
      heading.className = "__lvfd_status";
      heading.textContent = title;
      panel.appendChild(heading);

      choices.forEach(choice => {
        const option = document.createElement("button");
        option.type = "button";
        option.className = "__lvfd_format";
        option.textContent = choice.label;
        option.addEventListener("click", async event => {
          event.preventDefault();
          event.stopPropagation();
          panel.classList.remove("open");
          try {
            const downloadOptions = { ...choice };
            delete downloadOptions.label;
            await post("/download", { url, ...downloadOptions });
            toast("Download Placed.");
          } catch (error) {
            toast(`Download failed: ${error.message}`);
          }
        });
        panel.appendChild(option);
      });
    };

    const heights = [2160, 1440, 1080, 720, 480, 240, 120];
    addSection("MP4", heights.map(height => ({
      label: `${height}p`,
      output_format: "mp4",
      height
    })));
    addSection("MKV", heights.map(height => ({
      label: `${height}p`,
      output_format: "mkv",
      height
    })));
    addSection("Audio", [
      { label: "256k MP3", output_format: "mp3", audio_bitrate: 256 },
      { label: "128k MP3", output_format: "mp3", audio_bitrate: 128 },
      { label: "M4A", output_format: "m4a" }
    ]);
  }

  function findVideo(media) {
    if (media?.tagName === "VIDEO") return media;
    try {
      const doc = media.contentDocument;
      if (doc) return doc.querySelector("video");
    } catch (_) {}
    return null;
  }

  function isVisibleMedia(media) {
    if (!media?.isConnected) return false;
    const bounds = media.getBoundingClientRect();
    if (bounds.width < 120 || bounds.height < 80) return false;
    if (bounds.right <= 0 || bounds.bottom <= 0
      || bounds.left >= window.innerWidth || bounds.top >= window.innerHeight) {
      return false;
    }

    const style = window.getComputedStyle(media);
    return style.display !== "none"
      && style.visibility !== "hidden"
      && style.opacity !== "0";
  }

  function isVideoFrame(frame) {
    const label = `${frame.title} ${frame.getAttribute("aria-label") || ""}`;
    if (/\b(video|player|media)\b/i.test(label)) return true;

    try {
      const hostname = new URL(frame.src, location.href).hostname.toLowerCase();
      return [
        "youtube.com",
        "youtube-nocookie.com",
        "vimeo.com",
        "dailymotion.com",
        "twitch.tv",
        "streamable.com",
        "rumble.com",
        "facebook.com",
        "instagram.com"
      ].some(domain => hostname === domain || hostname.endsWith(`.${domain}`));
    } catch (_) {
      return false;
    }
  }

  function findMediaTargets() {
    const player = location.hostname === "www.youtube.com"
      ? document.querySelector("#movie_player")
      : null;
    if (player && isVisibleMedia(player)) {
      return [{ anchor: player, media: player.querySelector("video") }];
    }

    const targets = [...document.querySelectorAll("video")]
      .filter(isVisibleMedia)
      .map(video => ({ anchor: video, media: video }));
    for (const frame of document.querySelectorAll("iframe")) {
      if (isVideoFrame(frame) && isVisibleMedia(frame)) {
        targets.push({ anchor: frame, media: frame });
      }
    }
    return targets;
  }

  function install(anchor, media, url) {
    if (!anchor || injected.has(anchor) || !anchor.isConnected) return;
    const bounds = anchor.getBoundingClientRect();
    if (!isVisibleMedia(anchor)) return;
    injected.set(anchor, true);

    const host = document.createElement("div");
    host.className = "__lvfd_host";
    host.style.position = "fixed";
    host.style.zIndex = "2147483647";
    host.style.left = `${bounds.left}px`;
    host.style.top = `${bounds.top}px`;
    host.style.width = `${bounds.width}px`;
    host.style.height = `${bounds.height}px`;
    host.style.pointerEvents = "none";

    const button = document.createElement("button");
    button.className = BUTTON_CLASS;
    button.type = "button";
    button.setAttribute("aria-label", "Download");
    button.title = "Choose download options";
    button.appendChild(createDownloadIcon());

    const panel = document.createElement("div");
    panel.className = PANEL_CLASS;
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Download options");

    const dismissButton = document.createElement("button");
    dismissButton.className = "__lvfd_button_dismiss";
    dismissButton.type = "button";
    dismissButton.setAttribute("aria-label", "Remove download button until page refresh");
    dismissButton.title = "Hide until page refresh";
    dismissButton.textContent = "×";

    host.append(button, dismissButton, panel);
    document.documentElement.appendChild(host);
    controlHosts.set(host, anchor);

    dismissButton.addEventListener("click", event => {
      event.preventDefault();
      event.stopPropagation();
      pageControlsDismissed = true;
      clearPageControls();
    });

    button.addEventListener("click", async event => {
      event.preventDefault();
      event.stopPropagation();
      if (!enabled) return;

      const video = findVideo(media);
      if (video) video.pause();
      if (!url) {
        toast("This page has no accessible URL.");
        return;
      }

      if (pageSupported !== true) {
        await checkPageSupport(url);
        if (pageSupported !== true) {
          toast(pageSupportError || "yt-dlp cannot download media from this page.");
          return;
        }
      }
      await showDownloadOptions(url, button, panel);
    });
    host.addEventListener("mouseleave", () => {
      if (!panel.matches(":hover")) {
        panel.classList.remove("open");
      }
    });
  }

  function updateControlPositions() {
    for (const [host, anchor] of controlHosts) {
      if (!host.isConnected || !anchor.isConnected) {
        host.remove();
        controlHosts.delete(host);
        continue;
      }
      const bounds = anchor.getBoundingClientRect();
      host.style.left = `${bounds.left}px`;
      host.style.top = `${bounds.top}px`;
      host.style.width = `${bounds.width}px`;
      host.style.height = `${bounds.height}px`;
    }
  }

  function clearPageControls() {
    controlHosts.forEach((_anchor, host) => host.remove());
    controlHosts.clear();
    document.querySelectorAll(
      "." + BUTTON_CLASS + ", ." + PANEL_CLASS + ", .__lvfd_button_dismiss"
    ).forEach(control => control.remove());
    injected = new WeakMap();
  }

  function installPageControls(url) {
    findMediaTargets().forEach(({ anchor, media }) => install(anchor, media, url));
  }

  async function checkPageSupport(url) {
    const generation = pageGeneration;
    checkedUrl = url;
    supportCheckPending = true;
    pageSupported = null;
    pageSupportError = null;
    try {
      const result = await post("/formats", { url });
      if (generation === pageGeneration) {
        pageSupported = Array.isArray(result?.formats)
          && result.formats.some(format => format?.ext?.toLowerCase() !== "mhtml");
        if (!pageSupported) {
          pageSupportError = "yt-dlp found no downloadable media formats on this page.";
        }
      }
    } catch (error) {
      if (generation === pageGeneration) {
        pageSupportError = error.source === "server"
          ? `Downloader error: ${error.message}`
          : `Cannot reach the downloader: ${error.message}`;
        console.warn("Could not check yt-dlp support for this page:", error);
      }
    } finally {
      if (generation === pageGeneration) supportCheckPending = false;
    }

    return generation === pageGeneration ? pageSupported : null;
  }

  function scan() {
    const url = location.href;
    if (url !== checkedUrl) {
      clearPageControls();
      pageGeneration += 1;
      checkedUrl = null;
      pageSupported = null;
      pageSupportError = null;
      supportCheckPending = false;
    }
    if (!enabled || pageControlsDismissed) return;

    if (!findMediaTargets().length) return;

    installPageControls(url);
    updateControlPositions();

    if (!supportCheckPending && checkedUrl !== url) {
      checkPageSupport(url);
    }
  }

  function setEnabled(value) {
    enabled = value;
    document.documentElement.dataset.lvfdEnabled = String(enabled);
    if (!enabled) {
      document.querySelectorAll("." + PANEL_CLASS).forEach(panel => {
        panel.classList.remove("open");
      });
    } else {
      scan();
    }
  }

  chrome.storage.local.get({ enabled: true }, settings => {
    setEnabled(settings.enabled !== false);
  });
  chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName === "local" && changes.enabled) {
      setEnabled(changes.enabled.newValue !== false);
    }
  });

  scan();
  window.addEventListener("scroll", updateControlPositions, true);
  window.addEventListener("resize", updateControlPositions);
  new MutationObserver(scan).observe(document.documentElement, {
    childList: true,
    subtree: true
  });
  setInterval(scan, 1000);
})();

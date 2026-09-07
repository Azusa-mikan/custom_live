    const ROUTE_KEY = "mikan-live-route";
    const liveRoutesData = document.getElementById("liveRoutesData");
    const liveRoutesRaw = liveRoutesData ? JSON.parse(liveRoutesData.textContent) : null;
    const liveRoutes = (Array.isArray(liveRoutesRaw) && liveRoutesRaw.length) ? liveRoutesRaw : [""];
    const state = {
      likes: 0,
      messages: [],
      route: localStorage.getItem(ROUTE_KEY) || "线路 1",
    };
    const $ = (id) => document.getElementById(id);
    const video = $("liveVideo");
    const videoPlaceholder = $("videoPlaceholder");
    const videoStatus = $("videoStatus");
    const videoDetail = $("videoDetail");
    const routeSelect = $("routeSelect");
    const messageList = $("messageList");
    const emptyMessages = $("emptyMessages");
    const toast = $("toast");
    const videoShell = $("videoShell");
    let hls;
    let toastTimer;
    let controlsTimer;

    const syncChatHeight = () => {
      if (window.matchMedia("(max-width: 860px)").matches) {
        document.documentElement.style.removeProperty("--video-height");
        return;
      }
      document.documentElement.style.setProperty("--video-height", `${videoShell.getBoundingClientRect().height}px`);
    };
    new ResizeObserver(syncChatHeight).observe(videoShell);
    window.addEventListener("resize", syncChatHeight, { passive: true });
    syncChatHeight();

    const persistRoute = () => localStorage.setItem(ROUTE_KEY, state.route);
    const showToast = (message) => {
      toast.textContent = message;
      toast.classList.add("visible");
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => toast.classList.remove("visible"), 2400);
    };
    let autoplayMuted = false;
    const playWithFallback = () => {
      const play = video.play();
      if (play) {
        play.catch(() => {
          autoplayMuted = true;
          video.muted = true;
          updateVolumeUI();
          video.play().catch(() => {});
          showToast("自动播放被拦截，已静音播放，点一下画面恢复声音");
        });
      }
    };
    const formatTime = (timestamp) => new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(timestamp);

    function renderMessages() {
      messageList.querySelectorAll(".message").forEach((node) => node.remove());
      emptyMessages.hidden = state.messages.length > 0;
      state.messages.forEach((message) => {
        const name = message.name || "我";
        const item = document.createElement("article");
        item.className = "message";
        item.innerHTML = `
          <div class="message-meta">
            <span class="message-name"></span>
            <time class="message-time"></time>
          </div>
          <p class="message-text"></p>`;
        item.querySelector(".message-name").textContent = name;
        item.querySelector(".message-time").textContent = formatTime(message.time);
        item.querySelector(".message-time").dateTime = new Date(message.time).toISOString();
        item.querySelector(".message-text").textContent = message.text;
        messageList.append(item);
      });
      messageList.scrollTop = messageList.scrollHeight;
    }

    function renderLikes() {
      $("likeCount").textContent = state.likes;
      $("likeButton").classList.toggle("liked", state.likes > 0);
    }

    async function loadHls(url) {
      if (hls) { hls.destroy(); hls = undefined; }
      video.removeAttribute("src");
      video.load();
      videoPlaceholder.hidden = false;
      videoStatus.textContent = "正在等待直播";
      videoDetail.textContent = "选择线路后将自动连接";
      if (!url) {
        videoStatus.textContent = "尚未配置直播地址";
        videoDetail.textContent = "在 .env 的 STREAM_N 中填入 HLS 地址即可开始播放";
        return;
      }
      if (video.canPlayType("application/vnd.apple.mpegurl")) {
        video.src = url;
        video.addEventListener("loadedmetadata", () => videoPlaceholder.hidden = true, { once: true });
        playWithFallback();
        return;
      }
      try {
        const module = await import("https://esm.sh/hls.js@1.5.17");
        const Hls = module.default || module;
        if (Hls.isSupported()) {
          hls = new Hls({ enableWorker: true });
          hls.loadSource(url);
          hls.attachMedia(video);
          hls.on(Hls.Events.MANIFEST_PARSED, () => { videoPlaceholder.hidden = true; playWithFallback(); });
          hls.on(Hls.Events.LEVEL_SWITCHED, () => {
            if (!videoInfo.hidden) refreshVideoInfo();
          });
          hls.on(Hls.Events.ERROR, (_, data) => { if (data.fatal) { videoStatus.textContent = "直播连接失败"; videoDetail.textContent = "请切换线路或稍后重试"; } });
        } else {
          videoStatus.textContent = "浏览器不支持 HLS";
          videoDetail.textContent = "请使用支持 HLS 的浏览器打开";
        }
      } catch {
        videoStatus.textContent = "播放器加载失败";
        videoDetail.textContent = "请检查网络连接后重试";
      }
    }

    const routeLabel = $("routeLabel");
    const routeMenu = $("routeMenu");
    let activeRouteIndex = 0;
    const setMenuOpen = (open) => {
      routeMenu.hidden = !open;
      routeSelect.setAttribute("aria-expanded", String(open));
    };
    const selectRoute = (index, { silent = false } = {}) => {
      const route = `线路 ${index + 1}`;
      activeRouteIndex = index;
      state.route = route;
      persistRoute();
      routeLabel.textContent = route;
      routeMenu.querySelectorAll(".route-option").forEach((option) => {
        const active = Number(option.dataset.index) === index;
        option.classList.toggle("selected", active);
        option.setAttribute("aria-selected", String(active));
      });
      if (!silent) showToast(`已切换至${route}`);
      loadHls(liveRoutes[index]);
    };

    const savedIndex = Math.max(0, (parseInt(state.route.split(" ")[1], 10) || 1) - 1);
    selectRoute(Math.min(savedIndex, liveRoutes.length - 1), { silent: true });
    renderMessages();
    renderLikes();

    routeSelect.addEventListener("click", () => setMenuOpen(routeMenu.hidden));
    routeMenu.addEventListener("click", (event) => {
      const option = event.target.closest(".route-option");
      if (!option) return;
      selectRoute(Number(option.dataset.index));
      setMenuOpen(false);
    });
    document.addEventListener("click", (event) => {
      if (!routeMenu.hidden && !event.target.closest(".route-field")) setMenuOpen(false);
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !routeMenu.hidden) setMenuOpen(false);
    });
    const NAME_KEY = "mikan-live-name";
    const SHARE_KEY = "mikan-live-share-ip";
    const nameDialog = $("nameDialog");
    const nameInput = $("nameInput");
    let identity = loadIdentity();

    function loadIdentity() {
      const saved = localStorage.getItem(NAME_KEY);
      if (!saved) return null;
      return { name: saved, shareIp: localStorage.getItem(SHARE_KEY) !== "0" };
    }
    function openNameDialog() {
      const savedName = localStorage.getItem(NAME_KEY);
      nameInput.value = savedName || "";
      nameInput.classList.remove("invalid");
      $("shareIpCheckbox").checked = localStorage.getItem(SHARE_KEY) !== "0";
      nameDialog.hidden = false;
      nameInput.focus();
      nameInput.select();
    }
    function confirmName() {
      const name = nameInput.value.trim();
      if (!name) {
        nameInput.classList.add("invalid");
        nameInput.focus();
        return;
      }
      localStorage.setItem(NAME_KEY, name);
      const shareIp = $("shareIpCheckbox").checked;
      localStorage.setItem(SHARE_KEY, shareIp ? "1" : "0");
      identity = { name, shareIp };
      nameDialog.hidden = true;
      $("messageInput").focus();
    }
    $("dialogConfirm").addEventListener("click", confirmName);
    $("settingsButton").addEventListener("click", openNameDialog);
    nameDialog.addEventListener("click", (event) => {
      if (event.target === nameDialog) nameDialog.hidden = true;
    });
    nameInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); confirmName(); }
    });
    nameInput.addEventListener("input", () => nameInput.classList.remove("invalid"));
    if (!identity) openNameDialog();

    $("messageForm").addEventListener("submit", (event) => {
      event.preventDefault();
      const input = $("messageInput");
      const text = input.value.trim();
      if (!text) { showToast("请输入消息内容"); return; }
      if (!identity) { openNameDialog(); return; }
      if (!ws || ws.readyState !== WebSocket.OPEN) { showToast("连接已断开，正在重连…"); return; }
      ws.send(JSON.stringify({ text, name: identity.name, share_ip: identity.shareIp }));
      input.value = "";
    });
    $("likeButton").addEventListener("click", async () => {
      try {
        const response = await fetch("/api/likes", { method: "POST" });
        const data = await response.json();
        state.likes = data.likes;
        renderLikes();
      } catch {
        showToast("点赞失败，请稍后重试");
      }
    });
    let ws;
    const connectWs = () => {
      const protocol = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${protocol}://${location.host}/api/chat`);
      ws.onmessage = (event) => {
        let data;
        try { data = JSON.parse(event.data); } catch { return; }
        if (data.type === "history") {
          state.messages = data.items.map((item) => ({ name: item.name, text: item.text, time: item.time }));
        } else if (data.type === "message") {
          const message = data.message;
          state.messages.push({ name: message.name, text: message.text, time: message.time });
          state.messages = state.messages.slice(-200);
        } else {
          return;
        }
        renderMessages();
      };
      ws.onerror = () => { ws.close(); };
      ws.onclose = () => { ws = undefined; setTimeout(connectWs, 3000); };
    };
    connectWs();
    fetch("/api/likes")
      .then((response) => response.json())
      .then((data) => { state.likes = data.likes; renderLikes(); })
      .catch(() => {});
    const centerPlay = $("centerPlay");
    const playerControls = $("playerControls");
    const ctrlPlayIcon = $("ctrlPlay").querySelector("span");
    const centerPlayIcon = centerPlay.querySelector("span");
    const ctrlMuteIcon = $("ctrlMute").querySelector("span");
    const ctrlVolume = $("ctrlVolume");
    const ctrlFullscreen = $("ctrlFullscreen");

    const updatePlayUI = () => {
      const playing = !video.paused && !video.ended;
      const icon = playing ? "pause" : "play_arrow";
      ctrlPlayIcon.textContent = icon;
      centerPlayIcon.textContent = icon;
      centerPlay.hidden = !(videoPlaceholder.hidden && !playing);
    };
    const updateVolumeUI = () => {
      ctrlMuteIcon.textContent = video.muted || video.volume === 0 ? "volume_off" : "volume_up";
      ctrlVolume.value = String(Math.round(video.volume * 100));
    };
    const isFullscreen = () => document.fullscreenElement === videoShell;
    const setControls = (visible) => {
      playerControls.classList.toggle("show", visible);
      videoShell.classList.toggle("hide-cursor", !visible);
      clearTimeout(controlsTimer);
      if (visible && isFullscreen()) {
        controlsTimer = setTimeout(() => setControls(false), 2600);
      }
    };
    const togglePlay = () => {
      if (video.paused) video.play().catch(() => {}); else video.pause();
    };
    const handleVideoClick = () => {
      if (autoplayMuted) {
        autoplayMuted = false;
        video.muted = false;
        updateVolumeUI();
        return;
      }
      togglePlay();
    };

    video.addEventListener("play", () => { updatePlayUI(); setControls(true); });
    video.addEventListener("pause", () => { updatePlayUI(); setControls(true); });
    video.addEventListener("volumechange", updateVolumeUI);
    video.addEventListener("click", handleVideoClick);
    centerPlay.addEventListener("click", togglePlay);
    $("ctrlPlay").addEventListener("click", togglePlay);
    $("ctrlMute").addEventListener("click", () => {
      if (video.muted || video.volume === 0) {
        video.muted = false;
        if (video.volume === 0) video.volume = 0.5;
      } else {
        video.muted = true;
      }
    });
    ctrlVolume.addEventListener("input", () => {
      video.volume = Number(ctrlVolume.value) / 100;
      video.muted = false;
    });
    ctrlFullscreen.addEventListener("click", () => {
      if (document.fullscreenElement) document.exitFullscreen();
      else videoShell.requestFullscreen();
    });
    document.addEventListener("fullscreenchange", () => {
      if (isFullscreen()) setControls(true);
      else setControls(false);
    });
    videoShell.addEventListener("mouseenter", () => setControls(true));
    videoShell.addEventListener("mousemove", () => setControls(true));
    videoShell.addEventListener("mouseleave", () => setControls(false));
    updatePlayUI();
    updateVolumeUI();
    window.addEventListener("scroll", () => $("appBar").classList.toggle("scrolled", window.scrollY > 4), { passive: true });

    const videoInfo = $("videoInfo");
    const videoMenu = $("videoMenu");
    const infoMenuItem = $("infoMenuItem");
    const currentHlsLevel = () => {
      if (!hls || !hls.levels || hls.levels.length === 0) return null;
      const index = hls.currentLevel;
      return index >= 0 && index < hls.levels.length ? hls.levels[index] : hls.levels[0];
    };
    let measuredFps = null;
    let fpsPrevFrames = null;
    let fpsWindowStart = null;
    let fpsWindowFrames = 0;
    const updateFpsRow = () => {
      if (videoInfo.hidden || measuredFps === null) return;
      const row = videoInfo.querySelector('.info-row[data-name="帧率"]');
      if (row) row.querySelector(".info-value").textContent = `~${Math.round(measuredFps)} fps`;
    };
    if (typeof video.requestVideoFrameCallback === "function") {
      const fpsTick = (_now, metadata) => {
        const presented = metadata && metadata.presentedFrames;
        if (typeof presented === "number" && fpsPrevFrames !== null) {
          const delta = presented - fpsPrevFrames;
          if (delta >= 0) {
            if (fpsWindowStart === null) fpsWindowStart = metadata.mediaTime;
            fpsWindowFrames += delta;
            const elapsed = metadata.mediaTime - fpsWindowStart;
            if (elapsed >= 0.5 && fpsWindowFrames > 0) {
              measuredFps = fpsWindowFrames / elapsed;
              fpsWindowStart = metadata.mediaTime;
              fpsWindowFrames = 0;
              updateFpsRow();
            }
          }
        }
        fpsPrevFrames = typeof presented === "number" ? presented : fpsPrevFrames;
        video.requestVideoFrameCallback(fpsTick);
      };
      video.requestVideoFrameCallback(fpsTick);
    }
    const refreshVideoInfo = () => {
      const level = currentHlsLevel();
      const fpsFromLevel = level && level.frameRate ? `${level.frameRate} fps` : "—";
      const rows = [
        ["线路", state.route],
        ["地址", liveRoutes[activeRouteIndex] || "—"],
        ["分辨率", video.videoWidth && video.videoHeight ? `${video.videoWidth} × ${video.videoHeight}` : "—"],
        ["帧率", measuredFps ? `~${Math.round(measuredFps)} fps` : fpsFromLevel],
        ["编码", level && level.videoCodec ? level.videoCodec : "—"],
        ["码率", level && level.bitrate ? `${Math.round(level.bitrate / 1000)} kbps` : "—"],
      ];
      videoInfo.innerHTML = "";
      rows.forEach(([label, value]) => {
        const row = document.createElement("div");
        row.className = "info-row";
        row.dataset.name = label;
        const labelEl = document.createElement("span");
        labelEl.className = "info-label";
        labelEl.textContent = label;
        const valueEl = document.createElement("span");
        valueEl.className = "info-value";
        valueEl.textContent = value;
        row.append(labelEl, valueEl);
        videoInfo.append(row);
      });
    };
    const setInfoVisible = (visible) => {
      videoInfo.hidden = !visible;
      infoMenuItem.textContent = visible ? "隐藏视频信息" : "显示视频信息";
      if (visible) refreshVideoInfo();
    };
    videoShell.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      const rect = videoShell.getBoundingClientRect();
      const left = Math.min(event.clientX - rect.left, rect.width - 160);
      const top = Math.min(event.clientY - rect.top, rect.height - 60);
      videoMenu.style.left = `${Math.max(0, left)}px`;
      videoMenu.style.top = `${Math.max(0, top)}px`;
      videoMenu.hidden = false;
    });
    infoMenuItem.addEventListener("click", () => {
      setInfoVisible(videoInfo.hidden);
      videoMenu.hidden = true;
    });
    document.addEventListener("click", () => { videoMenu.hidden = true; });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      videoMenu.hidden = true;
      if (!videoInfo.hidden) setInfoVisible(false);
    });
    video.addEventListener("loadedmetadata", () => { if (!videoInfo.hidden) refreshVideoInfo(); });
    video.addEventListener("resize", () => { if (!videoInfo.hidden) refreshVideoInfo(); });

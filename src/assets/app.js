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
    const liveBadge = $("liveBadge");
    const routeSelect = $("routeSelect");
    const messageList = $("messageList");
    const emptyMessages = $("emptyMessages");
    const toast = $("toast");
    const videoShell = $("videoShell");
    let hls;
    let streamReady = false;
    let readyTimer;
    let firstFrameReady = false;
    let firstFrameTimer;
    let stallRetries = 0;
    let lastSampleTime = 0;
    let lastAdvanceAt = 0;
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

    // —— debug 探针（仅 ?debug=1 启用）——
    const DEBUG = new URLSearchParams(location.search).get("debug") === "1";
    let dbgBox = null;
    const dbgLog = (message) => {
      if (!DEBUG) return;
      if (!dbgBox) {
        dbgBox = document.createElement("div");
        dbgBox.style.cssText = [
          "position:fixed", "right:8px", "bottom:8px", "z-index:9999",
          "max-height:44vh", "width:min(560px, calc(100vw - 16px))",
          "overflow:auto", "background:rgba(0,0,0,.85)", "color:#9ef",
          "font:11px/1.5 ui-monospace,Consolas,monospace", "padding:8px 10px",
          "border-radius:10px", "pointer-events:auto", "white-space:pre-wrap",
        ].join(";");
        document.body.append(dbgBox);
      }
      const now = new Date();
      const line = document.createElement("div");
      line.textContent =
        `[${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}:${String(now.getSeconds()).padStart(2, "0")}.${String(now.getMilliseconds()).padStart(3, "0")}] ${message}`;
      dbgBox.append(line);
      while (dbgBox.childElementCount > 300) dbgBox.firstElementChild.remove();
      dbgBox.scrollTop = dbgBox.scrollHeight;
    };
    if (DEBUG) {
      const bufferedEnd = () => {
        try {
          const ranges = video.buffered;
          if (!ranges.length) return "-";
          let end = 0;
          for (let i = 0; i < ranges.length; i++) end = Math.max(end, ranges.end(i));
          return end.toFixed(2);
        } catch {
          return "?";
        }
      };
      ["waiting", "stalled", "playing", "pause", "seeked", "seeking", "emptied",
        "canplay", "loadedmetadata", "durationchange", "error", "ended",
      ].forEach((eventName) => {
        video.addEventListener(eventName, () => {
          dbgLog(
            `video:${eventName} t=${Number.isFinite(video.currentTime) ? video.currentTime.toFixed(2) : "?"} ` +
            `bufEnd=${bufferedEnd()} readyState=${video.readyState} paused=${video.paused}`);
        });
      });
    }

    const persistRoute = () => localStorage.setItem(ROUTE_KEY, state.route);
    const showToast = (message) => {
      toast.textContent = message;
      toast.classList.add("visible");
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => toast.classList.remove("visible"), 2400);
    };
    let autoplayMuted = false;
    let suppressTap = false;
    const playWithFallback = () => {
      const play = video.play();
      if (play) {
        play.catch(() => {
          autoplayMuted = true;
          dbgLog("autoplay 被拦截，静音重播");
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

    const clockTime = $("clockTime");
    const clockDate = $("clockDate");
    let clockTimer;
    const WEEK_NAMES = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
    const pad2 = (n) => String(n).padStart(2, "0");
    const renderClock = () => {
      const now = new Date();
      clockTime.textContent =
        `${pad2(now.getHours())}:${pad2(now.getMinutes())}:${pad2(now.getSeconds())}`;
      clockDate.textContent =
        `${now.getFullYear()}年${now.getMonth() + 1}月${now.getDate()}日 ${WEEK_NAMES[now.getDay()]}`;
    };
    const stopClock = () => { clearTimeout(clockTimer); clockTimer = undefined; };
    const startClock = () => {
      stopClock();
      renderClock();
      const tick = () => {
        renderClock();
        clockTimer = setTimeout(tick, 1000 - (Date.now() % 1000));
      };
      clockTimer = setTimeout(tick, 1000 - (Date.now() % 1000));
    };
    // LIVE 徽标显隐：用内联样式控制（display 规则不受 CSS 加载/缓存影响）
    const setBadgeVisible = (visible) => {
      liveBadge.style.display = visible ? "" : "none";
      liveBadge.hidden = !visible;
    };
    // placeholder 三种视觉态：loading(转圈) / offline(未推流时钟) / message(文字提示)
    const setPlaceholderState = (mode) => {
      videoPlaceholder.classList.toggle("offline", mode === "offline");
      videoPlaceholder.classList.toggle("message", mode === "message");
    };
    const markPlaying = () => {
      streamReady = true;
      setBadgeVisible(true);
      firstFrameReady = false;
      dbgLog("→ markPlaying：manifest 就绪");
      lastSampleTime = 0;
      lastAdvanceAt = 0;
      clearTimeout(readyTimer);
      clearTimeout(firstFrameTimer);
      stopClock();
      setPlaceholderState("loading");
      videoPlaceholder.hidden = true;
      // manifest 已就绪但可能还没有可播分片（冷启动/刚推流），
      // 没出首帧则按停滞处理。2s 对照实验证明：重载能出画面但首几秒分片会引发
      // video error，稳定窗口在 2~10s 之间，10s 是已验证的可靠值
      firstFrameTimer = setTimeout(onStallTimeout, 10000);
    };
    const enterOffline = () => {
      setBadgeVisible(false);
      firstFrameReady = false;
      dbgLog("→ enterOffline：进入离线时钟");
      clearTimeout(readyTimer);
      clearTimeout(firstFrameTimer);
      stopClock();
      setPlaceholderState("offline");
      videoPlaceholder.hidden = false;
      startClock();
    };
    // 就绪兜底：开始加载后若迟迟拿不到流（网络瞬断/源挂起等非 4xx 场景），
    // 不依赖 hls.js 何时报错，超时后强制切到离线时钟
    const scheduleReadyGuard = () => {
      clearTimeout(readyTimer);
      readyTimer = setTimeout(() => {
        if (streamReady) return;
        dbgLog("→ readyGuard：12s 未就绪");
        if (hls) { hls.destroy(); hls = undefined; }
        enterOffline();
      }, 12000);
    };
    // 首帧停滞处理：manifest 就绪后迟迟没出画面（hls.js 卡在空 playlists 上不自愈）。
    // 自动重载当前线路（不清零计数），同一线路累计 2 次仍失败则转离线时钟。
    const onStallTimeout = () => {
      if (firstFrameReady) return;
      if (document.hidden) { // 后台标签页播放不推进，延后再查而不是误重载
        firstFrameTimer = setTimeout(onStallTimeout, 3000);
        return;
      }
      stallRetries += 1;
      dbgLog(`→ 首帧停滞 ${stallRetries}/2 次，重载当前线路`);
      if (stallRetries > 2) {
        if (hls) { hls.destroy(); hls = undefined; }
        enterOffline();
        return;
      }
      loadHls(liveRoutes[activeRouteIndex]);
    };

    async function loadHls(url) {
      dbgLog(`loadHls: ${url || "(空)"}`);
      streamReady = false;
      firstFrameReady = false;
      clearTimeout(firstFrameTimer);
      setBadgeVisible(false);
      if (hls) { hls.destroy(); hls = undefined; }
      video.removeAttribute("src");
      video.load();
      stopClock();
      videoStatus.textContent = "";
      videoDetail.textContent = "";
      setPlaceholderState("loading");
      videoPlaceholder.hidden = false;
      if (!url) {
        enterOffline();
        return;
      }
      scheduleReadyGuard();
      if (video.canPlayType("application/vnd.apple.mpegurl")) {
        video.src = url;
        video.addEventListener("loadedmetadata", markPlaying, { once: true });
        video.addEventListener("error", () => { if (!streamReady) enterOffline(); });
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
          if (DEBUG) {
            hls.on(Hls.Events.LEVEL_LOADED, (_event, data) => {
              const d = data.details;
              dbgLog(
                `LEVEL_LOADED #${data.level} sn[${d.startSN}..${d.endSN}] n=${d.fragments.length} ` +
                `dur=${(d.totalduration || 0).toFixed(2)}s live=${d.live} target=${d.targetduration || 0}s ` +
                `${d.advanced ? "advanced" : d.updated ? "updated" : "MISSED"}`);
            });
            hls.on(Hls.Events.FRAG_LOADING, (_event, data) => {
              dbgLog(`FRAG_LOADING #${data.frag.sn}`);
            });
            hls.on(Hls.Events.FRAG_BUFFERED, (_event, data) => {
              dbgLog(`FRAG_BUFFERED #${data.frag.sn}`);
            });
          }
          hls.on(Hls.Events.MANIFEST_PARSED, () => { markPlaying(); playWithFallback(); });
          hls.on(Hls.Events.LEVEL_SWITCHED, () => {
            if (!videoInfo.hidden) refreshVideoInfo();
          });
          hls.on(Hls.Events.ERROR, (_, data) => {
            const errorStatus = data.response &&
              (data.response.status ?? data.response.statusCode);
            dbgLog(`ERROR ${data.type} ${data.details} fatal=${data.fatal} http=${errorStatus ?? "-"}`);
            if (streamReady) {
              // 已就绪后只在致命错误时回到离线时钟（播放中断流）
              if (data.fatal) enterOffline();
              return;
            }
            // 未就绪期间：
            //  - fatal / HTTP 4xx（如 MediaMTX no-stream 404）→ 源不可用，停掉重试直接离线
            //  - 超时/断网等瞬断错误 → 放行，交给 hls.js 自动重试；始终不就绪由 readyGuard 兜底
            const status = data.response &&
              (data.response.status ?? data.response.statusCode);
            const failSource = data.fatal ||
              (Number.isInteger(status) && status >= 400 && status < 500);
            if (!failSource) return;
            if (hls) { hls.destroy(); hls = undefined; }
            enterOffline();
          });
        } else {
          setPlaceholderState("message");
          videoStatus.textContent = "浏览器不支持 HLS";
          videoDetail.textContent = "请使用支持 HLS 的浏览器打开";
        }
      } catch {
        setPlaceholderState("message");
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
      stallRetries = 0; // 手动切换线路 = 一次全新的尝试，重置自动重载计数
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
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !nameDialog.hidden) nameDialog.hidden = true;
    });
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
    const VOLUME_KEY = "mikan-live-volume";
    // 音量持久化：刷新后沿用上次音量，而不是每次回到满格
    const savedVolume = Number.parseFloat(localStorage.getItem(VOLUME_KEY) || "");
    if (Number.isFinite(savedVolume) && savedVolume >= 0 && savedVolume <= 1) {
      video.volume = savedVolume;
    }

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
      localStorage.setItem(VOLUME_KEY, String(video.volume));
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
      if (suppressTap) { suppressTap = false; return; }
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
    video.addEventListener("playing", () => {
      // 真正出过画面：停止首帧停滞计时，后续暂停/卡顿不再触发自动重载
      firstFrameReady = true;
      clearTimeout(firstFrameTimer);
    });
    // hls.js 对 live 播放列表永不判定 ended（见 base-stream-controller 的 _streamEnded），
    // 因此"主播下播"在页面上的可观测现象是：buffer 耗尽后 currentTime 不再前进。
    // 用一个推进 watchdog 探测：前台播放中若 12s 无任何进度即判定直播停滞 → 时钟。
    const checkPlaybackStall = () => {
      if (
        !streamReady || !firstFrameReady ||
        video.paused || document.hidden ||
        !Number.isFinite(video.currentTime)
      ) {
        // 未就绪 / 未出过画面 / 用户暂停 / 后台：不累计停滞时间
        if (!video.paused && !document.hidden && streamReady) {
          lastSampleTime = video.currentTime;
          lastAdvanceAt = Date.now();
        }
        return;
      }
      const advanced = Math.abs(video.currentTime - lastSampleTime) >= 0.05;
      lastSampleTime = video.currentTime;
      if (advanced) {
        lastAdvanceAt = Date.now();
        return;
      }
      if (Date.now() - lastAdvanceAt > 12000) {
        dbgLog("→ 播放推进停滞 >12s，判定下播");
        if (hls) { hls.destroy(); hls = undefined; }
        enterOffline();
      }
    };
    setInterval(checkPlaybackStall, 2000);
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
    // 触屏：点一下画面唤出控制条（首次触摸不切换播放），控制条已在显示时再点画面才是播放/暂停
    videoShell.addEventListener("pointerdown", (event) => {
      if (event.pointerType !== "touch" || event.target !== video) return;
      if (!playerControls.classList.contains("show")) suppressTap = true;
      setControls(true);
      clearTimeout(controlsTimer);
      controlsTimer = setTimeout(() => setControls(false), 3200);
    });
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

/*
 * Ringback Mini App signaling protocol (JSON over one authenticated WebSocket)
 * ---------------------------------------------------------------------------
 * Client opens /ws, then MUST send first: { type: "auth", initData: "<Telegram initData>" }
 * Server: { type: "authenticated", user, iceServers, pendingCall? }
 * Server: { type: "incoming", callId, openingHint? }
 * Client: { type: "offer", callId, sdp, sdpType: "offer" }
 * Server: { type: "answer", callId, sdp, sdpType: "answer" }
 * Either direction: { type: "ping" } / { type: "pong" }
 * Client: { type: "decline" | "hangup", callId }
 * Server: { type: "ended", callId?, reason? } or { type: "error", message, code? }
 *
 * Offers are non-trickle: the browser waits for ICE gathering to complete and
 * puts every candidate in the SDP. Telegram initData is deliberately never put
 * in the URL, where it could be retained by access logs or browser history.
 */

(function () {
  "use strict";

  const STATES = Object.freeze({
    CONNECTING: "connecting",
    IDLE: "idle",
    INCOMING: "incoming",
    ACTIVE: "active",
    ENDED: "ended",
    ERROR: "error",
  });

  const STATE_COPY = Object.freeze({
    connecting: {
      eyebrow: "Защищённый канал",
      title: "Подключаемся…",
      copy: "Готовим звонки с вашим AI-агентом",
    },
    idle: {
      eyebrow: "Ringback в сети",
      title: "Готово к звонку",
      copy: "Когда агенту понадобится ваш ответ, звонок появится здесь",
    },
    incoming: {
      eyebrow: "AI-агент",
      title: "Входящий звонок",
      copy: "Агент ждёт вашего ответа",
    },
    active: {
      eyebrow: "Защищённый WebRTC-звонок",
      title: "Вы на связи",
      copy: "Говорите обычным голосом — агент вас слышит",
    },
    ended: {
      eyebrow: "Ringback",
      title: "Звонок завершён",
      copy: "Можно закрыть мини-приложение",
    },
    error: {
      eyebrow: "Нужно ваше внимание",
      title: "Не удалось подключиться",
      copy: "Проверьте интернет и повторите попытку",
    },
  });

  const config = Object.freeze({
    authTimeoutMs: 12_000,
    iceGatheringTimeoutMs: 15_000,
    heartbeatMs: 25_000,
    websocketUrl: window.RINGBACK_CONFIG && window.RINGBACK_CONFIG.websocketUrl,
  });

  const elements = {
    body: document.body,
    title: document.getElementById("stateTitle"),
    eyebrow: document.getElementById("stateEyebrow"),
    copy: document.getElementById("stateCopy"),
    callNote: document.getElementById("callNote"),
    callNoteText: document.getElementById("callNoteText"),
    connectionPill: document.getElementById("connectionPill"),
    connectionLabel: document.getElementById("connectionLabel"),
    offlineBanner: document.getElementById("offlineBanner"),
    timer: document.getElementById("callTimer"),
    mutedBadge: document.getElementById("mutedBadge"),
    startCallButton: document.getElementById("startCallButton"),
    muteButton: document.getElementById("muteButton"),
    muteLabel: document.getElementById("muteLabel"),
    acceptButton: document.getElementById("acceptButton"),
    declineButton: document.getElementById("declineButton"),
    cancelButton: document.getElementById("cancelButton"),
    hangupButton: document.getElementById("hangupButton"),
    doneButton: document.getElementById("doneButton"),
    retryButton: document.getElementById("retryButton"),
    audioGate: document.getElementById("audioGate"),
    remoteAudio: document.getElementById("remoteAudio"),
    announcer: document.getElementById("announcer"),
    themeColor: document.querySelector('meta[name="theme-color"]'),
  };

  let telegram = null;
  let socket = null;
  let socketGeneration = 0;
  let reconnectTimer = null;
  let authTimer = null;
  let heartbeatTimer = null;
  let reconnectAttempt = 0;
  let authenticated = false;
  let intentionalSocketClose = false;

  let currentState = STATES.CONNECTING;
  let currentCall = null;
  let peerConnection = null;
  let localStream = null;
  let remoteStream = null;
  let iceServers = [];
  let mediaAttempt = 0;
  let muted = false;
  let callStartedAt = 0;
  let timerInterval = null;
  let connectionFailureTimer = null;
  let callRequestPending = false;
  let callRequestTimer = null;

  function init() {
    setupTelegram();
    bindEvents();
    updateOnlineStatus();
    setState(STATES.CONNECTING);

    if (!window.RTCPeerConnection || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showError("Этот браузер не поддерживает WebRTC-звонки. Откройте Ringback в свежей версии Telegram.");
      return;
    }

    connect();
  }

  function setupTelegram() {
    telegram = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
    if (!telegram) return;

    tryCall(function () { telegram.ready(); });
    tryCall(function () { telegram.expand(); });
    tryCall(function () { telegram.disableVerticalSwipes(); });
    syncTelegramTheme();

    if (typeof telegram.onEvent === "function") {
      telegram.onEvent("themeChanged", syncTelegramTheme);
      telegram.onEvent("viewportChanged", syncViewportHeight);
    }
    syncViewportHeight();
  }

  function syncTelegramTheme() {
    if (!telegram) return;
    const theme = telegram.themeParams || {};
    const background = theme.bg_color || "#07111f";
    if (elements.themeColor) elements.themeColor.setAttribute("content", background);
    tryCall(function () { telegram.setHeaderColor(background); });
    tryCall(function () { telegram.setBackgroundColor(background); });
    tryCall(function () { telegram.setBottomBarColor(background); });
  }

  function syncViewportHeight() {
    if (!telegram || !telegram.viewportStableHeight) return;
    document.documentElement.style.setProperty("--tg-viewport-stable-height", telegram.viewportStableHeight + "px");
  }

  function bindEvents() {
    elements.acceptButton.addEventListener("click", acceptCall);
    elements.startCallButton.addEventListener("click", requestCall);
    elements.declineButton.addEventListener("click", declineCall);
    elements.cancelButton.addEventListener("click", hangupCall);
    elements.hangupButton.addEventListener("click", hangupCall);
    elements.muteButton.addEventListener("click", toggleMute);
    elements.retryButton.addEventListener("click", retryConnection);
    elements.doneButton.addEventListener("click", finish);
    elements.audioGate.addEventListener("click", unlockAudio);

    window.addEventListener("online", function () {
      updateOnlineStatus();
      if (!socket || socket.readyState > WebSocket.OPEN) retryConnection();
    });
    window.addEventListener("offline", updateOnlineStatus);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible" && (!socket || socket.readyState > WebSocket.OPEN)) {
        connect();
      }
    });
    window.addEventListener("pagehide", leavePage);
  }

  function connect() {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;

    if (!navigator.onLine) {
      setState(STATES.CONNECTING, { copy: "Нет интернета. Подключимся, как только сеть вернётся." });
      return;
    }

    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;

    const initData = telegram ? telegram.initData : "";
    if (!initData && !isLoopbackHost()) {
      setConnectionStatus(false, "Не в Telegram");
      showError("Откройте Ringback из кнопки меню в Telegram — так мы сможем безопасно проверить ваш профиль.");
      return;
    }

    intentionalSocketClose = false;
    authenticated = false;
    setConnectionStatus(false, "Подключение");
    if (!currentCall) setState(STATES.CONNECTING);

    const generation = ++socketGeneration;
    let nextSocket;
    try {
      nextSocket = new WebSocket(resolveWebSocketUrl());
    } catch (_error) {
      showError("Неверный адрес сервера Ringback.");
      return;
    }
    socket = nextSocket;

    nextSocket.addEventListener("open", function () {
      if (generation !== socketGeneration) return;
      send({ type: "auth", initData: initData });
      clearTimeout(authTimer);
      authTimer = setTimeout(function () {
        if (!authenticated && nextSocket.readyState === WebSocket.OPEN) {
          nextSocket.close(4001, "Authentication timeout");
        }
      }, config.authTimeoutMs);
    });

    nextSocket.addEventListener("message", function (event) {
      if (generation !== socketGeneration) return;
      handleSocketMessage(event.data);
    });

    nextSocket.addEventListener("error", function () {
      if (generation !== socketGeneration) return;
      setConnectionStatus(false, "Нет связи");
    });

    nextSocket.addEventListener("close", function () {
      if (generation !== socketGeneration) return;
      clearTimeout(authTimer);
      clearInterval(heartbeatTimer);
      authenticated = false;
      callRequestPending = false;
      clearTimeout(callRequestTimer);
      callRequestTimer = null;
      elements.startCallButton.disabled = false;
      setConnectionStatus(false, "Нет связи");

      if (intentionalSocketClose) return;
      if (currentCall) {
        cleanupCall();
        showError("Связь со звонком прервалась. Проверьте интернет и повторите попытку.");
      } else if (currentState !== STATES.ERROR) {
        setState(STATES.CONNECTING, { copy: "Восстанавливаем защищённое соединение…" });
      }
      scheduleReconnect();
    });
  }

  function resolveWebSocketUrl() {
    if (config.websocketUrl) {
      const configured = new URL(config.websocketUrl, window.location.href);
      if (configured.protocol !== "ws:" && configured.protocol !== "wss:") {
        throw new Error("WebSocket URL must use ws or wss");
      }
      return configured.toString();
    }
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    return protocol + "//" + window.location.host + "/ws";
  }

  function handleSocketMessage(raw) {
    if (typeof raw !== "string" || raw.length > 1_000_000) return;

    let message;
    try {
      message = JSON.parse(raw);
    } catch (_error) {
      return;
    }
    if (!message || typeof message !== "object" || typeof message.type !== "string") return;

    switch (message.type) {
      case "authenticated":
        handleAuthenticated(message);
        break;
      case "incoming":
        showIncoming(message);
        break;
      case "call_request":
        handleCallRequest(message);
        break;
      case "answer":
        applyAnswer(message);
        break;
      case "connected":
        if (currentCall && normalizedCallId(message.callId) === currentCall.id) activateCall();
        break;
      case "ended":
        handleRemoteEnd(message);
        break;
      case "error":
        handleServerError(message);
        break;
      case "ping":
        send({ type: "pong" });
        break;
      case "pong":
        break;
      default:
        break;
    }
  }

  function handleAuthenticated(message) {
    clearTimeout(authTimer);
    authenticated = true;
    callRequestPending = false;
    clearTimeout(callRequestTimer);
    callRequestTimer = null;
    elements.startCallButton.disabled = false;
    reconnectAttempt = 0;
    iceServers = sanitizeIceServers(message.iceServers);
    setConnectionStatus(true, "В сети");
    startHeartbeat();

    if (message.pendingCall && typeof message.pendingCall === "object") {
      showIncoming(message.pendingCall);
    } else {
      setState(STATES.IDLE);
      setClosingConfirmation(false);
    }
  }

  function sanitizeIceServers(value) {
    if (!Array.isArray(value)) return [];
    return value.slice(0, 12).filter(function (entry) {
      if (!entry || typeof entry !== "object") return false;
      if (typeof entry.urls === "string") return entry.urls.startsWith("stun:") || entry.urls.startsWith("turn:") || entry.urls.startsWith("turns:");
      return Array.isArray(entry.urls) && entry.urls.length > 0 && entry.urls.every(function (url) {
        return typeof url === "string" && (url.startsWith("stun:") || url.startsWith("turn:") || url.startsWith("turns:"));
      });
    }).map(function (entry) {
      return {
        urls: entry.urls,
        username: typeof entry.username === "string" ? entry.username : undefined,
        credential: typeof entry.credential === "string" ? entry.credential : undefined,
      };
    });
  }

  function showIncoming(message) {
    if (!authenticated) return;
    const callId = normalizedCallId(message.callId);
    if (!callId) return;

    if (currentCall && currentCall.id !== callId) {
      send({ type: "decline", callId: callId });
      return;
    }

    callRequestPending = false;
    clearTimeout(callRequestTimer);
    callRequestTimer = null;
    elements.startCallButton.disabled = false;
    currentCall = {
      id: callId,
      openingHint: normalizedText(message.openingHint, 320),
    };
    setState(STATES.INCOMING, {
      note: currentCall.openingHint,
    });
    setClosingConfirmation(true);
    haptic("notification", "warning");
    tryCall(function () { if (telegram) telegram.expand(); });
  }

  function requestCall() {
    if (!authenticated || currentState !== STATES.IDLE || callRequestPending) return;
    callRequestPending = true;
    elements.startCallButton.disabled = true;
    haptic("impact", "medium");
    if (!send({ type: "request_call" })) {
      callRequestPending = false;
      elements.startCallButton.disabled = false;
      showError("Не удалось запросить звонок. Проверьте связь.");
      return;
    }
    setState(STATES.CONNECTING, {
      title: "Готовим звонок…",
      copy: "AI-ассистент скоро позвонит",
    });
    clearTimeout(callRequestTimer);
    callRequestTimer = setTimeout(function () {
      if (!callRequestPending || currentCall) return;
      callRequestPending = false;
      elements.startCallButton.disabled = false;
      setState(STATES.IDLE, {
        copy: "Подготовка заняла слишком много времени. Попробуйте ещё раз.",
      });
    }, 35_000);
  }

  function handleCallRequest(message) {
    if (!callRequestPending) return;
    if (message.accepted) return;
    callRequestPending = false;
    clearTimeout(callRequestTimer);
    callRequestTimer = null;
    elements.startCallButton.disabled = false;
    const reason = message.reason === "unavailable"
      ? "Звонки пока не настроены на сервере."
      : "Ассистент уже занят другим разговором.";
    setState(STATES.IDLE, { copy: reason });
    haptic("notification", "error");
  }

  async function acceptCall() {
    if (currentState !== STATES.INCOMING || !currentCall || !authenticated) return;
    const callId = currentCall.id;
    const attempt = ++mediaAttempt;
    haptic("impact", "medium");
    requestTelegramFullscreen();
    setState(STATES.CONNECTING, {
      callConnecting: true,
      title: "Соединяем…",
      copy: "Получаем доступ к микрофону",
    });

    try {
      localStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: { ideal: true },
          noiseSuppression: { ideal: true },
          autoGainControl: { ideal: true },
          channelCount: { ideal: 1 },
        },
        video: false,
      });

      if (!currentCall || currentCall.id !== callId || mediaAttempt !== attempt) {
        stopStream(localStream);
        localStream = null;
        return;
      }

      setState(STATES.CONNECTING, {
        callConnecting: true,
        title: "Соединяем…",
        copy: "Создаём защищённый WebRTC-канал",
      });

      createPeerConnection();
      localStream.getAudioTracks().forEach(function (track) {
        peerConnection.addTrack(track, localStream);
      });

      const offer = await peerConnection.createOffer({ offerToReceiveAudio: true });
      await peerConnection.setLocalDescription(offer);
      await waitForIceGatheringComplete(peerConnection, config.iceGatheringTimeoutMs);

      if (!currentCall || currentCall.id !== callId || mediaAttempt !== attempt || !peerConnection.localDescription) return;
      const description = peerConnection.localDescription;
      if (!send({
        type: "offer",
        callId: callId,
        sdp: description.sdp,
        sdpType: "offer",
      })) {
        throw new Error("SIGNALING_CLOSED");
      }

      setState(STATES.CONNECTING, {
        callConnecting: true,
        title: "Ждём ответа…",
        copy: "Агент подключается к звонку",
      });
    } catch (error) {
      if (mediaAttempt !== attempt) return;
      send({ type: "decline", callId: callId });
      cleanupCall();
      showError(mediaErrorMessage(error));
      haptic("notification", "error");
    }
  }

  function createPeerConnection() {
    if (peerConnection) peerConnection.close();
    remoteStream = new MediaStream();
    peerConnection = new RTCPeerConnection({
      iceServers: iceServers,
      bundlePolicy: "max-bundle",
      rtcpMuxPolicy: "require",
    });

    peerConnection.addEventListener("track", function (event) {
      if (event.streams && event.streams[0]) {
        elements.remoteAudio.srcObject = event.streams[0];
      } else {
        remoteStream.addTrack(event.track);
        elements.remoteAudio.srcObject = remoteStream;
      }
      tryPlayRemoteAudio();
    });

    peerConnection.addEventListener("connectionstatechange", handlePeerConnectionState);
  }

  async function applyAnswer(message) {
    if (!currentCall || !peerConnection) return;
    if (normalizedCallId(message.callId) !== currentCall.id) return;
    if (message.sdpType && message.sdpType !== "answer") return;
    if (typeof message.sdp !== "string" || message.sdp.length > 1_000_000) return;

    try {
      await peerConnection.setRemoteDescription({ type: "answer", sdp: message.sdp });
      activateCall();
      tryPlayRemoteAudio();
    } catch (_error) {
      const callId = currentCall.id;
      send({ type: "hangup", callId: callId });
      cleanupCall();
      showError("Не удалось согласовать звуковой канал. Попробуйте принять следующий звонок.");
    }
  }

  function handlePeerConnectionState() {
    if (!peerConnection || !currentCall) return;
    clearTimeout(connectionFailureTimer);

    switch (peerConnection.connectionState) {
      case "connected":
        activateCall();
        break;
      case "disconnected":
        if (currentState === STATES.ACTIVE) {
          setState(STATES.ACTIVE, { copy: "Восстанавливаем звук…" });
        }
        connectionFailureTimer = setTimeout(function () {
          if (peerConnection && peerConnection.connectionState === "disconnected") failActiveCall();
        }, 8_000);
        break;
      case "failed":
        failActiveCall();
        break;
      default:
        break;
    }
  }

  function activateCall() {
    if (!currentCall) return;
    if (currentState === STATES.ACTIVE) {
      // Restore the normal copy after a brief WebRTC "disconnected" recovery.
      elements.copy.textContent = STATE_COPY.active.copy;
      return;
    }
    setState(STATES.ACTIVE);
    setConnectionStatus(true, "WebRTC");
    callStartedAt = Date.now();
    updateCallTimer();
    clearInterval(timerInterval);
    timerInterval = setInterval(updateCallTimer, 1_000);
    haptic("notification", "success");
  }

  function failActiveCall() {
    if (!currentCall) return;
    const callId = currentCall.id;
    send({ type: "hangup", callId: callId });
    cleanupCall();
    showError("Аудиоканал прервался. Проверьте сеть и дождитесь нового звонка.");
    haptic("notification", "error");
  }

  function declineCall() {
    if (currentState !== STATES.INCOMING || !currentCall) return;
    const callId = currentCall.id;
    send({ type: "decline", callId: callId });
    cleanupCall();
    setState(STATES.ENDED, {
      title: "Звонок отклонён",
      copy: "Агент получил ваш ответ",
    });
    setClosingConfirmation(false);
    haptic("impact", "light");
  }

  function hangupCall() {
    if (!currentCall) return;
    const callId = currentCall.id;
    const duration = getCallDuration();
    send({ type: "hangup", callId: callId });
    cleanupCall();
    setState(STATES.ENDED, {
      copy: duration ? "Длительность разговора: " + duration : "Можно закрыть мини-приложение",
    });
    setClosingConfirmation(false);
    haptic("impact", "medium");
  }

  function handleRemoteEnd(message) {
    if (!currentCall) return;
    if (message.callId && currentCall && normalizedCallId(message.callId) !== currentCall.id) return;
    const duration = getCallDuration();
    const reason = endedReason(message.reason);
    cleanupCall();
    setState(STATES.ENDED, {
      copy: duration ? reason + " Длительность: " + duration : reason,
    });
    setClosingConfirmation(false);
    haptic("impact", "light");
  }

  function handleServerError(message) {
    const text = localizedServerError(normalizedText(message.message, 360));
    // Authentication errors are deterministic; avoid a reconnect loop and let
    // the user explicitly retry after reopening Telegram or fixing access.
    if (!authenticated) intentionalSocketClose = true;
    if (currentCall) cleanupCall();
    showError(text);
    haptic("notification", "error");
  }

  function toggleMute() {
    if (currentState !== STATES.ACTIVE || !localStream) return;
    muted = !muted;
    localStream.getAudioTracks().forEach(function (track) { track.enabled = !muted; });
    renderMute();
    haptic("selection");
  }

  function renderMute() {
    elements.muteButton.setAttribute("aria-pressed", String(muted));
    elements.muteLabel.textContent = muted ? "Вкл. микрофон" : "Выкл. микрофон";
    elements.mutedBadge.hidden = !muted;
  }

  function cleanupCall() {
    ++mediaAttempt;
    clearInterval(timerInterval);
    clearTimeout(connectionFailureTimer);
    timerInterval = null;
    connectionFailureTimer = null;

    if (peerConnection) {
      peerConnection.removeEventListener("connectionstatechange", handlePeerConnectionState);
      peerConnection.close();
      peerConnection = null;
    }
    stopStream(localStream);
    stopStream(remoteStream);
    stopStream(elements.remoteAudio.srcObject);
    localStream = null;
    remoteStream = null;
    currentCall = null;
    callRequestPending = false;
    clearTimeout(callRequestTimer);
    callRequestTimer = null;
    elements.startCallButton.disabled = false;
    callStartedAt = 0;
    muted = false;
    renderMute();
    elements.remoteAudio.pause();
    elements.remoteAudio.srcObject = null;
    elements.audioGate.hidden = true;
    elements.timer.textContent = "00:00";
  }

  function stopStream(stream) {
    if (!stream || typeof stream.getTracks !== "function") return;
    stream.getTracks().forEach(function (track) { track.stop(); });
  }

  async function tryPlayRemoteAudio() {
    if (!elements.remoteAudio.srcObject) return;
    try {
      await elements.remoteAudio.play();
      elements.audioGate.hidden = true;
    } catch (_error) {
      elements.audioGate.hidden = false;
    }
  }

  function unlockAudio() {
    haptic("impact", "light");
    tryPlayRemoteAudio();
  }

  function waitForIceGatheringComplete(connection, timeoutMs) {
    if (connection.iceGatheringState === "complete") return Promise.resolve();

    return new Promise(function (resolve, reject) {
      let timeout;
      function finish(error) {
        clearTimeout(timeout);
        connection.removeEventListener("icegatheringstatechange", onStateChange);
        if (error) reject(error); else resolve();
      }
      function onStateChange() {
        if (connection.iceGatheringState === "complete") finish();
      }
      connection.addEventListener("icegatheringstatechange", onStateChange);
      timeout = setTimeout(function () {
        // Some Telegram Desktop/network combinations block public STUN while
        // still producing a usable host candidate. Non-trickle ICE used to
        // discard that candidate and fail the call before the offer reached
        // the server. Send the partial SDP when at least one candidate exists;
        // fail only when gathering produced nothing at all.
        const description = connection.localDescription;
        const sdp = description && typeof description.sdp === "string" ? description.sdp : "";
        if (/(?:^|\r?\n)a=candidate:/m.test(sdp)) finish();
        else finish(new Error("ICE_GATHERING_TIMEOUT"));
      }, timeoutMs);
    });
  }

  function setState(state, overrides) {
    const base = STATE_COPY[state] || STATE_COPY.error;
    const options = overrides || {};
    currentState = state;
    elements.body.dataset.state = state;
    elements.body.dataset.callConnecting = String(Boolean(options.callConnecting));
    elements.eyebrow.textContent = options.eyebrow || base.eyebrow;
    elements.title.textContent = options.title || base.title;
    elements.copy.textContent = options.copy || base.copy;

    const note = normalizedText(options.note, 320);
    elements.callNote.hidden = !note;
    elements.callNoteText.textContent = note;
    if (state !== STATES.ACTIVE) elements.audioGate.hidden = true;

    const announcement = elements.title.textContent + ". " + elements.copy.textContent;
    elements.announcer.textContent = "";
    window.setTimeout(function () { elements.announcer.textContent = announcement; }, 40);
  }

  function setConnectionStatus(online, label) {
    elements.connectionPill.dataset.online = String(Boolean(online));
    elements.connectionLabel.textContent = label;
  }

  function showError(copy) {
    setState(STATES.ERROR, { copy: copy });
    setClosingConfirmation(false);
  }

  function retryConnection() {
    haptic("impact", "light");
    cleanupSocket();
    reconnectAttempt = 0;
    setState(STATES.CONNECTING);
    connect();
  }

  function cleanupSocket() {
    intentionalSocketClose = true;
    ++socketGeneration;
    clearTimeout(authTimer);
    clearTimeout(reconnectTimer);
    clearInterval(heartbeatTimer);
    if (socket) {
      tryCall(function () { socket.close(1000, "Reconnect"); });
      socket = null;
    }
    authenticated = false;
  }

  function scheduleReconnect() {
    if (reconnectTimer || intentionalSocketClose || !navigator.onLine) return;
    const delay = Math.min(1_000 * Math.pow(2, reconnectAttempt), 20_000) + Math.floor(Math.random() * 500);
    reconnectAttempt += 1;
    reconnectTimer = setTimeout(function () {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  function startHeartbeat() {
    clearInterval(heartbeatTimer);
    heartbeatTimer = setInterval(function () {
      send({ type: "ping" });
    }, config.heartbeatMs);
  }

  function send(payload) {
    if (!socket || socket.readyState !== WebSocket.OPEN) return false;
    try {
      socket.send(JSON.stringify(payload));
      return true;
    } catch (_error) {
      return false;
    }
  }

  function finish() {
    haptic("impact", "light");
    if (telegram && typeof telegram.close === "function") {
      telegram.close();
    } else if (authenticated) {
      setState(STATES.IDLE);
    }
  }

  function leavePage() {
    if (currentCall) send({ type: "hangup", callId: currentCall.id });
    intentionalSocketClose = true;
    cleanupCall();
  }

  function requestTelegramFullscreen() {
    if (!telegram) return;
    tryCall(function () { telegram.expand(); });
    if (typeof telegram.requestFullscreen === "function" && !telegram.isFullscreen) {
      tryCall(function () { telegram.requestFullscreen(); });
    }
  }

  function setClosingConfirmation(enabled) {
    if (!telegram) return;
    if (enabled && typeof telegram.enableClosingConfirmation === "function") {
      tryCall(function () { telegram.enableClosingConfirmation(); });
    } else if (!enabled && typeof telegram.disableClosingConfirmation === "function") {
      tryCall(function () { telegram.disableClosingConfirmation(); });
    }
  }

  function haptic(kind, style) {
    if (!telegram || !telegram.HapticFeedback) return;
    const feedback = telegram.HapticFeedback;
    tryCall(function () {
      if (kind === "impact") feedback.impactOccurred(style || "light");
      if (kind === "notification") feedback.notificationOccurred(style || "success");
      if (kind === "selection") feedback.selectionChanged();
    });
  }

  function updateOnlineStatus() {
    elements.offlineBanner.hidden = navigator.onLine;
  }

  function updateCallTimer() {
    if (!callStartedAt) return;
    elements.timer.textContent = formatDuration(Math.floor((Date.now() - callStartedAt) / 1_000));
  }

  function getCallDuration() {
    if (!callStartedAt) return "";
    return formatDuration(Math.max(0, Math.floor((Date.now() - callStartedAt) / 1_000)));
  }

  function formatDuration(seconds) {
    const hours = Math.floor(seconds / 3_600);
    const minutes = Math.floor((seconds % 3_600) / 60);
    const remaining = seconds % 60;
    if (hours) return pad(hours) + ":" + pad(minutes) + ":" + pad(remaining);
    return pad(minutes) + ":" + pad(remaining);
  }

  function pad(number) {
    return String(number).padStart(2, "0");
  }

  function endedReason(reason) {
    const reasons = {
      completed: "Разговор завершён.",
      remote_hangup: "Агент завершил звонок.",
      agent_hangup: "Агент завершил звонок.",
      timeout: "Время ожидания истекло.",
      cancelled: "Агент отменил звонок.",
      declined: "Звонок отклонён.",
      connection_lost: "Соединение прервалось.",
    };
    return reasons[reason] || "Звонок завершён.";
  }

  function mediaErrorMessage(error) {
    if (!error) return "Не удалось включить микрофон.";
    if (error.name === "NotAllowedError" || error.name === "SecurityError") {
      return "Telegram не получил доступ к микрофону. Разрешите доступ в настройках и откройте Ringback снова.";
    }
    if (error.name === "NotFoundError") {
      return "На устройстве не найден доступный микрофон.";
    }
    if (error.name === "NotReadableError") {
      return "Микрофон занят другим приложением. Закройте его и попробуйте снова.";
    }
    if (error.message === "ICE_GATHERING_TIMEOUT") {
      return "Не удалось подготовить сетевой канал. Проверьте Wi‑Fi или мобильный интернет.";
    }
    return "Не удалось начать аудиозвонок. Дождитесь следующего входящего звонка.";
  }

  function localizedServerError(raw) {
    if (!raw) return "Сервер Ringback не смог обработать запрос.";
    const lower = raw.toLowerCase();
    if (lower.includes("no telegram user is allowed") || lower.includes("not allowed")) {
      return "Ваш Telegram-профиль пока не добавлен в Ringback. Проверьте список разрешённых пользователей на сервере.";
    }
    if (lower.includes("authentication") || lower.includes("initdata") || lower.includes("signature") || lower.includes("stale")) {
      return "Не удалось подтвердить Telegram-профиль. Закройте Ringback и откройте его заново из меню бота.";
    }
    if (lower.includes("call is no longer available")) {
      return "Этот звонок уже завершён. Дождитесь следующего входящего звонка.";
    }
    if (lower.includes("webrtc negotiation failed") || lower.includes("invalid webrtc offer")) {
      return "Не удалось создать WebRTC-канал. Проверьте сеть и дождитесь нового звонка.";
    }
    return raw;
  }

  function normalizedCallId(value) {
    if (typeof value !== "string" && typeof value !== "number") return "";
    return String(value).trim().slice(0, 160);
  }

  function isLoopbackHost() {
    return window.location.hostname === "localhost" ||
      window.location.hostname === "127.0.0.1" ||
      window.location.hostname === "::1";
  }

  function normalizedText(value, limit) {
    if (typeof value !== "string") return "";
    return value.replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim().slice(0, limit || 320);
  }

  function tryCall(callback) {
    try { callback(); } catch (_error) { /* Optional host capability. */ }
  }

  init();
})();

# Ringback Mini App frontend

This directory is a static, mobile-first Telegram Mini App. It has no build
step: serve `index.html`, `styles.css`, and `app.js` from the same HTTPS origin
as the Ringback server. The microphone API requires HTTPS outside localhost.

By default the client connects to `/ws` on the current origin. A deployment
that keeps signaling on another origin can define this before `app.js` loads:

```html
<script>
  window.RINGBACK_CONFIG = { websocketUrl: "wss://ringback.example.com/ws" };
</script>
```

## WebSocket protocol

Messages are UTF-8 JSON objects. Telegram authentication data is sent only in
the WebSocket and must never be added to the URL.

For local development only, the frontend permits an empty `initData` on
`localhost`, `127.0.0.1`, or `::1`. The server must still reject that unless
its explicit loopback-only development mode is enabled.

1. The browser opens `/ws` and immediately sends:

   ```json
   { "type": "auth", "initData": "<Telegram.WebApp.initData>" }
   ```

After authentication, an idle Mini App can ask the standalone agent to call
the authenticated user. It cannot choose another Telegram account:

```json
{ "type": "request_call" }
```

The server answers with `{"type":"call_request","accepted":true,"reason":"queued"}`
or rejects it as `busy`/`unavailable`; an accepted request is followed by the
normal `incoming` message.

2. After validating `initData` (including its age), the server replies:

   ```json
   {
     "type": "authenticated",
     "user": { "id": 123 },
     "iceServers": [{ "urls": "stun:stun.example.com:3478" }],
     "pendingCall": { "callId": "optional-id", "openingHint": "Optional context" }
   }
   ```

   `pendingCall` is optional. TURN credentials inside `iceServers` should be
   short-lived.

3. A new call is delivered as:

   ```json
   { "type": "incoming", "callId": "call-id", "openingHint": "Optional context" }
   ```

4. On accept, the Mini App requests microphone access, waits until ICE
   gathering is complete, and sends one non-trickle offer:

   ```json
   { "type": "offer", "callId": "call-id", "sdp": "...", "sdpType": "offer" }
   ```

5. The server completes the WebRTC negotiation with:

   ```json
   { "type": "answer", "callId": "call-id", "sdp": "...", "sdpType": "answer" }
   ```

6. Decline and hangup are client messages. Call completion is a server
   message:

   ```json
   { "type": "decline", "callId": "call-id" }
   { "type": "hangup", "callId": "call-id" }
   { "type": "ended", "callId": "call-id", "reason": "remote_hangup" }
   ```

7. Either side may send `{"type":"ping"}`; the other replies with
   `{"type":"pong"}`. A recoverable server failure is
   `{"type":"error","code":"...","message":"..."}`.

The browser intentionally does not send trickle ICE candidates. The server's
answer must likewise contain its gathered candidates in the SDP.

## Telegram integration

The app consumes Telegram theme variables and safe-area insets, expands on
launch, requests fullscreen when the user accepts a call, enables close
confirmation during a live call, and uses Telegram haptics for call actions.
It shows an explicit sound-unlock control if the WebView blocks remote-audio
autoplay.

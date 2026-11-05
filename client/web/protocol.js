// Verbindung zum Gateway über das WebSocket-Protokoll aus services/gateway/app.py.
//
// Ohne DOM und ohne Spielregeln: Die Oberfläche (app.js oder später eine
// richtige) hängt sich über onMessage/onStatus an und ruft die Methoden
// unten auf. Geprüft wird alles auf dem Server.

const STORAGE_ID = "queens.player_id";
const STORAGE_NAME = "queens.name";
const RECONNECT_START = 1000;
const RECONNECT_MAX = 8000;
const CLOSE_REPLACED = 4001; // durch neue Verbindung mit gleicher ID ersetzt
const CLOSE_REJECTED = 4400; // hello fehlte oder war ungültig

// sessionStorage statt localStorage: vier Tabs im selben Browser sollen vier
// Spieler sein. Kann in privaten Fenstern fehlen, daher try/catch.
export const session = {
  get(key) {
    try { return sessionStorage.getItem(key); } catch { return null; }
  },
  set(key, value) {
    try { sessionStorage.setItem(key, value); } catch { /* egal */ }
  },
  clear() {
    try { sessionStorage.removeItem(STORAGE_ID); sessionStorage.removeItem(STORAGE_NAME); } catch { /* egal */ }
  },
};

// crypto.randomUUID gibt es nur über https/localhost, getRandomValues überall.
export function newActionId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function gatewayUrl(loc = window.location) {
  return `${loc.protocol === "https:" ? "wss" : "ws"}://${loc.host}/ws`;
}

export class QueensClient {
  // onMessage(message): jede Nachricht vom Gateway (veraltete Sichten schon verworfen)
  // onStatus(status, detail): "verbinde" | "verbunden" | "getrennt" | "ersetzt" | "abgelehnt"
  constructor({ url = gatewayUrl(), onMessage = () => {}, onStatus = () => {} } = {}) {
    this.url = url;
    this.onMessage = onMessage;
    this.onStatus = onStatus;
    this.playerId = session.get(STORAGE_ID);
    this.name = session.get(STORAGE_NAME);
    this.ws = null;
    this.delay = RECONNECT_START;
    this.lastVersion = -1;
    this.stopped = false;
  }

  get hasSession() {
    return Boolean(this.playerId && this.name);
  }

  connect(name = this.name) {
    this.name = name;
    session.set(STORAGE_NAME, name);
    this.stopped = false;
    this.onStatus("verbinde");
    const ws = new WebSocket(this.url);
    this.ws = ws;
    ws.onopen = () => {
      ws.send(JSON.stringify({ op: "hello", name: this.name, player_id: this.playerId }));
    };
    ws.onmessage = (event) => {
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      this.handle(message);
    };
    ws.onclose = (event) => {
      if (this.ws === ws) this.ws = null;
      if (this.stopped) return;
      if (event.code === CLOSE_REPLACED) { this.onStatus("ersetzt"); return; }
      if (event.code === CLOSE_REJECTED) { this.onStatus("abgelehnt"); return; }
      this.onStatus("getrennt", this.delay);
      setTimeout(() => { if (!this.stopped) this.connect(); }, this.delay);
      this.delay = Math.min(this.delay * 2, RECONNECT_MAX);
    };
  }

  disconnect() {
    this.stopped = true;
    if (this.ws) this.ws.close();
  }

  handle(message) {
    if (message.op === "welcome") {
      this.playerId = message.player_id;
      session.set(STORAGE_ID, this.playerId);
      this.delay = RECONNECT_START;
      this.onStatus("verbunden", message.reconnected);
    } else if (message.op === "game_started") {
      this.lastVersion = -1;
    } else if (message.op === "state") {
      const version = message.view.version ?? 0;
      if (version < this.lastVersion) return; // veraltete Sicht
      this.lastVersion = version;
    }
    this.onMessage(message);
  }

  send(message) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    this.ws.send(JSON.stringify(message));
    return true;
  }

  // --- Lobby ---
  createLobby() { return this.send({ op: "create_lobby" }); }
  joinLobby(code) { return this.send({ op: "join_lobby", lobby_id: code.trim().toUpperCase() }); }
  leaveLobby() { return this.send({ op: "leave_lobby" }); }
  ready(flag = true) { return this.send({ op: "ready", ready: flag }); }

  // --- Spiel: jede Aktion mit eigener action_id ---
  action(type, fields = {}) {
    return this.send({ op: "action", action: { type, action_id: newActionId(), ...fields } });
  }
  peekInitial(slots) { return this.action("PeekInitial", { slots }); }
  drawFromDeck() { return this.action("DrawFromDeck"); }
  takeFromDiscard() { return this.action("TakeFromDiscard"); }
  swapWithHand(slot) { return this.action("SwapWithHand", { slot }); }
  discardDrawn() { return this.action("DiscardDrawn"); }
  useJack(first, second) { return this.action("UseJack", { first, second }); }
  useKing(target) { return this.action("UseKing", { target }); }
  skipSpecial() { return this.action("SkipSpecial"); }
  knock() { return this.action("Knock"); }
  throwIn(slot) { return this.action("ThrowIn", { slot }); }
}

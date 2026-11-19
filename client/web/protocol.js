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

export function gatewayUrl(loc = window.location, host = loc.host) {
  return `${loc.protocol === "https:" ? "wss" : "ws"}://${host}/ws`;
}

// Woher kommt die Seite? Vom Gateway (Port 8000) oder vom Leitstand
// (control.py, Port 8080). Der Leitstand beantwortet /api/config und nennt den
// Gateway-Port. Überschreiben: ?gateway=host:port bzw. ?control=http://host:port
export async function resolveEndpoints(loc = window.location) {
  const params = new URLSearchParams(loc.search);
  let gateway = params.get("gateway");
  let control = params.get("control");
  let config = null;
  try {
    const response = await fetch("/api/config", { cache: "no-store", signal: AbortSignal.timeout(1500) });
    config = response.ok ? await response.json() : null;
    if (config?.control) {
      control ??= loc.origin;
      gateway ??= `${loc.hostname}:${config.gateway_port}`;
    }
  } catch { /* Seite kommt vom Gateway */ }
  gateway ??= loc.host;
  control ??= `${loc.protocol}//${loc.hostname}:8080`;
  return {
    ws: gatewayUrl(loc, gateway),
    control: control.replace(/\/$/, ""),
    gateway,
    invite: inviteTargets(loc, gateway, config),
  };
}

// /api/config neu abfragen: Die Netzwerkadressen des Hosts ändern sich, wenn er
// das Netz wechselt (z. B. vom WLAN auf den Hotspot), ohne dass die Seite neu lädt.
export async function fetchConfig() {
  try {
    const response = await fetch("/api/config", { cache: "no-store", signal: AbortSignal.timeout(1500) });
    return response.ok ? await response.json() : null;
  } catch {
    return null;
  }
}

const isLoopback = (host) => host === "localhost" || host.startsWith("127.") || host === "[::1]" || host === "::1";

// Welche Art Netz steckt hinter einer IPv4-Adresse? Nur für die Beschriftung.
export function networkLabel(ip) {
  const [a, b, c, d] = ip.split(".").map(Number);
  if (a === 100 && b >= 64 && b <= 127) return "Tailscale";
  if (a === 192 && b === 168 && c === 137) return "Windows-Hotspot dieses Rechners";
  if (a === 172 && b === 20 && c === 10) return "iPhone-Hotspot";
  if (a === 169 && b === 254) return "ohne Netz (link-local)";
  if (a === 172 && b >= 16 && b <= 31 && d === 1) return "vermutlich virtuell (WSL/Hyper-V/Docker)";
  if (a === 10 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168)) return "WLAN / Hotspot";
  return "WLAN / LAN (Internetverbindung)";
}

const UNLIKELY = ["vermutlich virtuell (WSL/Hyper-V/Docker)", "ohne Netz (link-local)"];

// Einladungsadressen: immer das Gateway (den Leitstand erreichen andere Rechner
// nicht), statt 127.0.0.1/localhost jede Netzwerkadresse des Hosts. Die erste
// ist die wahrscheinlichste (Standardroute), virtuelle Adapter stehen hinten.
// { links: [{base, ip, label}], reachable: bool, reason?: "local" | "no-ip" }
export function inviteTargets(loc, gateway, config) {
  const target = new URL(`${loc.protocol}//${gateway}`);
  const port = target.port ? `:${target.port}` : "";
  const ips = config?.lan_ips ?? [];
  const hosts = isLoopback(target.hostname) ? ips : [target.hostname, ...ips.filter((ip) => ip !== target.hostname)];
  const links = hosts.map((ip) => ({ ip, base: `${target.protocol}//${ip}${port}`, label: networkLabel(ip) }));
  // Eigener Hotspot zuerst (Mitspieler hängen dann fast sicher darin), virtuelle Adapter zuletzt.
  const rank = (link) => (link.label.startsWith("Windows-Hotspot") ? -1 : UNLIKELY.includes(link.label) ? 1 : 0);
  links.sort((x, y) => rank(x) - rank(y));
  if (!links.length) {
    return { links: [{ ip: target.hostname, base: target.origin, label: "nur dieser Rechner" }], reachable: false, reason: "no-ip" };
  }
  if (config?.public === false) return { links, reachable: false, reason: "local" };
  return { links, reachable: true };
}

// Kompatibel zu älterem Aufruf: nur die wahrscheinlichste Adresse.
export function inviteBase(loc, gateway, config) {
  const { links, reachable, reason } = inviteTargets(loc, gateway, config);
  return { base: links[0].base, reachable, reason };
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
    this.retryTimer = null;
  }

  get connected() {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  get hasSession() {
    return Boolean(this.playerId && this.name);
  }

  connect(name = this.name) {
    this.name = name;
    session.set(STORAGE_NAME, name);
    this.stopped = false;
    clearTimeout(this.retryTimer);
    if (this.ws) { const old = this.ws; this.ws = null; old.close(); }
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
      if (this.ws !== ws) return; // bereits ersetzt
      this.ws = null;
      if (this.stopped) return;
      if (event.code === CLOSE_REPLACED) { this.onStatus("ersetzt"); return; }
      if (event.code === CLOSE_REJECTED) { this.onStatus("abgelehnt"); return; }
      this.onStatus("getrennt", this.delay);
      this.retryTimer = setTimeout(() => { if (!this.stopped) this.connect(); }, this.delay);
      this.delay = Math.min(this.delay * 2, RECONNECT_MAX);
    };
  }

  // Nach einem Serverstart nicht auf den Backoff warten.
  reconnectNow() {
    if (this.stopped || this.connected || !this.hasSession && !this.name) return;
    this.delay = RECONNECT_START;
    this.connect();
  }

  disconnect() {
    this.stopped = true;
    clearTimeout(this.retryTimer);
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

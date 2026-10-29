/* Tells the server this tab is open, so DevPanel can show who is on the site right now. A ping every 30 s while
   the tab is visible, one more on every page change, and a goodbye beacon when the tab is hidden or closed. Only the
   anonymous id the app already keeps and the page kind leave the browser. */

import { clientId } from "./api";

const EVERY_MS = 30_000;
let path = location.pathname;
let playing = false;
let timer = 0;

function send(url: string, body: object, beacon = false) {
  const id = clientId();
  if (!id) return;
  const data = JSON.stringify({ id, ...body });
  if (beacon && navigator.sendBeacon?.(url, data)) return;
  fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: data, keepalive: true })
    .catch(() => {});
}

function ping() {
  if (document.visibilityState !== "visible") return;
  send("/api/presence", { path, play: playing });
}

function schedule() {
  window.clearInterval(timer);
  timer = window.setInterval(ping, EVERY_MS);
}

export function startPresence() {
  ping();
  schedule();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") { ping(); schedule(); }
    else { window.clearInterval(timer); send("/api/presence/leave", {}, true); }
  });
  window.addEventListener("pagehide", () => send("/api/presence/leave", {}, true));
}

/** The router calls this on every navigation. */
export function presencePage(next: string) {
  if (next === path) return;
  path = next;
  ping();
}

/** The arcade run marks its visitors as playing. */
export function presencePlaying(on: boolean) {
  if (on === playing) return;
  playing = on;
  ping();
}

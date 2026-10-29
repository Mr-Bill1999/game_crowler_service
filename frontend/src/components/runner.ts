/* The arcade run on the entrance stage: an ASCII world scrolls past the hero, who stays put on the
   left third and only jumps, ducks and throws fireballs. Drawn on a 2D canvas under the hero, in
   the same 6×11 character grid and inks, so the world and the wizard read as one drawing.

   What comes at him:
   - rocks and spikes: jump;
   - slimes and goblins: jump, or burn them (goblins walk towards him, so they arrive sooner);
   - bats at head height: duck under, or burn;
   - hanging logs: duck, nothing else helps;
   - coins in arcs over the ground things: a reward for the jump.

   Three lives; the world speeds up until a typical run ends in about two minutes. */

const CELL_W = 6;
const CELL_H = 11;
// 10px of a monospace face advances exactly 6px (0.6em), so a whole row of glyphs drawn as one
// string lands on the 6×11 grid: one fillText per row instead of one per glyph, which is what
// keeps phones at a steady frame rate.
const FONT = `500 10px "JetBrains Mono", ui-monospace, monospace`;
// Inside the run everything is in colour — the contrast with the quiet ink-on-paper entrance.
// Ink on paper, like the rest of the entrance: things differ by density, not by colour.
const INK = "#22261f";
const SAGE = "#7c8a70";
const C = { rock: INK, spikes: INK, slime: INK, goblin: INK, bat: INK, log: INK, coin: SAGE, pop: SAGE, hurt: INK };
const LIVES = 3;

type Kind = "rock" | "spikes" | "slime" | "goblin" | "bat" | "log" | "coin";
type Art = { frames: string[][]; color: string; kill: boolean; solid: boolean; fps?: number };

const ART: Record<Kind, Art> = {
  rock: { color: C.rock, kill: false, solid: true, frames: [[
    "  .--.  ",
    " /##%#\\ ",
    "/#%###%\\",
  ]] },
  spikes: { color: C.spikes, kill: false, solid: false, frames: [[
    " /\\/\\/\\ ",
    "/######\\",
  ]] },
  slime: { color: C.slime, kill: true, solid: false, fps: 4, frames: [[
    "  .---.  ",
    " / o o \\ ",
    "(   ~   )",
    " `~~~~~` ",
  ], [
    "         ",
    " .-----. ",
    "( o   o )",
    "`~~~~~~~`",
  ]] },
  goblin: { color: C.goblin, kill: true, solid: false, fps: 7, frames: [[
    " ,^^^, ",
    " (o.o) ",
    " <|#|> ",
    "  / \\  ",
  ], [
    " ,^^^, ",
    " (o.o) ",
    " <|#|> ",
    "  | |  ",
  ]] },
  bat: { color: C.bat, kill: true, solid: false, fps: 8, frames: [[
    "/\\_     _/\\",
    "   \\(oo)/  ",
    "     vv    ",
  ], [
    "           ",
    " \\_/(oo)\\_/",
    "     vv    ",
  ]] },
  log: { color: C.log, kill: false, solid: true, frames: [[
    "[##########]",
  ]] },
  coin: { color: C.coin, kill: false, solid: false, fps: 6, frames: [["(o)"], ["(O)"], ["(0)"], ["(O)"]] },
};

type Thing = { kind: Kind; x: number; lift: number; dead: number; hit: boolean; seed: number; walk: number };
type Spark = { x: number; y: number; vx: number; vy: number; ch: string; color: string; life: number; age: number };
type Popup = { x: number; y: number; text: string; age: number };
export type Box = { left: number; right: number; top: number; bottom: number };

export type StepIn = {
  /** The hero's box in stage pixels, or null before he is loaded. */
  hero: Box | null;
  /** The fireball's head in stage pixels, if one is flying. */
  fire: { x: number; y: number } | null;
};
export type StepOut = { fireSpent: boolean; heroHit: boolean };

const rand = (a: number, b: number) => a + Math.random() * (b - a);
const pick = <T,>(items: [T, number][]) => {
  let r = Math.random() * items.reduce((s, [, w]) => s + w, 0);
  for (const [item, w] of items) if ((r -= w) <= 0) return item;
  return items[items.length - 1][0];
};

export function createRunner(host: HTMLElement, onHud: (score: number, lives: number) => void) {
  const touch = window.matchMedia("(pointer: coarse)").matches || window.innerWidth <= 720;
  const canvas = document.createElement("canvas");
  canvas.className = "entry-world";
  host.prepend(canvas);
  const ctx = canvas.getContext("2d")!;

  let w = 0;
  let h = 0;
  let floor = 0; // the stage y of the hero's feet
  let heroH = 120; // the standing hero's height in pixels
  let heroX = 0; // the stage x of the hero's centre

  let running = false;
  let over = false;
  let t = 0; // seconds into the run
  let scroll = 0; // pixels travelled
  let speedNow = 0;
  let nextIn = 0;
  let score = 0;
  let lives = LIVES;
  let hurt = 0; // seconds of blinking safety after a hit
  let things: Thing[] = [];
  let sparks: Spark[] = [];
  let popups: Popup[] = [];
  let shake = 0;
  let fill = 0; // the scene's fill fading in at the start

  // About three seconds across a wide stage; never so slow that a jump cannot clear a rock.
  const base = () => Math.max(200, w / 3);
  const factor = () => Math.min(1 + t / 55, 2.5);

  const layout = (width: number, height: number, feet: number, heroHeight: number, heroCentre: number) => {
    const dpr = Math.min(window.devicePixelRatio || 1, touch ? 1.5 : 2);
    w = width;
    h = height;
    floor = feet;
    heroH = heroHeight;
    heroX = heroCentre;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  };

  /** The row whose top is the floor: things stand on the row above it. */
  const floorRow = () => Math.round(floor / CELL_H);

  const size = (kind: Kind) => {
    const f = ART[kind].frames[0];
    return { cols: f[0].length, rows: f.length };
  };

  /** A thing's box in stage pixels. */
  const boxOf = (th: Thing): Box => {
    const { cols, rows } = size(th.kind);
    const bottom = floorRow() * CELL_H - th.lift;
    return { left: th.x, right: th.x + cols * CELL_W, top: bottom - rows * CELL_H, bottom };
  };

  const overlap = (a: Box, b: Box) => a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;

  const spawn = () => {
    const x = w + CELL_W * 2;
    const kinds: [Kind, number][] = [["rock", 3], ["spikes", 3], ["slime", 3]];
    if (t > 14) kinds.push(["goblin", 2.5], ["bat", 2.5]);
    if (t > 32) kinds.push(["log", 2]);
    if (t > 60) kinds.push(["bat", 1.5], ["goblin", 1]);
    const kind = pick(kinds);
    // Bats and logs hang a little over two-thirds of his height: standing or jumping hits them,
    // ducking passes under.
    const high = Math.round(heroH * 0.68);
    const lift = kind === "bat" ? high : kind === "log" ? high : 0;
    things.push({ kind, x, lift, dead: 0, hit: false, seed: Math.random() * 10, walk: kind === "goblin" ? base() * 0.3 : 0 });
    // Coins in an arc over a ground thing, where a good jump goes anyway.
    if (lift === 0 && Math.random() < 0.35) {
      const { cols } = size(kind);
      const mid = x + (cols * CELL_W) / 2;
      for (let i = -2; i <= 2; i++) {
        const arc = 1 - (i * i) / 6;
        things.push({ kind: "coin", x: mid + i * CELL_W * 4 - CELL_W, lift: Math.round(heroH * (0.25 + 0.42 * arc)), dead: 0, hit: false, seed: i, walk: things[things.length - 1].walk });
      }
    }
    // The gap to the next one, in seconds: never shorter than a jump and a breath.
    nextIn = Math.max(0.95, rand(1.1, 2.0) * (1 - t / 260));
  };

  const burst = (x: number, y: number, color: string, n = 14) => {
    for (let i = 0; i < n; i++) {
      const a = Math.random() * Math.PI * 2;
      const v = rand(60, 220);
      sparks.push({ x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v - 60, ch: "*+.:'"[i % 5], color, life: rand(0.35, 0.7), age: 0 });
    }
  };

  const start = () => {
    running = true;
    over = false;
    t = 0;
    scroll = 0;
    nextIn = 1.6;
    score = 0;
    lives = LIVES;
    hurt = 0;
    things = [];
    sparks = [];
    popups = [];
    fill = 0;
    onHud(0, lives);
  };

  const stop = () => {
    running = false;
    over = false;
    things = [];
    sparks = [];
    popups = [];
    ctx.clearRect(0, 0, w, h);
  };

  const cell = (col: number, row: number, ch: string) => ctx.fillText(ch, col * CELL_W, row * CELL_H + CELL_H / 2);
  // Glyphs gathered per row, then each row drawn as one string (gaps as spaces).
  const rowsBuf = new Map<number, string[]>();
  const put = (col: number, row: number, ch: string) => {
    if (col < 0 || row < 0) return;
    let r = rowsBuf.get(row);
    if (!r) { r = []; rowsBuf.set(row, r); }
    r[col] = ch;
  };
  const flush = () => {
    for (const [row, r] of rowsBuf) {
      let start = 0;
      while (start < r.length && r[start] === undefined) start++;
      let line = "";
      for (let i = start; i < r.length; i++) line += r[i] ?? " ";
      if (line) ctx.fillText(line, start * CELL_W, row * CELL_H + CELL_H / 2);
    }
    rowsBuf.clear();
  };

  const drawArt = (th: Thing, now: number) => {
    const art = ART[th.kind];
    const frame = art.frames[art.fps ? Math.floor(now / 1000 * art.fps + th.seed) % art.frames.length : 0];
    const b = boxOf(th);
    const bob = th.kind === "bat" ? Math.round(Math.sin(now / 160 + th.seed) * 0.6) : 0;
    const col0 = Math.round(b.left / CELL_W);
    const row0 = Math.round(b.top / CELL_H) + bob;
    ctx.globalAlpha = th.dead ? Math.max(0, 1 - (t - th.dead) * 4) : 0.92;
    ctx.fillStyle = art.color;
    frame.forEach((line, r) => ctx.fillText(line, col0 * CELL_W, (row0 + r) * CELL_H + CELL_H / 2));
    if (th.kind === "log") {
      // The chains it hangs from, up to the top of the stage.
      ctx.globalAlpha = 0.45;
      for (let r = row0 - 1; r >= 0; r--) { put(col0 + 2, r, ":"); put(col0 + 9, r, ":"); }
      flush();
    }
  };

  const drawWorld = (now: number) => {
    ctx.clearRect(0, 0, w, h);
    ctx.font = FONT;
    ctx.textBaseline = "middle";
    ctx.save();
    if (shake > 0) ctx.translate(Math.round(rand(-1, 1) * shake * 6), 0);
    const cols = Math.ceil(w / CELL_W) + 1;
    const fr = floorRow();

    // Clouds, slow and faint.
    ctx.fillStyle = INK;
    ctx.globalAlpha = 0.16 * fill;
    const cloudOff = scroll * 0.06;
    for (let i = 0; i < 5; i++) {
      const span = w + 240;
      const x = ((i * 337 + 80 - cloudOff) % span + span) % span - 120;
      const row = 2 + ((i * 5) % 8);
      const c0 = Math.round(x / CELL_W);
      ["   .--.    ", " .(    ).-.", "(___.____._)"].forEach((line, r) => {
        ctx.fillText(line, c0 * CELL_W, (row + r) * CELL_H + CELL_H / 2);
      });
    }

    // Two layers of hills, only their ridges, traced in slopes.
    const hills = (speed: number, height: number, seed: number, alpha: number) => {
      const off = scroll * speed;
      const at = (x: number) => (Math.sin(x * 0.009 + seed) * 0.35 + Math.sin(x * 0.023 + seed * 2) * 0.2 + Math.sin(x * 0.004 + seed) * 0.45 + 1) / 2 * height;
      ctx.globalAlpha = alpha * fill;
      for (let c = 0; c < cols; c++) {
        const x = c * CELL_W;
        const a = Math.round(at(x - CELL_W + off) / CELL_H);
        const b = Math.round(at(x + off) / CELL_H);
        const row = fr - 1 - b;
        put(c, row, b > a ? "/" : b < a ? "\\" : "_");
        const v = Math.sin(Math.floor((x + off) / CELL_W) * 7.13 + seed) * 9301.7;
        if (v - Math.floor(v) > 0.93) put(c, row + 2, ".");
      }
      flush();
    };
    hills(0.12, h * 0.15, 1.7, 0.12);
    hills(0.3, h * 0.06, 4.1, 0.22);

    // The ground he runs on and a little rubble under it, at full speed.
    const shift = Math.floor(scroll / CELL_W);
    const groundAt = (c: number) => { const v = Math.sin((c + shift) * 12.9898) * 43758.5453; return v - Math.floor(v); };
    ctx.globalAlpha = 0.6 * fill;
    for (let c = 0; c < cols; c++) put(c, fr, groundAt(c) > 0.86 ? "=" : "-");
    flush();
    ctx.globalAlpha = 0.28 * fill;
    for (let c = 0; c < cols; c++) {
      const v = groundAt(c * 7 + 3);
      if (v > 0.7) put(c, fr + 1, v > 0.9 ? "," : ".");
      if (v < 0.12) put(c, fr + 2, "'");
    }
    flush();
    ctx.globalAlpha = 1;

    for (const th of things) drawArt(th, now);

    for (const s of sparks) {
      ctx.globalAlpha = Math.max(0, 1 - s.age / s.life);
      ctx.fillStyle = s.color;
      cell(Math.round(s.x / CELL_W), Math.round(s.y / CELL_H), s.ch);
    }
    ctx.fillStyle = C.pop;
    for (const p of popups) {
      ctx.globalAlpha = Math.max(0, 1 - p.age / 0.9);
      const row = Math.round((p.y - p.age * 40) / CELL_H);
      ctx.fillText(p.text, Math.round(p.x / CELL_W) * CELL_W, row * CELL_H + CELL_H / 2);
    }
    ctx.restore();
    ctx.globalAlpha = 1;
  };

  const step = (dt: number, now: number, input: StepIn): StepOut => {
    const out: StepOut = { fireSpent: false, heroHit: false };
    if (!running) return out;
    if (over) {
      // The world eases to a stop under the game-over card.
      speedNow = Math.max(0, speedNow - speedNow * dt * 4);
    } else {
      t += dt;
      speedNow = base() * factor();
      score += factor() * dt * 12;
      nextIn -= dt;
      if (nextIn <= 0) spawn();
    }
    fill = Math.min(1, fill + dt * 2.5);
    const dx = speedNow * dt;
    scroll += dx;
    hurt = Math.max(0, hurt - dt);
    shake = Math.max(0, shake - dt * 3);

    for (const th of things) th.x -= dx + (over || th.dead ? 0 : th.walk * dt);

    if (!over && input.hero) {
      const hero = input.hero;
      for (const th of things) {
        if (th.dead || th.hit) continue;
        const b = boxOf(th);
        if (th.kind === "coin") {
          if (overlap(hero, b)) {
            th.dead = t;
            score += 25;
            burst((b.left + b.right) / 2, (b.top + b.bottom) / 2, C.coin, 6);
            popups.push({ x: b.left, y: b.top, text: "+25", age: 0 });
          }
          continue;
        }
        // A forgiving box: the art's outline is softer than its cells.
        const soft = { left: b.left + CELL_W, right: b.right - CELL_W, top: b.top + 3, bottom: b.bottom - 2 };
        if (hurt <= 0 && overlap(hero, soft)) {
          th.hit = true;
          out.heroHit = true;
          lives -= 1;
          hurt = 1.4;
          shake = 1;
          burst(heroX, (hero.top + hero.bottom) / 2, C.hurt, 10);
          if (lives <= 0) over = true;
        }
      }
    }

    if (input.fire && !over) {
      const f = input.fire;
      for (const th of things) {
        if (th.dead || th.kind === "coin") continue;
        const b = boxOf(th);
        const reach = 22;
        if (f.x > b.left && f.x < b.right && f.y > b.top - reach && f.y < b.bottom + reach) {
          if (ART[th.kind].kill) {
            th.dead = t;
            score += 100;
            burst((b.left + b.right) / 2, (b.top + b.bottom) / 2, ART[th.kind].color, 16);
            popups.push({ x: b.left, y: b.top, text: "+100", age: 0 });
            out.fireSpent = true;
          } else if (ART[th.kind].solid) {
            out.fireSpent = true;
          }
          if (out.fireSpent) break;
        }
      }
    }

    for (const s of sparks) { s.age += dt; s.x += s.vx * dt - dx; s.y += s.vy * dt; s.vy += 420 * dt; }
    for (const p of popups) { p.age += dt; p.x -= dx; }
    things = things.filter((th) => th.x > -CELL_W * 16 && !(th.dead && t - th.dead > 0.3));
    sparks = sparks.filter((s) => s.age < s.life);
    popups = popups.filter((p) => p.age < 0.9);

    drawWorld(now);
    onHud(Math.floor(score), lives);
    return out;
  };

  return {
    layout,
    start,
    stop,
    step,
    get over() { return over; },
    get hurt() { return hurt; },
    get speed() { return running && !over ? factor() : 0; },
    get score() { return Math.floor(score); },
    dispose() { canvas.remove(); },
  };
}

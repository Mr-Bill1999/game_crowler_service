/* One place for the entrance controls: the 3D gamepad writes here, the ASCII hero reads.
   The d-pad is a held direction (x right, y down); face buttons ask for an action. */

export type Action = "jump" | "wave" | "dance" | "fireball";
export type Dir = { x: number; y: number };

type Listener = (action: Action) => void;
const listeners = new Set<Listener>();

export const padInput = {
  dpad: { x: 0, y: 0 } as Dir,
  /** The pad's top edge on screen, as a share of its box height from the top; the hero stands on it. */
  top: null as number | null,
  act(action: Action) {
    listeners.forEach((listener) => listener(action));
  },
  onAct(listener: Listener) {
    listeners.add(listener);
    return () => { listeners.delete(listener); };
  },
};

/** The four face buttons of the 3D model, in a diamond like a common console pad. */
export const BUTTON_ACTION: Record<string, Action> = {
  Btn_2: "jump", // bottom
  Btn_1: "wave", // right
  Btn_3: "dance", // left
  Btn_0: "fireball", // top
};

/* The arcade run's score and lives, for the page's HUD; the game writes, React reads. */
export type Hud = { score: number; lives: number };
let hud: Hud = { score: 0, lives: 3 };
const hudListeners = new Set<() => void>();
export const gameHud = {
  get: () => hud,
  set(next: Hud) {
    if (next.score === hud.score && next.lives === hud.lives) return;
    hud = next;
    hudListeners.forEach((l) => l());
  },
  subscribe(l: () => void) {
    hudListeners.add(l);
    return () => { hudListeners.delete(l); };
  },
};

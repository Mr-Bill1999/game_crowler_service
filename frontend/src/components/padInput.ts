/* One place for the entrance controls: the 3D gamepad writes here, the ASCII hero reads.
   The left stick and the d-pad are held directions (x right, y down, length up to 1); face
   buttons ask for an action. */

export type Action = "jump" | "wave" | "dance" | "fireball";
export type Dir = { x: number; y: number };

type Listener = (action: Action) => void;
const listeners = new Set<Listener>();

export const padInput = {
  left: { x: 0, y: 0 } as Dir,
  right: { x: 0, y: 0 } as Dir,
  dpad: { x: 0, y: 0 } as Dir,
  act(action: Action) {
    listeners.forEach((listener) => listener(action));
  },
  onAct(listener: Listener) {
    listeners.add(listener);
    return () => { listeners.delete(listener); };
  },
};

/** The face buttons of the 3D model, laid out like a common console pad (A B X Y). */
export const BUTTON_ACTION: Record<string, Action> = {
  Btn_2: "jump", // bottom
  Btn_1: "wave", // right
  Btn_3: "dance", // left
  Btn_0: "fireball", // top
};

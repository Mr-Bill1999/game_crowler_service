/* One place for the entrance controls: the 3D gamepad writes here, the ASCII hero reads.
   The left stick is a direction (x right, y down, length up to 1); the d-pad turns the hero;
   face buttons ask for an action. */

export type Action = "jump" | "wave" | "dance" | "fireball";
export type Dir = { x: number; y: number };

type Listener = (action: Action) => void;
type TurnListener = (step: number) => void;
const listeners = new Set<Listener>();
const turners = new Set<TurnListener>();

export const padInput = {
  left: { x: 0, y: 0 } as Dir,
  right: { x: 0, y: 0 } as Dir,
  act(action: Action) {
    listeners.forEach((listener) => listener(action));
  },
  onAct(listener: Listener) {
    listeners.add(listener);
    return () => { listeners.delete(listener); };
  },
  /** A d-pad press: -1 turns the hero left, 1 right, 0 is "up". */
  turn(step: number) {
    turners.forEach((listener) => listener(step));
  },
  onTurn(listener: TurnListener) {
    turners.add(listener);
    return () => { turners.delete(listener); };
  },
};

/** The face buttons of the 3D model, laid out like a common console pad (A B X Y). */
export const BUTTON_ACTION: Record<string, Action> = {
  Btn_2: "jump", // bottom
  Btn_1: "wave", // right
  Btn_3: "dance", // left
  Btn_0: "fireball", // top
};

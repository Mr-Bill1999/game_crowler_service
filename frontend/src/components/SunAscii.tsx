import { useEffect, useState } from "react";

/* The sun in the top right corner of the stage, a simple ASCII drawing with twinkling rays. On
   the entrance it is the Play button, a ">" in the middle; in the run it smiles over the world. */

const RAYS = [
  ["    \\   |   /    ", "--               --", "    /   |   \\    "],
  ["     .  |  .     ", "-                 -", "     '  |  '     "],
];
const DISC = ["     .-'''-.     ", "    /       \\    ", "    |   >   |    ", "    \\       /    ", "     '-...-'     "];
const SMILE = ["     .-'''-.     ", "    /       \\    ", "    |  ^ ^  |    ", "    \\  \\_/  /    ", "     '-...-'     "];

export function SunAscii({ play }: { play: boolean }) {
  const [twinkle, setTwinkle] = useState(0);
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const timer = setInterval(() => setTwinkle((t) => 1 - t), 700);
    return () => clearInterval(timer);
  }, []);
  const rays = RAYS[twinkle];
  const disc = play ? DISC : SMILE;
  // The middle row carries the side rays; the others are the disc alone.
  const middle = rays[1].slice(0, 2) + disc[2].slice(2, 15) + rays[1].slice(17);
  return (
    <pre className="sun-ascii" aria-hidden="true">
      <span className="sun-ray">{rays[0]}</span>{"\n"}
      {disc[0]}{"\n"}{disc[1]}{"\n"}
      <span className="sun-ray">{middle.slice(0, 2)}</span>{middle.slice(2, 15)}<span className="sun-ray">{middle.slice(15)}</span>{"\n"}
      {disc[3]}{"\n"}{disc[4]}{"\n"}
      <span className="sun-ray">{rays[2]}</span>
    </pre>
  );
}

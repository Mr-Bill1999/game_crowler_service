/* Content arrives softly: blocks of the inner pages fade in and rise a little as they enter the
   view, a few at a time in reading order. One observer for the whole app — new nodes (a tab
   switch, more reviews, the next page of tiles) are picked up as React adds them. Once shown, an
   element is handed back its own styles, so hovers and transitions work as before. Off for
   people who ask for reduced motion. */

const SELECTOR = [
  ".page-head", ".catalog-filters", ".feed-tabs", ".tile",
  ".back", ".hero", ".card-page > .description", ".card-page > .block", ".card-foot",
  ".voice", ".agree", ".factor-map", ".review", ".similar-item",
].join(",");

export function startReveal() {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const seen = new WeakSet<Element>();

  const done = (el: HTMLElement) => {
    el.classList.remove("reveal", "is-in");
    el.style.removeProperty("--reveal-delay");
  };

  const io = new IntersectionObserver((entries) => {
    const shown = entries.filter((e) => e.isIntersecting).map((e) => e.target as HTMLElement);
    shown.sort((a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1));
    shown.forEach((el, i) => {
      io.unobserve(el);
      el.style.setProperty("--reveal-delay", `${Math.min(i, 8) * 60}ms`);
      requestAnimationFrame(() => el.classList.add("is-in"));
      window.setTimeout(() => done(el), 900 + Math.min(i, 8) * 60);
    });
  }, { rootMargin: "0px 0px -4% 0px", threshold: 0.04 });

  const take = (el: Element) => {
    if (seen.has(el) || el.closest(".entry")) return;
    seen.add(el);
    el.classList.add("reveal");
    io.observe(el);
  };
  const scan = (root: Element | Document) => {
    if (root instanceof Element && root.matches(SELECTOR)) take(root);
    root.querySelectorAll(SELECTOR).forEach(take);
  };

  scan(document);
  new MutationObserver((records) => {
    for (const r of records) r.addedNodes.forEach((n) => { if (n instanceof Element) scan(n); });
  }).observe(document.body, { childList: true, subtree: true });
}

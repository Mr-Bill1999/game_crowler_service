import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { useEffect } from "react";
import { Icon } from "./components/Icon";
import { DevPanel } from "./pages/DevPanel";
import { GameCard } from "./pages/GameCard";
import { GamesList } from "./pages/GamesList";
import { Welcome } from "./pages/Welcome";
import "./styles.css";

const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 30_000, retry: 1 } } });

function Shell() {
  const location = useLocation();
  const params = new URLSearchParams(location.search);
  const legacyCatalog = location.pathname === "/" && ["q", "sort", "platform", "archived", "top"].some((key) => params.has(key));
  const welcome = location.pathname === "/" && !legacyCatalog;

  useEffect(() => { window.scrollTo(0, 0); }, [location.pathname]);

  return (
    <div className={`shell ${welcome ? "welcome-shell" : ""}`}>
      {/* The entrance has its own header in the light identity. */}
      {!welcome && <header className="topbar">
        <NavLink to="/" className="brand">GameGallery</NavLink>
        <nav aria-label="Основная навигация">
          <NavLink to="/games" end className={() => (location.pathname === "/games" && !params.has("top")) || legacyCatalog || location.pathname.startsWith("/game/") ? "active" : undefined}>Каталог</NavLink>
          <NavLink to="/games?top=1" className={() => (location.pathname === "/games" && params.has("top") ? "active" : undefined)}>Топ Metacritic</NavLink>
        </nav>
        <NavLink to="/devpanel" className="topbar-side">DevPanel<Icon name="arrow-up-right" /></NavLink>
      </header>}
      <main>
        <Routes>
          <Route path="/" element={legacyCatalog ? <GamesList /> : <Welcome />} />
          <Route path="/games" element={<GamesList />} />
          <Route path="/game/:slug" element={<GameCard />} />
          <Route path="/devpanel" element={<DevPanel />} />
        </Routes>
      </main>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Shell />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);

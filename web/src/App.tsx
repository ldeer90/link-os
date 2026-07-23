import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { SystemProvider } from "./context/SystemContext";
import { AgenciesPage } from "./pages/AgenciesPage";
import { BacklinksPage } from "./pages/BacklinksPage";
import { CampaignsPage } from "./pages/CampaignsPage";
import { DomainsPage } from "./pages/DomainsPage";
import { IntakePage } from "./pages/IntakePage";
import { InventoryPage } from "./pages/InventoryPage";
import { OverviewPage } from "./pages/OverviewPage";
import { RepliesOffersPage } from "./pages/RepliesOffersPage";
import { ScrapingPage } from "./pages/ScrapingPage";
import { SettingsPage } from "./pages/SettingsPage";

export function App() {
  return (
    <SystemProvider>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<OverviewPage />} />
          <Route path="intake" element={<IntakePage />} />
          <Route path="domains" element={<DomainsPage />} />
          <Route path="scraping" element={<ScrapingPage />} />
          <Route path="backlinks" element={<BacklinksPage />} />
          <Route path="campaigns" element={<CampaignsPage />} />
          <Route path="replies" element={<RepliesOffersPage />} />
          <Route path="inventory" element={<InventoryPage />} />
          <Route path="agencies" element={<AgenciesPage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate replace to="/" />} />
        </Route>
      </Routes>
    </SystemProvider>
  );
}

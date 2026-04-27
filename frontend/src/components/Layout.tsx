import { ReactNode } from "react";
import { NavTabs } from "./NavTabs";
import { CPUIndicator } from "./CPUIndicator";

interface LayoutProps<TTab extends string> {
  activeTab: TTab;
  tabLabels: Record<TTab, string>;
  tabOrder: TTab[];
  onTabChange: (tab: TTab) => void;
  children: ReactNode;
}

export function Layout<TTab extends string>({
  activeTab,
  tabLabels,
  tabOrder,
  onTabChange,
  children
}: LayoutProps<TTab>): JSX.Element {
  // Pause Polling wenn Tab nicht sichtbar
  if (typeof document !== "undefined" && document.hidden) {
    return <div className="layout layout--hidden">{children}</div>;
  }
  return (
    <div className="layout">
      <header className="layout__header">
        <div>
          <h1 className="layout__title">-JONA$ MONEY MACHINE-</h1>
        </div>
        <CPUIndicator />
      </header>
      <NavTabs activeTab={activeTab} tabLabels={tabLabels} tabOrder={tabOrder} onChange={onTabChange} />
      <main className="layout__content">{children}</main>
      <footer className="layout__footer">
        <span>© {new Date().getFullYear()} JONA$ Money Machine</span>
        <a href="/docs" target="_blank" rel="noreferrer">
          API Docs
        </a>
      </footer>
    </div>
  );
}

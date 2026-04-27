interface NavTabsProps<TTab extends string> {
  activeTab: TTab;
  tabLabels: Record<TTab, string>;
  tabOrder: TTab[];
  onChange: (tab: TTab) => void;
}

export function NavTabs<TTab extends string>({
  activeTab,
  tabLabels,
  tabOrder,
  onChange
}: NavTabsProps<TTab>): JSX.Element {
  return (
    <nav className="nav-tabs">
      {tabOrder.map((tab) => (
        <button
          key={tab}
          type="button"
          className={`nav-tabs__item ${activeTab === tab ? "nav-tabs__item--active" : ""}`}
          onClick={() => onChange(tab)}
        >
          {tabLabels[tab]}
        </button>
      ))}
    </nav>
  );
}

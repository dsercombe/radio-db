import { useMemo, useState } from "react";
import type { StrategyBranch } from "../api/client";

interface StrategyBranchTreeProps {
  branches: StrategyBranch[];
  selectedBranch: StrategyBranch | null;
  onSelect: (branch: StrategyBranch) => void;
}

interface TreeNode {
  key: string;
  label: string;
  type: "strategy" | "variation" | "direction" | "symbol" | "branch";
  children: TreeNode[];
  branch?: StrategyBranch;
}

export function StrategyBranchTree({
  branches,
  selectedBranch,
  onSelect,
}: StrategyBranchTreeProps): JSX.Element {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const tree = useMemo(() => {
    const root: TreeNode[] = [];
    const strategyMap = new Map<string, TreeNode>();
    const variationMap = new Map<string, TreeNode>();
    const directionMap = new Map<string, TreeNode>();

    for (const branch of branches) {
      // Strategy Node
      const strategyKey = `strategy:${branch.strategy}`;
      let strategyNode = strategyMap.get(strategyKey);
      if (!strategyNode) {
        strategyNode = {
          key: strategyKey,
          label: branch.strategy,
          type: "strategy",
          children: [],
        };
        strategyMap.set(strategyKey, strategyNode);
        root.push(strategyNode);
      }

      // Variation Node
      const variationKey = `${strategyKey}:variation:${branch.variation}`;
      let variationNode = variationMap.get(variationKey);
      if (!variationNode) {
        variationNode = {
          key: variationKey,
          label: branch.variation,
          type: "variation",
          children: [],
        };
        variationMap.set(variationKey, variationNode);
        strategyNode.children.push(variationNode);
      }

      // Direction Node
      const directionKey = `${variationKey}:direction:${branch.direction}`;
      let directionNode = directionMap.get(directionKey);
      if (!directionNode) {
        directionNode = {
          key: directionKey,
          label: branch.direction,
          type: "direction",
          children: [],
        };
        directionMap.set(directionKey, directionNode);
        variationNode.children.push(directionNode);
      }

      // Symbol/Branch Node
      const branchKey = `${directionKey}:symbol:${branch.symbol}`;
      const branchNode: TreeNode = {
        key: branchKey,
        label: branch.symbol,
        type: "branch",
        children: [],
        branch,
      };
      directionNode.children.push(branchNode);
    }

    return root;
  }, [branches]);

  const toggleExpanded = (key: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  };

  const renderNode = (node: TreeNode, level: number = 0): JSX.Element => {
    const isExpanded = expanded.has(node.key);
    const hasChildren = node.children.length > 0;
    const isSelected =
      node.branch &&
      selectedBranch &&
      node.branch.strategy === selectedBranch.strategy &&
      node.branch.variation === selectedBranch.variation &&
      node.branch.direction === selectedBranch.direction &&
      node.branch.symbol === selectedBranch.symbol;

    const handleClick = () => {
      if (hasChildren) {
        toggleExpanded(node.key);
      }
      if (node.branch) {
        onSelect(node.branch);
      }
    };

    return (
      <div key={node.key}>
        <div
          style={{
            padding: "0.25rem 0.5rem",
            cursor: hasChildren || node.branch ? "pointer" : "default",
            backgroundColor: isSelected ? "var(--color-primary-light)" : "transparent",
            borderRadius: "4px",
            marginLeft: `${level * 1}rem`,
            display: "flex",
            alignItems: "center",
            gap: "0.25rem",
          }}
          onClick={handleClick}
        >
          {hasChildren && (
            <span style={{ width: "1rem", textAlign: "center" }}>
              {isExpanded ? "▼" : "▶"}
            </span>
          )}
          {!hasChildren && <span style={{ width: "1rem" }} />}
          <span
            style={{
              fontWeight: node.type === "branch" ? "bold" : "normal",
              fontSize: node.type === "branch" ? "0.9rem" : "0.85rem",
            }}
          >
            {node.label}
          </span>
        </div>
        {hasChildren && isExpanded && (
          <div>{node.children.map((child) => renderNode(child, level + 1))}</div>
        )}
      </div>
    );
  };

  return (
    <div style={{ maxHeight: "70vh", overflowY: "auto" }}>
      {tree.length === 0 ? (
        <p>Keine Zweige gefunden.</p>
      ) : (
        tree.map((node) => renderNode(node))
      )}
    </div>
  );
}


import { useEffect, useState } from "react";
import { fetchCPUStats, type CPUStats } from "../api/client";

export function CPUIndicator(): JSX.Element {
  const [cpuStats, setCpuStats] = useState<CPUStats | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    let intervalId: number | null = null;

    const loadCPUStats = async () => {
      try {
        const stats = await fetchCPUStats();
        if (mounted) {
          setCpuStats(stats);
          setError(null);
        }
      } catch (err) {
        if (mounted) {
          setError("CPU stats unavailable");
          console.error("Failed to fetch CPU stats:", err);
        }
      }
    };

    // Load immediately
    loadCPUStats();

    // Then update every 30 seconds (entlastet Backend)
    intervalId = window.setInterval(loadCPUStats, 30_000);

    return () => {
      mounted = false;
      if (intervalId !== null) {
        clearInterval(intervalId);
      }
    };
  }, []);

  const cpuPercent = cpuStats?.system?.cpu_percent ?? 0;
  const cpuCount = cpuStats?.system?.cpu_count ?? 1;

  // Determine color and emoji based on CPU usage (feminine pink/purple theme)
  let color = "#b066ff"; // Purple (low usage)
  let bgGradient = "linear-gradient(135deg, rgba(176, 102, 255, 0.25), rgba(255, 93, 200, 0.2))";
  let emoji = "💜";
  let sparkle = "✨";
  
  if (cpuPercent >= 80) {
    color = "#ff5dc8"; // Pink (high usage)
    bgGradient = "linear-gradient(135deg, rgba(255, 93, 200, 0.35), rgba(255, 42, 167, 0.3))";
    emoji = "💖";
    sparkle = "💫";
  } else if (cpuPercent >= 60) {
    color = "#ff93d9"; // Light pink (medium-high usage)
    bgGradient = "linear-gradient(135deg, rgba(255, 147, 217, 0.3), rgba(255, 93, 200, 0.25))";
    emoji = "💗";
    sparkle = "⭐";
  } else if (cpuPercent >= 40) {
    color = "#d19fff"; // Light purple (medium usage)
    bgGradient = "linear-gradient(135deg, rgba(209, 159, 255, 0.28), rgba(176, 102, 255, 0.22))";
    emoji = "💝";
    sparkle = "✨";
  }

  return (
    <div
      style={{
        position: "relative",
        zIndex: 2,
        padding: "8px 14px",
        background: "rgba(255, 255, 255, 0.2)",
        backdropFilter: "blur(10px)",
        color: "#fff",
        borderRadius: "16px",
        fontSize: "13px",
        fontFamily: '"Comic Sans MS", "Trebuchet MS", "Inter", sans-serif',
        border: `2px solid ${color}`,
        boxShadow: `
          0 2px 12px ${color}60,
          0 0 20px rgba(255, 255, 255, 0.2),
          inset 0 1px 0 rgba(255, 255, 255, 0.3)
        `,
        display: "flex",
        alignItems: "center",
        gap: "8px",
        transition: "all 0.3s cubic-bezier(0.34, 1.56, 0.64, 1)",
        cursor: "default",
      }}
      title={`💕 CPU Usage: ${cpuPercent.toFixed(1)}% across ${cpuCount} core${cpuCount !== 1 ? "s" : ""} ${sparkle}`}
      onMouseEnter={(e) => {
        e.currentTarget.style.transform = "scale(1.05)";
        e.currentTarget.style.boxShadow = `
          0 4px 18px ${color}80,
          0 0 30px rgba(255, 255, 255, 0.3),
          inset 0 1px 0 rgba(255, 255, 255, 0.4)
        `;
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.transform = "scale(1)";
        e.currentTarget.style.boxShadow = `
          0 2px 12px ${color}60,
          0 0 20px rgba(255, 255, 255, 0.2),
          inset 0 1px 0 rgba(255, 255, 255, 0.3)
        `;
      }}
    >
      <span style={{ fontSize: "16px", filter: "drop-shadow(0 2px 4px rgba(0,0,0,0.3))" }}>
        {emoji}
      </span>
      <div
        style={{
          width: "10px",
          height: "10px",
          borderRadius: "50%",
          background: `radial-gradient(circle, ${color}, ${color}dd)`,
          boxShadow: `
            0 0 8px ${color},
            0 0 16px ${color}80,
            inset 0 2px 4px rgba(255, 255, 255, 0.5)
          `,
          animation: cpuPercent > 80 ? "sparkle 1.5s ease-in-out infinite" : "none",
          position: "relative",
        }}
      >
        {cpuPercent > 80 && (
          <span
            style={{
              position: "absolute",
              top: "-6px",
              right: "-6px",
              fontSize: "9px",
              animation: "twinkle 1s ease-in-out infinite",
            }}
          >
            {sparkle}
          </span>
        )}
      </div>
      <span style={{ fontWeight: 600, textShadow: "0 1px 3px rgba(0,0,0,0.4)" }}>
        CPU: <strong style={{ color, fontWeight: 700, fontSize: "14px" }}>{cpuPercent.toFixed(1)}%</strong>
      </span>
      <span style={{ opacity: 0.85, fontSize: "11px", fontWeight: 500, textShadow: "0 1px 2px rgba(0,0,0,0.3)" }}>
        {cpuCount} core{cpuCount !== 1 ? "s" : ""}
      </span>
      <style>{`
        @keyframes sparkle {
          0%, 100% { 
            transform: scale(1);
            box-shadow: 0 0 10px ${color}, 0 0 20px ${color}80, inset 0 2px 4px rgba(255, 255, 255, 0.5);
          }
          50% { 
            transform: scale(1.2);
            box-shadow: 0 0 15px ${color}, 0 0 30px ${color}aa, inset 0 2px 4px rgba(255, 255, 255, 0.7);
          }
        }
        @keyframes twinkle {
          0%, 100% { 
            opacity: 1;
            transform: scale(1) rotate(0deg);
          }
          50% { 
            opacity: 0.5;
            transform: scale(1.3) rotate(180deg);
          }
        }
      `}</style>
    </div>
  );
}

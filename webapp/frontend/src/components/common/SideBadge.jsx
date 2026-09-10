export default function SideBadge({ side }) {
  return <span className={`side-badge ${side === "LONG" ? "side-long" : "side-short"}`}>{side}</span>;
}

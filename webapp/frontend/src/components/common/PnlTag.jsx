import { ArrowUpRight, ArrowDownRight } from "lucide-react";
import { fmtPct } from "../../lib/format";

export default function PnlTag({ value, pct }) {
  if (pct === null || pct === undefined) return null;
  const up = value >= 0;
  return (
    <div className={`pnl-tag ${up ? "pnl-up" : "pnl-down"}`}>
      {up ? <ArrowUpRight size={12} strokeWidth={2.5} /> : <ArrowDownRight size={12} strokeWidth={2.5} />}
      <span>{fmtPct(pct)}</span>
    </div>
  );
}

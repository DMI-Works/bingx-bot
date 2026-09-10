import { ChevronRight } from "lucide-react";

export default function LinkRow({ icon: Icon, title, value }) {
  return (
    <div className="row">
      <div className="row-icon">
        <Icon size={17} />
      </div>
      <div className="row-main">
        <span className="settings-title">{title}</span>
      </div>
      {value && <span className="row-value">{value}</span>}
      <ChevronRight size={16} className="chev" />
    </div>
  );
}

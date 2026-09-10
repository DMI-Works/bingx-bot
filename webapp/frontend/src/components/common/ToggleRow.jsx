import { useState } from "react";

export default function ToggleRow({ icon: Icon, title, sub, checked, defaultOn = false, disabled, onToggle }) {
  // Управляемый режим: checked + onToggle заданы извне (например, кнопка "Торговля включена").
  // Неуправляемый режим: используется только defaultOn, состояние живёт внутри самого ряда
  // (например, локальные переключатели уведомлений).
  const isControlled = checked !== undefined && typeof onToggle === "function";
  const [internalOn, setInternalOn] = useState(defaultOn);
  const on = isControlled ? checked : internalOn;

  const handleToggle = () => {
    if (disabled) return;
    const next = !on;
    if (isControlled) {
      onToggle(next);
    } else {
      setInternalOn(next);
    }
  };

  return (
    <div className="row">
      {Icon && (
        <div className="row-icon">
          <Icon size={17} />
        </div>
      )}
      <div className="row-main">
        <span className="settings-title">{title}</span>
        {sub && <div className="row-sub">{sub}</div>}
      </div>
      <div
        className={`switch ${on ? "switch-on" : ""}`}
        role="switch"
        aria-checked={on}
        aria-disabled={disabled || undefined}
        onClick={handleToggle}
        style={disabled ? { opacity: 0.5, pointerEvents: "none" } : undefined}
      >
        <div className="switch-knob" />
      </div>
    </div>
  );
}

export const fmtUsd = (n) =>
  `${n >= 0 ? "+" : "−"}$${Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export const fmtUsdPlain = (n) =>
  `$${(n ?? 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export const fmtPct = (n) => `${n >= 0 ? "+" : "−"}${Math.abs(n).toFixed(2)}%`;

function toLocalDate(iso) {
  return new Date(iso.includes("Z") || iso.includes("+") ? iso : `${iso}Z`);
}

export const fmtDate = (iso) => {
  if (!iso) return "—";
  const d = toLocalDate(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
};

// Только время — используется внутри группы "по дням", где дата уже
// показана один раз в заголовке-разделителе.
export const fmtTime = (iso) => {
  if (!iso) return "—";
  const d = toLocalDate(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
};

export function initialsOf(name) {
  if (!name) return "??";
  return name
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase())
    .join("") || "??";
}

// Стабильный ключ календарного дня в ЛОКАЛЬНОЙ таймзоне пользователя — не
// UTC, иначе сделки поздно вечером "перетекают" не в тот день на экране.
export function dayKey(iso) {
  if (!iso) return "unknown";
  const d = toLocalDate(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleDateString("sv-SE"); // YYYY-MM-DD
}

// Заголовок-разделитель для группы сделок за день: "Сегодня" / "Вчера" /
// "10 сентября" (год добавляется, только если сделка не за текущий год).
export function fmtDayLabel(iso) {
  if (!iso) return "—";
  const d = toLocalDate(iso);
  if (Number.isNaN(d.getTime())) return String(iso);

  const now = new Date();
  const startOfDay = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
  const diffDays = Math.round((startOfDay(now) - startOfDay(d)) / 86400000);

  if (diffDays === 0) return "Сегодня";
  if (diffDays === 1) return "Вчера";

  return d.toLocaleDateString("ru-RU", {
    day: "2-digit",
    month: "long",
    year: d.getFullYear() !== now.getFullYear() ? "numeric" : undefined,
  });
}

// Группирует уже отсортированный по убыванию closed_at список сделок в
// последовательные блоки по календарному дню (сегодня выше, дальше в
// прошлое) — ключ, заголовок и сумма net PnL за день.
export function groupTradesByDay(trades) {
  const groups = [];
  let current = null;
  for (const t of trades) {
    const key = dayKey(t.closed_at);
    if (!current || current.key !== key) {
      current = { key, label: fmtDayLabel(t.closed_at), items: [], pnl: 0 };
      groups.push(current);
    }
    current.items.push(t);
    current.pnl += t.net_pnl ?? 0;
  }
  return groups;
}

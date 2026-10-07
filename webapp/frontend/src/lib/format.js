export const fmtUsd = (n) =>
  `${n >= 0 ? "+" : "−"}$${Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export const fmtUsdPlain = (n) =>
  `$${(n ?? 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export const fmtPct = (n) => `${n >= 0 ? "+" : "−"}${Math.abs(n).toFixed(2)}%`;

// SL в % ROI (как на бирже и как считает TrailingStopManager):
// сдвиг цены относительно входа × плечо, со знаком «в сторону позиции».
//   < 0 — стоп ещё в зоне убытка (начальный SL, напр. −20%)
//   > 0 — стоп уже переставлен в прибыль трейлингом
// Возвращает null, если данных не хватает.
export const slRoiPercent = ({ entry_price, stop_loss_price, side, leverage }) => {
  const entry = Number(entry_price);
  const sl = Number(stop_loss_price);
  const lev = Number(leverage);
  if (!entry || !sl || !lev) return null;
  const priceMove = (sl - entry) / entry;
  return priceMove * (side === "LONG" ? 1 : -1) * lev * 100;
};

function toLocalDate(iso) {
  return new Date(iso.includes("Z") || iso.includes("+") ? iso : `${iso}Z`);
}

export const fmtDate = (iso) => {
  if (!iso) return "—";
  const d = toLocalDate(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
};

// Длительность сделки (duration_seconds из trade_analytics) в коротком виде:
// "2ч 15м", "45м", "<1м".
export const fmtDuration = (seconds) => {
  if (seconds == null) return null;
  const totalMin = Math.round(seconds / 60);
  if (totalMin < 1) return "<1м";
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return h > 0 ? `${h}ч ${m}м` : `${m}м`;
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

// Суммарная комиссия одной сделки (открытие + закрытие). commission_usdt —
// уже готовая сумма с бэкенда, но считаем и сами на случай, если пришли
// только commission_open/close без него (напр. более старый бэкенд) —
// тогда хотя бы частичная комиссия всё равно не потеряется молча.
export function tradeCommission(t) {
  if (t.commission_usdt != null) return t.commission_usdt;
  if (t.commission_open == null && t.commission_close == null) return null;
  return (t.commission_open ?? 0) + (t.commission_close ?? 0);
}

// Группирует уже отсортированный по убыванию closed_at список сделок в
// последовательные блоки по календарному дню (сегодня выше, дальше в
// прошлое) — ключ, заголовок, сумма net PnL и сумма комиссии за день
// (комиссия — отдельно от pnl, net_pnl её уже учитывает внутри, это для
// наглядности "сколько чистыми, а сколько конкретно съела биржа").
export function groupTradesByDay(trades) {
  const groups = [];
  let current = null;
  for (const t of trades) {
    const key = dayKey(t.closed_at);
    if (!current || current.key !== key) {
      current = { key, label: fmtDayLabel(t.closed_at), items: [], pnl: 0, commission: 0 };
      groups.push(current);
    }
    current.items.push(t);
    current.pnl += t.net_pnl ?? 0;
    current.commission += tradeCommission(t) ?? 0;
  }
  return groups;
}
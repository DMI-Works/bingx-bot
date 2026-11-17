import { locale, t } from "../i18n";

export const fmtUsd = (n) =>
  `${n >= 0 ? "+" : "−"}$${Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export const fmtUsdPlain = (n) =>
  `$${(n ?? 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export const fmtPct = (n) => `${n >= 0 ? "+" : "−"}${Math.abs(n).toFixed(2)}%`;

// Profit factor: число, «∞» (убытков не было, но прибыль есть) или «—» (нет данных).
export const fmtProfitFactor = (pf, hasProfit) => (pf != null ? pf.toFixed(2) : hasProfit ? "∞" : "—");

// Объём за 24ч в коротком виде: 1.23B / 45.6M / 789K.
export function fmtVolume(v) {
  if (v >= 1e9) return (v / 1e9).toFixed(2) + "B";
  if (v >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (v >= 1e3) return (v / 1e3).toFixed(0) + "K";
  return String(v);
}

export function fmtPrice(p) {
  if (p == null) return "—";
  return p >= 1 ? p.toLocaleString(locale(), { maximumFractionDigits: 2 }) : p.toPrecision(3);
}

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
  return d.toLocaleString(locale(), { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
};

// Длительность сделки (duration_seconds из trade_analytics) в коротком виде:
// "2h 15m" / "2ч 15м", "45m", "<1m".
export const fmtDuration = (seconds) => {
  if (seconds == null) return null;
  const totalMin = Math.round(seconds / 60);
  if (totalMin < 1) return t("dur_lt_min");
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return h > 0 ? t("dur_hm", { h, m }) : t("dur_m", { m });
};

// Только время — используется внутри группы "по дням", где дата уже
// показана один раз в заголовке-разделителе.
export const fmtTime = (iso) => {
  if (!iso) return "—";
  const d = toLocalDate(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit" });
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

// Заголовок-разделитель для группы сделок за день: "Today" / "Yesterday" /
// "10 September" (год добавляется, только если сделка не за текущий год).
export function fmtDayLabel(iso) {
  if (!iso) return "—";
  const d = toLocalDate(iso);
  if (Number.isNaN(d.getTime())) return String(iso);

  const now = new Date();
  const startOfDay = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
  const diffDays = Math.round((startOfDay(now) - startOfDay(d)) / 86400000);

  if (diffDays === 0) return t("today");
  if (diffDays === 1) return t("yesterday");

  return d.toLocaleDateString(locale(), {
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
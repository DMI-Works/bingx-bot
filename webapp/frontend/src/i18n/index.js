// ---------------------------------------------------------------------------
// Мультиязычность Mini App: en (по умолчанию), ru, uk.
// Язык хранится на бэкенде (users.lang, POST /api/me/lang); пока не выбран —
// показываем экран выбора, а тексты — по-английски. t() читает текущий язык
// из модуля, поэтому работает и вне компонентов (format.js); App
// перемонтирует дерево при смене языка (key={lang}).
// ---------------------------------------------------------------------------
import en from "./en";
import ru from "./ru";
import uk from "./uk";

export const LANGS = [
  { code: "en", flag: "🇬🇧", name: "English" },
  { code: "ru", flag: "🇷🇺", name: "Русский" },
  { code: "uk", flag: "🇺🇦", name: "Українська" },
];

const DICTS = { en, ru, uk };
const LOCALES = { en: "en-GB", ru: "ru-RU", uk: "uk-UA" };

let current = "en";

export function setLang(code) {
  current = DICTS[code] ? code : "en";
  if (typeof document !== "undefined") document.documentElement.lang = current;
}

export const getLang = () => current;

// Локаль для toLocaleString / toLocaleDateString.
export const locale = () => LOCALES[current];

export function t(key, vars) {
  let text = DICTS[current][key] ?? DICTS.en[key] ?? key;
  if (vars) {
    text = text.replace(/\{(\w+)\}/g, (m, name) => (vars[name] !== undefined ? String(vars[name]) : m));
  }
  return text;
}

// Множественное число по правилам языка: ключи `${key}_one|_few|_many|_other`,
// например tn("trades_count", 5) → «5 сделок» / «5 trades».
export function tn(key, n, vars) {
  const rule = new Intl.PluralRules(LOCALES[current]).select(n);
  const dict = DICTS[current];
  const k = dict[`${key}_${rule}`] !== undefined ? `${key}_${rule}` : `${key}_other`;
  return t(k, { n, ...vars });
}

// Язык Telegram-клиента — только чтобы подсветить вариант на экране выбора.
export function detectTelegramLang() {
  const code = (window.Telegram?.WebApp?.initDataUnsafe?.user?.language_code || "").slice(0, 2).toLowerCase();
  if (code === "ua") return "uk";
  return DICTS[code] ? code : "en";
}

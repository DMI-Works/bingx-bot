import { Bot, ShieldCheck, Clock, Bell, BookOpen, Wallet, Repeat, KeyRound, Link2 } from "lucide-react";
import { openGuide } from "./guideEvents";

const FEATURES = [
  { icon: Clock, title: "24/7", text: "Торгует сам, круглосуточно" },
  { icon: ShieldCheck, title: "Стоп всегда", text: "Стоп-лосс стоит на бирже" },
  { icon: Bell, title: "В Telegram", text: "Сообщает о каждой сделке" },
];

const STEPS = [
  { icon: Wallet, title: "Деньги на фьючерсном счёте", text: "USDT для Live или демо-VST для Testnet", section: "funds" },
  { icon: Repeat, title: "Хедж-режим на BingX", text: "Двусторонние позиции в настройках фьючерсов", section: "hedge" },
  { icon: KeyRound, title: "API-ключ", text: "Чтение + торговля фьючерсами, без вывода", section: "keys" },
  { icon: Link2, title: "Подключение здесь", text: "Вставьте ключ ниже — и бот запустится", section: "connect" },
];

export default function Welcome() {
  return (
    <div className="welcome">
      <div className="welcome-hero">
        <div className="welcome-glow" />
        <div className="guide-hero-icon"><Bot size={30} /></div>
        <h1 className="welcome-title">Velora — торговый бот для BingX</h1>
        <p className="welcome-text">
          Автоматическая торговля бессрочными фьючерсами. Деньги остаются на вашем счёте —
          бот только открывает и закрывает сделки.
        </p>
        <div className="welcome-features">
          {FEATURES.map(({ icon: Icon, title, text }) => (
            <div className="welcome-feature" key={title}>
              <Icon size={18} className="welcome-feature-icon" />
              <div className="welcome-feature-title">{title}</div>
              <div className="welcome-feature-text">{text}</div>
            </div>
          ))}
        </div>
      </div>

      <div className="section">
        <div className="section-head">
          <span className="section-title">4 шага до запуска</span>
          <span className="section-count">~10 мин</span>
        </div>
        <div className="welcome-steps">
          {STEPS.map(({ icon: Icon, title, text, section }, i) => (
            <button className="welcome-step" key={title} onClick={() => openGuide(section)}>
              <div className="guide-step-badge">{i + 1}</div>
              <div className="welcome-step-main">
                <div className="welcome-step-title"><Icon size={14} /> {title}</div>
                <div className="welcome-step-text">{text}</div>
              </div>
            </button>
          ))}
        </div>
        <button className="primary-btn" onClick={() => openGuide()}>
          <BookOpen size={16} /> Подробная инструкция и ответы на вопросы
        </button>
      </div>
    </div>
  );
}

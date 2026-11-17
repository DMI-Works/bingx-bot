import { Bot, ShieldCheck, Clock, Bell, BookOpen, Wallet, Repeat, KeyRound, Link2 } from "lucide-react";
import { openGuide } from "./guideEvents";
import { t } from "../../i18n";

// title/text — ключи перевода
const FEATURES = [
  { icon: Clock, title: null, text: "feat_247_text" },
  { icon: ShieldCheck, title: "feat_stop_title", text: "feat_stop_text" },
  { icon: Bell, title: "feat_tg_title", text: "feat_tg_text" },
];

const STEPS = [
  { icon: Wallet, key: "funds" },
  { icon: Repeat, key: "hedge" },
  { icon: KeyRound, key: "keys" },
  { icon: Link2, key: "connect" },
];

export default function Welcome() {
  return (
    <div className="welcome">
      <div className="welcome-hero">
        <div className="welcome-glow" />
        <div className="guide-hero-icon"><Bot size={30} /></div>
        <h1 className="welcome-title">{t("welcome_title")}</h1>
        <p className="welcome-text">{t("welcome_text")}</p>
        <div className="welcome-features">
          {FEATURES.map(({ icon: Icon, title, text }) => (
            <div className="welcome-feature" key={text}>
              <Icon size={18} className="welcome-feature-icon" />
              <div className="welcome-feature-title">{title ? t(title) : "24/7"}</div>
              <div className="welcome-feature-text">{t(text)}</div>
            </div>
          ))}
        </div>
      </div>

      <div className="section">
        <div className="section-head">
          <span className="section-title">{t("steps_title")}</span>
          <span className="section-count">{t("steps_time")}</span>
        </div>
        <div className="welcome-steps">
          {STEPS.map(({ icon: Icon, key }, i) => (
            <button className="welcome-step" key={key} onClick={() => openGuide(key)}>
              <div className="guide-step-badge">{i + 1}</div>
              <div className="welcome-step-main">
                <div className="welcome-step-title"><Icon size={14} /> {t(`step_${key}_title`)}</div>
                <div className="welcome-step-text">{t(`step_${key}_text`)}</div>
              </div>
            </button>
          ))}
        </div>
        <button className="primary-btn" onClick={() => openGuide()}>
          <BookOpen size={16} /> {t("guide_button")}
        </button>
      </div>
    </div>
  );
}

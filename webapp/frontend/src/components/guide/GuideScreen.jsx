import { useEffect, useRef, useState } from "react";
import { X, Bot } from "lucide-react";
import { apiGet } from "../../lib/api";
import { getLang, t } from "../../i18n";
import GuideBodyEn from "./content/en";
import GuideBodyRu from "./content/ru";
import GuideBodyUk from "./content/uk";

// Текст инструкции — отдельный компонент на каждый язык (много разметки
// внутри абзацев); id разделов у всех одинаковые, по ним работает оглавление.
const BODIES = { en: GuideBodyEn, ru: GuideBodyRu, uk: GuideBodyUk };

const TOC = ["how", "account", "funds", "hedge", "keys", "connect", "settings", "security", "billing", "emergency", "faq"];

export default function GuideScreen({ section, onClose }) {
  const bodyRef = useRef(null);
  const [billing, setBilling] = useState(null);

  useEffect(() => {
    apiGet("/billing").then(setBilling).catch(() => { /* умови покажемо загальними словами */ });
  }, []);

  const scrollTo = (id) => {
    const el = bodyRef.current?.querySelector(`#${id}`);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  useEffect(() => { if (section) setTimeout(() => scrollTo(section), 50); }, [section]);

  const fee = billing?.fee_percent ?? 10;
  const Body = BODIES[getLang()] ?? GuideBodyEn;

  return (
    <div className="guide-overlay">
      <div className="guide-topbar">
        <span className="guide-topbar-title">{t("guide_title")}</span>
        <button className="icon-btn" onClick={onClose} title={t("close")}><X size={18} /></button>
      </div>

      <div className="guide-body" ref={bodyRef}>
        <div className="guide-hero">
          <div className="guide-hero-icon"><Bot size={28} /></div>
          <h1 className="guide-hero-title">{t("guide_hero_title")}</h1>
          <p className="guide-hero-text">{t("guide_hero_text")}</p>
        </div>

        <div className="guide-toc">
          {TOC.map((id) => (
            <button key={id} className="guide-toc-item" onClick={() => scrollTo(id)}>{t(`toc_${id}`)}</button>
          ))}
        </div>

        <Body billing={billing} fee={fee} />

        <div className="guide-footer">{t("guide_footer")}</div>
      </div>
    </div>
  );
}

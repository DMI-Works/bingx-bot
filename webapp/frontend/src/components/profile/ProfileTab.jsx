import { useState, useEffect } from "react";
import { LogOut } from "lucide-react";
import { apiGet, tg } from "../../lib/api";
import { fmtUsd, fmtUsdPlain, initialsOf } from "../../lib/format";
import { Spinner } from "../common";

export default function ProfileTab() {
  const [profile, setProfile] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    apiGet("/profile")
      .then((data) => !cancelled && setProfile(data))
      .catch((e) => !cancelled && setError(e.message));
    return () => { cancelled = true; };
  }, []);

  // Данные о самом пользователе даёт сам Telegram (initData) — не то, что
  // отдаёт наш бэкенд про биржевой аккаунт. Вне Telegram (например, при
  // локальной разработке в обычном браузере) tg будет null — тогда просто
  // показываем заглушку вместо падения.
  const tgUser = tg?.initDataUnsafe?.user;
  const displayName = tgUser
    ? [tgUser.first_name, tgUser.last_name].filter(Boolean).join(" ")
    : "Профиль недоступен";
  const handle = tgUser?.username ? `@${tgUser.username}` : "—";

  return (
    <div className="tab-pane">
      <div className="profile-head">
        {tgUser?.photo_url
          ? <img className="avatar avatar-photo" src={tgUser.photo_url} alt="" />
          : <div className="avatar">{initialsOf(displayName)}</div>}
        <div>
          <div className="profile-name">{displayName}</div>
          <div className="profile-handle">{handle}</div>
        </div>
      </div>

      {error && <div className="error-banner">Не удалось загрузить данные: {error}</div>}

      <div className="section">
        <div className="section-head">
          <span className="section-title">Подключение к бирже</span>
        </div>
        <div className="list">
          <div className="row">
            <div className="row-main">
              <div className="row-title-line">
                <span className="symbol">BingX</span>
                {profile && (
                  <span className={`mode-badge ${profile.testnet ? "mode-testnet" : "mode-live"}`}>
                    {profile.testnet ? "Testnet" : "Live"}
                  </span>
                )}
              </div>
              <div className="row-sub">
                {profile?.api_key_suffix
                  ? `API-ключ подключён · •••• ${profile.api_key_suffix}`
                  : "API-ключ подключён"}
              </div>
            </div>
          </div>
        </div>
      </div>

      <div className="section">
        <div className="section-head">
          <span className="section-title">Баланс аккаунта</span>
        </div>
        {!profile && !error && (
          <div className="hero-loading"><Spinner /></div>
        )}
        {profile && (
          <div className="list">
            <div className="kv-row">
              <span className="kv-label">Доступно</span>
              <span className="kv-value">{fmtUsdPlain(profile.available)}</span>
            </div>
            <div className="kv-row">
              <span className="kv-label">В позициях</span>
              <span className="kv-value">{fmtUsdPlain(profile.used_margin)}</span>
            </div>
            {profile.unrealized_pnl !== 0 && (
              <div className="kv-row">
                <span className="kv-label">Нереализ. PnL</span>
                <span className={`kv-value ${profile.unrealized_pnl >= 0 ? "text-profit" : "text-loss"}`}>
                  {fmtUsd(profile.unrealized_pnl)}
                </span>
              </div>
            )}
            <div className="kv-row">
              <span className="kv-label">Всего</span>
              <span className="kv-value kv-value-strong">{fmtUsdPlain(profile.equity)}</span>
            </div>
          </div>
        )}
      </div>

      <button className="danger-btn" onClick={() => tg?.close()} disabled={!tg}>
        <LogOut size={16} />
        Закрыть приложение
      </button>
    </div>
  );
}

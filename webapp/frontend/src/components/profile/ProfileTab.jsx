import { useState, useEffect } from "react";
import { LogOut, Repeat } from "lucide-react";
import { apiGet, apiPost, tg } from "../../lib/api";
import { fmtUsd, fmtUsdPlain, initialsOf } from "../../lib/format";
import { Spinner } from "../common";

// Подтверждение: в Telegram — нативное окно, вне его (локальная разработка) — window.confirm.
function confirmAsync(message) {
  if (tg?.showConfirm) {
    return new Promise((resolve) => tg.showConfirm(message, resolve));
  }
  return Promise.resolve(window.confirm(message));
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export default function ProfileTab() {
  const [profile, setProfile] = useState(null);
  const [error, setError] = useState(null);
  // Состояние смены режима: idle | switching; switchMsg — что показать пользователю.
  const [switching, setSwitching] = useState(false);
  const [switchMsg, setSwitchMsg] = useState(null);

  useEffect(() => {
    let cancelled = false;
    apiGet("/profile")
      .then((data) => !cancelled && setProfile(data))
      .catch((e) => !cancelled && setError(e.message));
    return () => { cancelled = true; };
  }, []);

  // Отправляет смену режима. force=true — после подтверждения при открытых позициях.
  async function postMode(target, force) {
    const res = await apiPost("/settings/mode", { testnet: target, force });
    if (!res.restarting) return; // уже в этом режиме
    // Бот штатно перезапускается в этом же процессе (~10-20с): /api/* на это
    // время могут отвечать 503. Опрашиваем /profile, пока режим не сменится.
    setSwitchMsg(`Перезапуск бота в режим ${target ? "Testnet" : "Live"}…`);
    for (let i = 0; i < 40; i++) {
      await sleep(1500);
      try {
        const p = await apiGet("/profile");
        if (p.testnet === target) {
          setProfile(p);
          setSwitchMsg(null);
          setSwitching(false);
          return;
        }
      } catch { /* бэкенд ещё перезапускается — продолжаем опрос */ }
    }
    // Не дождались — перезагрузим страницу, пусть подтянет актуальное состояние.
    window.location.reload();
  }

  async function handleSwitchMode() {
    if (!profile || switching) return;
    const target = !profile.testnet; // куда переключаемся
    const warn = target
      ? "Переключить в TESTNET? Бот перезапустится и начнёт торговать на тестовой бирже (виртуальные средства)."
      : "⚠️ Переключить в LIVE (РЕАЛЬНЫЕ ДЕНЬГИ)? Бот перезапустится и начнёт торговать на реальном счёте.";
    if (!(await confirmAsync(warn))) return;

    setSwitching(true);
    setError(null);
    setSwitchMsg("Переключение режима…");
    try {
      await postMode(target, false);
    } catch (e) {
      if (e.status === 409) {
        const n = e.detail?.open_positions ?? "?";
        const ok = await confirmAsync(
          `Есть ${n} открытых позиций. Они останутся в текущей базе без сопровождения SL/TP до возврата в этот режим. Переключить принудительно?`
        );
        if (ok) {
          try {
            await postMode(target, true);
            return;
          } catch (e2) {
            setError(e2.message);
          }
        }
      } else {
        setError(e.message);
      }
      setSwitchMsg(null);
      setSwitching(false);
    }
  }

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
        {profile && (
          <>
            <button
              className="secondary-btn"
              onClick={handleSwitchMode}
              disabled={switching}
            >
              {switching ? <Spinner /> : <Repeat size={16} />}
              {switching
                ? "Переключение…"
                : `Переключить в ${profile.testnet ? "Live" : "Testnet"}`}
            </button>
            {switchMsg && <div className="row-sub" style={{ marginTop: 8 }}>{switchMsg}</div>}
          </>
        )}
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

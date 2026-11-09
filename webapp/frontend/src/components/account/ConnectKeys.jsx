import { useState } from "react";
import { KeyRound } from "lucide-react";
import { apiPost } from "../../lib/api";
import { Spinner } from "../common";

// Подключение API-ключа BingX. Без пропа testnet пользователь сам выбирает
// режим (первый вход); с пропом — ключ для указанного режима (Профиль).
export default function ConnectKeys({ testnet: fixedTestnet, onDone }) {
  const [testnet, setTestnet] = useState(fixedTestnet ?? true);
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const canChooseMode = fixedTestnet === undefined;

  const submit = async () => {
    if (!apiKey.trim() || !apiSecret.trim()) { setErr("Заполните оба поля"); return; }
    setBusy(true);
    setErr(null);
    try {
      onDone(await apiPost("/account/keys", { testnet, api_key: apiKey, api_secret: apiSecret }));
    } catch (e) {
      setErr(e.message);
      setBusy(false);
    }
  };

  return (
    <div className="section">
      <div className="section-head">
        <span className="section-title">
          Подключение BingX{canChooseMode ? "" : ` · ${testnet ? "Testnet" : "Live"}`}
        </span>
      </div>
      <div className="list">
        <div className="row">
          <div className="row-main">
            <div className="row-sub">
              Создайте API-ключ на BingX с правами на чтение и торговлю фьючерсами и вставьте его сюда.
              Ключ проверяется на бирже и хранится в зашифрованном виде.
            </div>
            {canChooseMode && (
              <div className="param-edit-line">
                <button className="param-save-btn" disabled={testnet} onClick={() => setTestnet(true)}>Testnet</button>
                <button className="param-save-btn" disabled={!testnet} onClick={() => setTestnet(false)}>Live</button>
              </div>
            )}
            <div className="param-edit-line">
              <input
                className="param-input"
                placeholder="API Key"
                autoComplete="off"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
              />
            </div>
            <div className="param-edit-line">
              <input
                className="param-input"
                type="password"
                placeholder="Secret Key"
                autoComplete="off"
                value={apiSecret}
                onChange={(e) => setApiSecret(e.target.value)}
              />
            </div>
            {err && <div className="row-sub" style={{ color: "var(--loss)" }}>{err}</div>}
          </div>
        </div>
      </div>
      <button className="secondary-btn" onClick={submit} disabled={busy}>
        {busy ? <Spinner /> : <KeyRound size={16} />}
        {busy ? "Проверяем ключ…" : `Подключить${testnet ? " Testnet" : " Live (реальные деньги)"}`}
      </button>
    </div>
  );
}

import { useEffect, useRef, useState } from "react";
import {
  X, Bot, UserPlus, Wallet, Repeat, KeyRound, Link2, SlidersHorizontal, ShieldCheck,
  Receipt, Siren, HelpCircle, ChevronDown, AlertTriangle, CheckCircle2, Info, LineChart,
} from "lucide-react";
import { apiGet } from "../../lib/api";

function Callout({ kind = "info", children }) {
  const Icon = kind === "warn" ? AlertTriangle : kind === "ok" ? CheckCircle2 : Info;
  return (
    <div className={`guide-callout guide-callout-${kind}`}>
      <Icon size={16} className="guide-callout-icon" />
      <div>{children}</div>
    </div>
  );
}

function Step({ id, n, icon: Icon, title, children }) {
  return (
    <section className="guide-card" id={id}>
      <div className="guide-card-head">
        <div className="guide-step-badge">{n ?? <Icon size={18} />}</div>
        <div className="guide-card-title">
          {n != null && <Icon size={16} className="guide-card-title-icon" />}
          {title}
        </div>
      </div>
      <div className="guide-card-body">{children}</div>
    </section>
  );
}

function Path({ items }) {
  return (
    <div className="guide-path">
      {items.map((x, i) => (
        <span key={x}>
          <span className="guide-path-item">{x}</span>
          {i < items.length - 1 && <span className="guide-path-sep">›</span>}
        </span>
      ))}
    </div>
  );
}

function Faq({ q, children }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={`guide-faq ${open ? "guide-faq-open" : ""}`}>
      <button className="guide-faq-q" onClick={() => setOpen(!open)}>
        <span>{q}</span>
        <ChevronDown size={16} className="guide-faq-chev" />
      </button>
      {open && <div className="guide-faq-a">{children}</div>}
    </div>
  );
}

const TOC = [
  ["how", "Как это работает"],
  ["account", "Аккаунт BingX"],
  ["funds", "Деньги на счёте"],
  ["hedge", "Хедж-режим"],
  ["keys", "API-ключ"],
  ["connect", "Подключение"],
  ["settings", "Настройки риска"],
  ["security", "Безопасность"],
  ["billing", "Оплата"],
  ["emergency", "Аварийная остановка"],
  ["faq", "Вопросы и ответы"],
];

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

  return (
    <div className="guide-overlay">
      <div className="guide-topbar">
        <span className="guide-topbar-title">Инструкция</span>
        <button className="icon-btn" onClick={onClose} title="Закрыть"><X size={18} /></button>
      </div>

      <div className="guide-body" ref={bodyRef}>
        <div className="guide-hero">
          <div className="guide-hero-icon"><Bot size={28} /></div>
          <h1 className="guide-hero-title">Как запустить бота</h1>
          <p className="guide-hero-text">
            Около 10 минут: аккаунт BingX, деньги на фьючерсном счёте, API-ключ — и бот начинает торговать сам.
          </p>
        </div>

        <div className="guide-toc">
          {TOC.map(([id, label]) => (
            <button key={id} className="guide-toc-item" onClick={() => scrollTo(id)}>{label}</button>
          ))}
        </div>

        <Step id="how" icon={LineChart} title="Как это работает">
          <p>
            Бот круглосуточно следит за рынком и сам открывает и закрывает сделки на <b>бессрочных фьючерсах BingX</b>
            по стратегиям, которые настраивает администратор. Деньги всегда остаются на <b>вашем</b> счёте BingX —
            бот только отправляет туда ордера через API-ключ.
          </p>
          <ul className="guide-list">
            <li>У каждой сделки сразу ставится стоп-лосс на бирже — он сработает, даже если бот недоступен.</li>
            <li>Когда сделка идёт в плюс, стоп подтягивается: сначала в безубыток, потом за ценой.</li>
            <li>Об открытии, закрытии и переносе стопа бот пишет вам в Telegram.</li>
            <li>Все сделки и результат — на вкладке «Статистика», детали — в «Аналитике».</li>
          </ul>
          <Callout kind="warn">
            Торговля фьючерсами с плечом рискованна: бывают серии убыточных сделок. Подключайте только те деньги,
            которые готовы рисковать. Начните с <b>Testnet</b> — это демо-счёт с виртуальными деньгами.
          </Callout>
        </Step>

        <Step id="account" n={1} icon={UserPlus} title="Аккаунт BingX">
          <p>Если аккаунта ещё нет — зарегистрируйтесь на <b>bingx.com</b> или в приложении BingX.</p>
          <ul className="guide-list">
            <li>Пройдите верификацию личности (KYC) — без неё биржа может ограничить создание API-ключей и торговлю.</li>
            <li>Включите двухфакторную защиту (Google Authenticator) — она понадобится при создании ключа.</li>
          </ul>
        </Step>

        <Step id="funds" n={2} icon={Wallet} title="Деньги на фьючерсном счёте">
          <p>
            Бот торгует только с <b>фьючерсного счёта USDT (бессрочные фьючерсы)</b>. Деньги на спотовом счёте он не видит.
          </p>
          <div className="guide-mode-grid">
            <div className="guide-mode guide-mode-test">
              <div className="guide-mode-title">Testnet</div>
              <div className="guide-mode-text">
                Демо-счёт BingX с виртуальными <b>VST</b>. Включите демо-торговлю в разделе фьючерсов —
                виртуальный баланс выдаёт сама биржа. Ничем не рискуете.
              </div>
            </div>
            <div className="guide-mode guide-mode-live">
              <div className="guide-mode-title">Live</div>
              <div className="guide-mode-text">
                Реальные <b>USDT</b>. Переведите их на фьючерсный счёт:
                <Path items={["Активы", "Перевод", "Спот → Бессрочные фьючерсы"]} />
              </div>
            </div>
          </div>
          <Callout>
            Сколько нужно: каждая сделка берёт маржу от минимальной до максимальной (по умолчанию 10–100 USDT),
            одновременно открыто до 3 сделок. Ориентир — от 50 USDT, комфортнее от 100–200 USDT.
          </Callout>
        </Step>

        <Step id="hedge" n={3} icon={Repeat} title="Включите хедж-режим">
          <p>
            Бот открывает позиции с явной стороной (LONG / SHORT), поэтому на бирже должен стоять
            <b> хедж-режим</b> (двусторонние позиции). В одностороннем режиме биржа отклонит ордера бота.
          </p>
          <Path items={["Фьючерсы", "Настройки ⚙", "Режим позиции", "Хедж"]} />
          <Callout>Режим нельзя сменить, пока есть открытые позиции или ордера — переключите его до подключения бота. Если начинаете с Testnet, проверьте режим и на демо-счёте.</Callout>
        </Step>

        <Step id="keys" n={4} icon={KeyRound} title="Создайте API-ключ">
          <Path items={["Профиль", "Управление API", "Создать API"]} />
          <ol className="guide-list guide-list-num">
            <li>Название — любое, например <span className="guide-code">Velora</span>.</li>
            <li>Подтвердите создание кодами из почты / SMS / Google Authenticator.</li>
            <li>Права: оставьте <b>«Чтение»</b> и включите <b>«Торговля бессрочными фьючерсами»</b>.</li>
            <li>Ограничение по IP не включайте: адрес сервера бота может меняться, и ключ перестанет работать.</li>
            <li>Скопируйте <b>API Key</b> и <b>Secret Key</b>. Secret показывается только один раз — сохраните его сразу.</li>
          </ol>
          <Callout kind="warn">
            Никогда не включайте право <b>«Вывод средств»</b>. Боту оно не нужно, а без него никто не сможет вывести
            деньги с вашего счёта даже с ключом.
          </Callout>
          <Callout kind="ok">Один и тот же ключ работает и для Testnet (VST), и для Live (USDT).</Callout>
        </Step>

        <Step id="connect" n={5} icon={Link2} title="Подключите ключ в приложении">
          <ol className="guide-list guide-list-num">
            <li>Выберите режим: <b>Testnet</b> (рекомендуем для начала) или <b>Live</b>.</li>
            <li>Вставьте API Key и Secret Key и нажмите «Подключить».</li>
            <li>Бот проверит ключ на бирже, сохранит его в зашифрованном виде и сразу начнёт работать.</li>
          </ol>
          <p>Переключиться между Testnet и Live или заменить ключ можно позже: <Path items={["Профиль", "Подключение к бирже"]} /></p>
        </Step>

        <Step id="settings" n={6} icon={SlidersHorizontal} title="Настройте риск под себя">
          <Path items={["Настройки", "Торговля"]} />
          <div className="guide-kv">
            <div><b>Торговля включена</b><span>главный выключатель новых сделок; открытые позиции бот продолжает вести</span></div>
            <div><b>Максимум позиций</b><span>сколько сделок может быть открыто одновременно</span></div>
            <div><b>Мин. и макс. маржа</b><span>сколько USDT бот вкладывает в одну сделку</span></div>
            <div><b>Максимальный стоп</b><span>самый большой допустимый убыток одной сделки в % ROI</span></div>
          </div>
          <Callout>Начните с небольших значений и увеличивайте, когда привыкнете к поведению бота.</Callout>
        </Step>

        <Step id="security" icon={ShieldCheck} title="Безопасность">
          <ul className="guide-list">
            <li>Бот не знает ваш пароль от BingX и не может войти в аккаунт.</li>
            <li>Без права «Вывод средств» ключ позволяет только торговать — вывести деньги невозможно.</li>
            <li>Ключи хранятся в зашифрованном виде.</li>
            <li>Отозвать доступ можно в любой момент: удалите ключ в «Профиле» или на BingX в «Управлении API».</li>
          </ul>
        </Step>

        <Step id="billing" icon={Receipt} title="Оплата">
          {billing?.exempt ? (
            <p>Для вашего аккаунта оплата не взимается.</p>
          ) : (
            <ul className="guide-list">
              <li>Первые <b>7 дней — бесплатно</b>.</li>
              <li>Дальше — <b>{fee}% от чистой прибыли</b> за каждую неделю, только на <b>Live</b>. Testnet бесплатный.</li>
              <li>Нет прибыли — нет оплаты. Убыточная неделя переносится: комиссия начислится, только когда минус отыгран.</li>
              <li>Пока счёт не оплачен, новые сделки не открываются (открытые бот продолжает вести). Оплата — через администратора.</li>
            </ul>
          )}
          <p>Текущая неделя и счета — в <Path items={["Профиль", "Подписка"]} /></p>
        </Step>

        <Step id="emergency" icon={Siren} title="Аварийная остановка">
          <p>
            Если нужно срочно всё остановить — отправьте боту команду <span className="guide-code">/emergency</span>
            {" "}или нажмите «Аварийная остановка» под сообщением <span className="guide-code">/start</span>. Можно выбрать:
          </p>
          <ul className="guide-list">
            <li><b>Только остановить торговлю</b> — новых сделок не будет, открытые остаются со стопами.</li>
            <li><b>Остановить и закрыть позиции</b> — бот закроет всё по рынку.</li>
          </ul>
          <p>Включить торговлю снова: <Path items={["Настройки", "Торговля включена"]} /></p>
        </Step>

        <section className="guide-card" id="faq">
          <div className="guide-card-head">
            <div className="guide-step-badge"><HelpCircle size={18} /></div>
            <div className="guide-card-title">Вопросы и ответы</div>
          </div>
          <div className="guide-card-body guide-faq-list">
            <Faq q="Ключ не подключается — что делать?">
              Проверьте, что скопировали ключ и Secret полностью, без пробелов; что включено право «Торговля
              бессрочными фьючерсами»; что ключ не привязан к IP и не удалён на бирже. Текст ошибки биржи
              показывается под полями.
            </Faq>
            <Faq q="Ключ подключён, но сделок нет">
              Это нормально: бот входит только когда стратегия видит сигнал — иногда это часы. Проверьте также, что
              включён хедж-режим, на фьючерсном счёте есть деньги, в «Настройках» включена торговля и нет
              неоплаченного счёта (тогда сверху будет красная плашка).
            </Faq>
            <Faq q="Чем Testnet отличается от Live?">
              Testnet — демо-счёт BingX с виртуальными VST: сделки настоящие по логике, но деньги ненастоящие.
              Live — реальные USDT. Начните с Testnet, посмотрите на результат, потом переходите на Live.
            </Faq>
            <Faq q="Почему сделка закрылась в минус?">
              Сработал стоп-лосс — это часть стратегии: убытки ограничиваются, прибыльные сделки держатся дольше.
              Оценивайте результат за неделю-месяц, а не по одной сделке.
            </Faq>
            <Faq q="Можно торговать вручную на этом же счёте?">
              Можно, но лучше на других монетах: бот видит только свои сделки, а ручные позиции на тех же монетах
              могут мешать его ордерам и стопам.
            </Faq>
            <Faq q="Можно закрыть сделку бота самому?">
              Да: на вкладке «Статистика» у открытой позиции есть кнопка закрытия по рынку. Закрытие прямо на бирже
              бот тоже увидит.
            </Faq>
            <Faq q="Как отключить бота совсем?">
              Выключите «Торговля включена» в настройках и закройте позиции, затем удалите ключ в «Профиле»
              или на BingX.
            </Faq>
            <Faq q="Сколько бот зарабатывает?">
              Гарантий нет — результат зависит от рынка. Ориентируйтесь на свою статистику в Testnet и на вкладке
              «Статистика».
            </Faq>
            <Faq q="Не приходят уведомления">
              Убедитесь, что бот не заблокирован в Telegram и вы нажимали в нём «Старт» — без этого бот не может
              писать вам первым.
            </Faq>
          </div>
        </section>

        <div className="guide-footer">Остались вопросы — напишите администратору.</div>
      </div>
    </div>
  );
}

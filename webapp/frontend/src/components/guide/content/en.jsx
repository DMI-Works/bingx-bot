import {
  UserPlus, Wallet, Repeat, KeyRound, Link2, SlidersHorizontal, ShieldCheck, Receipt, Siren, HelpCircle, LineChart,
} from "lucide-react";
import { Callout, Step, Path, Faq } from "../GuideParts";

// Guide text (English). Section structure and ids match ru.jsx / uk.jsx.
export default function GuideBodyEn({ billing, fee }) {
  return (
    <>
      <Step id="how" icon={LineChart} title="How it works">
        <p>
          The bot watches the market around the clock and opens and closes trades on <b>BingX perpetual futures</b>
          {" "}by itself, using strategies configured by the administrator. The money always stays on <b>your</b> BingX
          account — the bot only sends orders there through the API key.
        </p>
        <ul className="guide-list">
          <li>Every trade gets a stop loss on the exchange right away — it triggers even if the bot is offline.</li>
          <li>When a trade goes into profit, the stop is pulled up: first to breakeven, then behind the price.</li>
          <li>The bot messages you in Telegram about every open, close and stop move.</li>
          <li>All trades and results are on the “Statistics” tab, details are in “Analytics”.</li>
        </ul>
        <Callout kind="warn">
          Leveraged futures trading is risky: losing streaks happen. Only connect money you are ready to risk.
          Start with <b>Testnet</b> — a demo account with virtual money.
        </Callout>
      </Step>

      <Step id="account" n={1} icon={UserPlus} title="BingX account">
        <p>If you don't have an account yet, sign up at <b>bingx.com</b> or in the BingX app.</p>
        <ul className="guide-list">
          <li>Complete identity verification (KYC) — without it the exchange may restrict API keys and trading.</li>
          <li>Enable two-factor authentication (Google Authenticator) — you'll need it to create a key.</li>
        </ul>
      </Step>

      <Step id="funds" n={2} icon={Wallet} title="Funds on the futures account">
        <p>
          The bot trades only from the <b>USDT futures account (perpetual futures)</b>. It cannot see money on the spot account.
        </p>
        <div className="guide-mode-grid">
          <div className="guide-mode guide-mode-test">
            <div className="guide-mode-title">Testnet</div>
            <div className="guide-mode-text">
              BingX demo account with virtual <b>VST</b>. Enable demo trading in the futures section —
              the exchange gives you the virtual balance. You risk nothing.
            </div>
          </div>
          <div className="guide-mode guide-mode-live">
            <div className="guide-mode-title">Live</div>
            <div className="guide-mode-text">
              Real <b>USDT</b>. Transfer them to the futures account:
              <Path items={["Assets", "Transfer", "Spot → Perpetual Futures"]} />
            </div>
          </div>
        </div>
        <Callout>
          How much you need: each trade uses margin between the minimum and maximum (10–100 USDT by default),
          with up to 3 trades open at once. A guideline is 50 USDT and up, 100–200 USDT is more comfortable.
        </Callout>
      </Step>

      <Step id="hedge" n={3} icon={Repeat} title="Enable hedge mode">
        <p>
          The bot opens positions with an explicit side (LONG / SHORT), so the exchange must be in
          <b> hedge mode</b> (two-way positions). In one-way mode the exchange will reject the bot's orders.
        </p>
        <Path items={["Futures", "Settings ⚙", "Position mode", "Hedge"]} />
        <Callout>The mode can't be changed while there are open positions or orders — switch it before connecting the bot. If you start with Testnet, check the mode on the demo account as well.</Callout>
      </Step>

      <Step id="keys" n={4} icon={KeyRound} title="Create an API key">
        <Path items={["Profile", "API Management", "Create API"]} />
        <ol className="guide-list guide-list-num">
          <li>Any name, for example <span className="guide-code">Velora</span>.</li>
          <li>Confirm the creation with codes from email / SMS / Google Authenticator.</li>
          <li>Permissions: keep <b>“Read”</b> and enable <b>“Perpetual Futures trading”</b>.</li>
          <li>Don't enable IP restriction: the bot server's address may change and the key would stop working.</li>
          <li>Copy the <b>API Key</b> and <b>Secret Key</b>. The secret is shown only once — save it right away.</li>
        </ol>
        <Callout kind="warn">
          Never enable the <b>“Withdraw”</b> permission. The bot doesn't need it, and without it nobody can withdraw
          money from your account even with the key.
        </Callout>
        <Callout kind="ok">The same key works for both Testnet (VST) and Live (USDT).</Callout>
      </Step>

      <Step id="connect" n={5} icon={Link2} title="Connect the key in the app">
        <ol className="guide-list guide-list-num">
          <li>Choose the mode: <b>Testnet</b> (recommended to start) or <b>Live</b>.</li>
          <li>Paste the API Key and Secret Key and tap “Connect”.</li>
          <li>The bot checks the key on the exchange, stores it encrypted and starts working right away.</li>
        </ol>
        <p>You can switch between Testnet and Live or replace the key later: <Path items={["Profile", "Exchange connection"]} /></p>
      </Step>

      <Step id="settings" n={6} icon={SlidersHorizontal} title="Tune the risk">
        <Path items={["Settings", "Trading"]} />
        <div className="guide-kv">
          <div><b>Trading enabled</b><span>main switch for new trades; the bot keeps managing open positions</span></div>
          <div><b>Maximum positions</b><span>how many trades can be open at once</span></div>
          <div><b>Min and max margin</b><span>how many USDT the bot puts into one trade</span></div>
          <div><b>Maximum stop</b><span>the largest allowed loss of one trade in % ROI</span></div>
        </div>
        <Callout>Start with small values and increase them once you get used to how the bot behaves.</Callout>
      </Step>

      <Step id="security" icon={ShieldCheck} title="Security">
        <ul className="guide-list">
          <li>The bot doesn't know your BingX password and can't log into your account.</li>
          <li>Without the “Withdraw” permission the key only allows trading — withdrawing money is impossible.</li>
          <li>Keys are stored encrypted.</li>
          <li>You can revoke access at any time: delete the key in “Profile” or on BingX in “API Management”.</li>
        </ul>
      </Step>

      <Step id="billing" icon={Receipt} title="Payment">
        {billing?.exempt ? (
          <p>No payment is charged for your account.</p>
        ) : (
          <ul className="guide-list">
            <li>The first <b>7 days are free</b>.</li>
            <li>After that — <b>{fee}% of net profit</b> for each week, only on <b>Live</b>. Testnet is free.</li>
            <li>No profit — no payment. A losing week carries over: the fee is charged only once the loss is recovered.</li>
            <li>While an invoice is unpaid, no new trades are opened (the bot keeps managing open ones). Payment goes through the administrator.</li>
          </ul>
        )}
        <p>Current week and invoices are in <Path items={["Profile", "Subscription"]} /></p>
      </Step>

      <Step id="emergency" icon={Siren} title="Emergency stop">
        <p>
          If you need to stop everything urgently, send the bot the <span className="guide-code">/emergency</span> command
          {" "}or tap “Emergency stop” under the <span className="guide-code">/start</span> message. You can choose:
        </p>
        <ul className="guide-list">
          <li><b>Only stop trading</b> — no new trades, open ones stay with their stops.</li>
          <li><b>Stop and close positions</b> — the bot closes everything at market.</li>
        </ul>
        <p>Turn trading back on: <Path items={["Settings", "Trading enabled"]} /></p>
      </Step>

      <section className="guide-card" id="faq">
        <div className="guide-card-head">
          <div className="guide-step-badge"><HelpCircle size={18} /></div>
          <div className="guide-card-title">FAQ</div>
        </div>
        <div className="guide-card-body guide-faq-list">
          <Faq q="The key won't connect — what should I do?">
            Check that you copied the key and secret fully, without spaces; that the “Perpetual Futures trading”
            permission is enabled; that the key isn't bound to an IP and hasn't been deleted on the exchange. The
            exchange's error text is shown under the fields.
          </Faq>
          <Faq q="The key is connected, but there are no trades">
            That's normal: the bot enters only when a strategy sees a signal — sometimes it takes hours. Also check
            that hedge mode is on, there is money on the futures account, trading is enabled in “Settings” and there
            is no unpaid invoice (a red banner would be shown at the top).
          </Faq>
          <Faq q="What's the difference between Testnet and Live?">
            Testnet is a BingX demo account with virtual VST: the trades are real in logic, but the money isn't.
            Live is real USDT. Start with Testnet, look at the results, then move to Live.
          </Faq>
          <Faq q="Why did a trade close at a loss?">
            The stop loss triggered — that's part of the strategy: losses are limited, profitable trades are held
            longer. Judge results over a week or a month, not by a single trade.
          </Faq>
          <Faq q="Can I trade manually on the same account?">
            Yes, but better on other coins: the bot only sees its own trades, and manual positions on the same coins
            can interfere with its orders and stops.
          </Faq>
          <Faq q="Can I close a bot trade myself?">
            Yes: on the “Statistics” tab an open position has a market close button. The bot will also notice if you
            close it directly on the exchange.
          </Faq>
          <Faq q="How do I turn the bot off completely?">
            Turn off “Trading enabled” in settings and close the positions, then delete the key in “Profile”
            or on BingX.
          </Faq>
          <Faq q="How much does the bot earn?">
            There are no guarantees — results depend on the market. Go by your own statistics on Testnet and on the
            “Statistics” tab.
          </Faq>
          <Faq q="I don't get notifications">
            Make sure the bot isn't blocked in Telegram and that you pressed “Start” in it — without that the bot
            can't message you first.
          </Faq>
        </div>
      </section>
    </>
  );
}

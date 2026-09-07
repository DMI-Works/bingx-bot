import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App.jsx";

const tg = window.Telegram?.WebApp;

if (tg) {
  tg.ready();
  tg.expand();
  tg.setHeaderColor?.("#0B0E13");
  tg.setBackgroundColor?.("#0B0E13");
}

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);

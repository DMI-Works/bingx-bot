"""
Заголовки безпеки для всіх відповідей (API і статика Mini App).

CORS не вмикаємо навмисно: Mini App і API живуть на одному домені (Vite у
dev-режимі проксіює /api), тож чужі сайти не повинні читати відповіді API.
X-Frame-Options не ставимо: Telegram Web відкриває Mini App у фреймі —
дозволені «батьки» задаються через frame-ancestors.
"""

from fastapi import FastAPI, Request

CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://telegram.org",
    # recharts та index.html використовують inline-стилі; шрифти — Google Fonts
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com data:",
    # аватар користувача приходить з t.me
    "img-src 'self' data: blob: https:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org",
])

SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
}


def install_security_headers(app: FastAPI) -> None:
    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.path.startswith("/api/"):
            # відповіді API містять баланс/позиції — не кешуємо ніде
            response.headers.setdefault("Cache-Control", "no-store")
        return response

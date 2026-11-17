"""
Мова відповідей API: Mini App надсилає поточну мову в заголовку X-Lang
(en/ru/uk). Middleware кладе її в контекст запиту, а tr() перекладає тексти
помилок з каталогу core/common/i18n.py — без передавання request у кожну
функцію. Немає заголовка — англійська.
"""

from contextvars import ContextVar

from fastapi import FastAPI, Request

from core.common.i18n import DEFAULT_LANG, normalize_lang, t

_request_lang: ContextVar[str] = ContextVar("request_lang", default=DEFAULT_LANG)


def request_lang() -> str:
    return _request_lang.get()


def tr(key: str, **kwargs) -> str:
    return t(_request_lang.get(), key, **kwargs)


def install_request_lang(app: FastAPI) -> None:
    @app.middleware("http")
    async def _lang(request: Request, call_next):
        token = _request_lang.set(normalize_lang(request.headers.get("x-lang")))
        try:
            return await call_next(request)
        finally:
            _request_lang.reset(token)

"""V6 phase 12: the interface's dictionary in each language, and the language saved in the account."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app import i18n
from app.api.deps import current_user, get_state
from app.errors import AppError
from app.i18n import store

router = APIRouter(prefix="/api", tags=["i18n"])


@router.get("/i18n/{lang}")
def dictionary(lang: str, request: Request, response: Response) -> dict:
    """{msgid: text} for every interface text of the language (Arabic: only the panel's edits and plural forms).
    Small, cached by the app with its `version`; fetched again when a text changes in the panel."""
    code = i18n.normalize(lang)
    if code is None or code != lang:
        raise AppError(404, "not_found", "غير موجود.")
    data = store.client_messages(code)
    response.headers["ETag"] = f'"{data["version"]}"'
    return data


class LanguageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lang: str = Field(max_length=5)


@router.put("/me/language")
def set_language(body: LanguageBody, request: Request, response: Response) -> dict:
    """The language follows the account: chosen in «الإعدادات ← اللغة» (or the phone's language the first time)."""
    code = i18n.normalize(body.lang)
    if code is None:
        raise AppError(400, "invalid_input", "لغة غير مدعومة.")
    st = get_state(request)
    with st.database.session() as db:
        user = current_user(request, db)
        user.language = code
    set_language_cookie(response, request, code)
    return {"language": code}


def set_language_cookie(response: Response, request: Request, lang: str) -> None:
    """For the pages served without JavaScript (policies, download, the boot screen): not a secret."""
    st = get_state(request)
    response.set_cookie("dz_lang", lang, max_age=365 * 86400, path="/", secure=st.settings.COOKIE_SECURE,
                        httponly=False, samesite="lax")

"""V6 phase 2: the market list for the Home screen (read-only; the server alone talks to Bybit)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import current_user, get_state
from app.services import market

router = APIRouter(prefix="/api", tags=["market"])


@router.get("/market")
def market_list(request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        current_user(request, db)
        return market.current(db, st.settings)

"""
Modulo di autenticazione condiviso per le dashboard HTTP dei bot della suite ITA.

Identico in tutti i repo della suite (coordinator, yield, neutral, lp, dca, perp,
degen): un fix qui va copiato invariato negli altri, cosi' non si ripete il gap
per cui /api/release_funds su LP era rimasto senza controllo del token mentre gli
altri bot lo richiedevano.
"""

import hmac
from email.message import Message
from typing import Union


def is_run_token_valid(headers: Union[Message, dict], run_token: str) -> bool:
    """
    Verifica il token di autorizzazione per le chiamate POST della dashboard.
    Accetta il token via header X-Run-Token, X-Admin-Token, o Authorization
    (con o senza prefisso "Bearer "). Confronto a tempo costante con hmac.
    """
    if not run_token:
        return False

    provided = headers.get("X-Run-Token", "") or headers.get("X-Admin-Token", "")
    if not provided and "Authorization" in headers:
        auth = headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            provided = auth[7:].strip()
        else:
            provided = auth.strip()

    return bool(provided and hmac.compare_digest(provided, run_token))

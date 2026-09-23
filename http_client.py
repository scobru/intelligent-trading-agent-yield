"""
Client HTTP condiviso per le API pubbliche (DefiLlama in primis).

Queste API hanno rate limit stretti e gratuiti: GeckoTerminal risponde 429
dopo una trentina di richieste al minuto. Qui stanno le tre difese che
servono, in un punto solo:

  - pacing per host: non si spara piu' veloce di quanto l'host accetti;
  - retry con backoff che rispetta l'header Retry-After;
  - cache a tempo: nello stesso ciclo la stessa URL si scarica una volta.
"""

import logging
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

# Intervallo minimo fra due richieste allo stesso host, in secondi.
# GeckoTerminal free: ~30 richieste/minuto -> 2.1s di margine.
HOST_MIN_INTERVAL = {
    "api.geckoterminal.com": 2.1,
    "api.gopluslabs.io": 2.0,
    "api.dexscreener.com": 0.3,
    "tokens.coingecko.com": 1.0,
    "yields.llama.fi": 1.0,
}
DEFAULT_MIN_INTERVAL = 0.5

_lock = threading.Lock()
_last_call: Dict[str, float] = {}
_cache: Dict[str, Any] = {}


def _cache_key(url: str, params: Optional[Dict[str, Any]]) -> str:
    if not params:
        return url
    return url + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))


def _wait_turn(host: str):
    """Fa passare abbastanza tempo dall'ultima richiesta allo stesso host."""
    min_interval = HOST_MIN_INTERVAL.get(host, DEFAULT_MIN_INTERVAL)
    with _lock:
        last = _last_call.get(host, 0.0)
        delay = min_interval - (time.time() - last)
        if delay > 0:
            time.sleep(delay)
        _last_call[host] = time.time()


def get_json(url: str, params: Dict[str, Any] = None, timeout: int = 20,
             cache_ttl: float = 0, max_retries: int = 3) -> Optional[Any]:
    """
    GET con pacing, retry e cache. Restituisce None se la risorsa resta
    irraggiungibile: chi chiama decide se e' fatale.
    """
    key = _cache_key(url, params)

    if cache_ttl > 0:
        cached = _cache.get(key)
        if cached and (time.time() - cached["time"]) < cache_ttl:
            return cached["data"]

    host = urlparse(url).netloc
    backoff = 2.0

    for attempt in range(1, max_retries + 1):
        _wait_turn(host)
        try:
            resp = requests.get(url, params=params, timeout=timeout,
                                headers={"accept": "application/json"})

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else backoff
                except ValueError:
                    wait = backoff
                wait = min(wait, 30.0)
                if attempt < max_retries:
                    logger.warning(
                        "%s ha risposto 429 (tentativo %s/%s): attendo %.1fs",
                        host, attempt, max_retries, wait,
                    )
                    time.sleep(wait)
                    backoff *= 2
                    continue
                logger.warning("%s: rate limit non rientrato dopo %s tentativi", host, max_retries)
                return None

            resp.raise_for_status()
            data = resp.json()

            if cache_ttl > 0:
                _cache[key] = {"time": time.time(), "data": data}
            return data

        except requests.exceptions.RequestException as exc:
            if attempt < max_retries:
                logger.warning("Richiesta a %s fallita (%s/%s): %s — riprovo",
                               host, attempt, max_retries, exc)
                time.sleep(backoff)
                backoff *= 2
                continue
            logger.warning("Richiesta fallita %s: %s", url, exc)
            return None
        except ValueError as exc:  # JSON malformato
            logger.warning("Risposta non JSON da %s: %s", url, exc)
            return None

    return None


def clear_cache():
    """Usata dai test; in produzione la cache scade da sola."""
    _cache.clear()
    _last_call.clear()

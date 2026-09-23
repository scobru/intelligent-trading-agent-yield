#!/bin/bash
set -u

echo "========================================================"
echo "Starting Yield Agent (Base passive yield) on Docker"
echo "========================================================"

# Il rendimento si muove lentamente: un ciclo all'ora basta e avanza
INTERVAL="${TRADING_INTERVAL:-3600}"

echo "[1/2] Starting Web Dashboard on port ${PORT:-3000}..."
python dashboard.py &

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    echo "📱 Starting Telegram Bot listener..."
    python telegram_bot.py &
fi

echo "[2/2] Starting yield loop (interval: ${INTERVAL}s)..."
if [ "${PAPER_TRADING:-false}" = "true" ]; then
    echo "📝 PAPER attivo: portafoglio virtuale."
elif [ "${DRY_RUN:-true}" = "true" ]; then
    echo "🧪 DRY-RUN attivo: nessuna transazione verrà firmata."
fi
echo ""

while true; do
    echo "⏰ [$(date -u +%Y-%m-%dT%H:%M:%SZ)] Running yield cycle..."
    python main.py
    echo "💤 Sleeping for ${INTERVAL} seconds until next cycle..."
    sleep "${INTERVAL}"
done

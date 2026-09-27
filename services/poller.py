"""
Резервний поллер Zoom: періодично перевіряє хмарні записи та ставить нові
зустрічі в чергу (на випадок, якщо вебхук не дійшов).
"""
import logging
import os
from collections import Counter
from datetime import datetime, timedelta, timezone

from services import zoom
from services.pipeline import ingest_zoom_meeting

log = logging.getLogger(__name__)

POLL_LOOKBACK_DAYS = int(os.environ.get("ZOOM_POLL_LOOKBACK_DAYS", "3"))


def poll_once(days: int | None = None) -> dict:
    """Перевіряє записи за останні `days` днів. Повертає лічильники результатів."""
    days = max(1, int(days or POLL_LOOKBACK_DAYS))
    today = datetime.now(timezone.utc).date()
    meetings = zoom.list_recordings(today - timedelta(days=days), today)
    counts = Counter()
    for meeting in meetings:
        try:
            result = ingest_zoom_meeting(zoom.meeting_info(meeting), origin="poller")
            counts[result["result"]] += 1
        except Exception:
            log.exception("Поллер: не вдалося обробити зустріч %s", meeting.get("uuid"))
            counts["error"] += 1
    summary = {"meetings": len(meetings), **counts}
    if counts.get("queued"):
        log.info("Поллер Zoom: %s", summary)
    return summary

"""알림: 콘솔 + (설정 시) 텔레그램. 알림 실패가 매매 루프를 멈추게 하지 않는다."""
from __future__ import annotations

import os
import sys
from collections.abc import Callable
from datetime import datetime
from typing import Any

import requests


def make_notifier(prefix: str, post: Callable[..., Any] | None = None) -> Callable[[str], None]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    post = post or requests.post

    def notify(msg: str) -> None:
        line = f"[{datetime.now():%H:%M:%S}] [{prefix}] {msg}"
        print(line, flush=True)
        if token and chat:
            try:
                post(f"https://api.telegram.org/bot{token}/sendMessage",
                     json={"chat_id": chat, "text": line}, timeout=5)
            except requests.RequestException as e:
                print(f"알림 전송 실패: {e}", file=sys.stderr)

    return notify

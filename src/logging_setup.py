"""공용 로깅 설정."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_CONFIGURED = False


def setup_logging(level: str = "INFO", log_dir: str | Path = "logs",
                  log_file: str = "bot.log") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    # Windows 콘솔(cp949) 등에서 비호환 글자로 크래시하지 않도록 (인코딩 유지, 대체문자 처리)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s"
    handlers: list[logging.Handler] = []
    # 무콘솔(pythonw, Task Scheduler 데몬)에선 sys.stdout 이 None → StreamHandler 생략,
    # 파일 핸들러만으로 로깅한다(콘솔 없으면 콘솔 핸들러가 emit 에서 깨짐).
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    handlers.append(logging.FileHandler(Path(log_dir) / log_file, encoding="utf-8"))
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format=fmt, handlers=handlers)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

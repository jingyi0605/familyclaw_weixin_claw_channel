from __future__ import annotations

import logging
from pathlib import Path

from .models import WeixinRuntimeContext


def get_logger(name: str, *, context: WeixinRuntimeContext | None = None) -> logging.Logger:
    """统一插件 logger 名称，并按需挂载私有日志文件。"""

    logger = logging.getLogger(f"plugins.weixin_claw_channel.{name}")
    if context is not None:
        _ensure_file_handler(logger=logger, target=context.logs_dir / f"{name}.log")
    return logger


def _ensure_file_handler(*, logger: logging.Logger, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    handler_key = str(target.resolve())
    for handler in logger.handlers:
        if getattr(handler, "_weixin_log_target", None) == handler_key:
            return
    file_handler = logging.FileHandler(target, encoding="utf-8")
    file_handler._weixin_log_target = handler_key  # type: ignore[attr-defined]
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(file_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

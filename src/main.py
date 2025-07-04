#!/usr/bin/env python
# -*- coding: UTF-8 -*-

#  Copyright (C) 2024. Suto-Commune
#  _
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Affero General Public License as
#  published by the Free Software Foundation, either version 3 of the
#  License, or (at your option) any later version.
#  _
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Affero General Public License for more details.
#  _
#  You should have received a copy of the GNU Affero General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
@File       : main.py

@Author     : hsn

@Date       : 2024/11/16 下午10:50
"""

import fastapi
import logging
from loguru import logger

from .router import routers


class InterceptHandler(logging.Handler):
    def emit(self, record):
        # 获取 Loguru 的 level
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        logger.opt(depth=6, exception=record.exc_info).log(level, record.getMessage())


# 拦截标准日志
logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

# 可选：拦截 Uvicorn/Starlette 的 logger
for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
    logging.getLogger(name).handlers = [InterceptHandler()]
    logging.getLogger(name).propagate = False

logger.add(
    sink="logs/auto-tweet_general.log",
    rotation="500 MB",
    retention="30 days",
    compression="zip",
    level="INFO",
)
logger.add(
    sink="logs/auto-tweet_error.log",
    rotation="500 MB",
    retention="30 days",
    compression="zip",
    level="ERROR",
    backtrace=True,
    diagnose=True,
)
app = fastapi.FastAPI()
app.include_router(routers, prefix="/api/v1")

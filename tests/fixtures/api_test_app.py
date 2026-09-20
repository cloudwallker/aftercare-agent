"""真实 TCP 测试 API：客户隔离由测试进程指定，不开放 HTTP 故障入口。"""

import os

from aftercare.api import create_app as application
from aftercare.contracts import Actor


def create_app():
    return application(
        actors={
            "client": Actor(os.environ["TEST_CUSTOMER"], "client"),
            "reviewer": Actor("reviewer_demo", "reviewer"),
        }
    )

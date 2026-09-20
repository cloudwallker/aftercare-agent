"""仅测试子进程使用的故障组合入口；生产配置没有故障开关。"""

from mock_merchant.api import create_app as merchant_app


def create_app():
    return merchant_app(testing=True, fault_mode="commit_then_503_once")

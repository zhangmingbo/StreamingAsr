"""
asr_license_client.py - ASR服务授权客户端
启动时验证授权，之后每日定时重新验证。
连续验证失败则停止服务。

环境变量:
  LICENSE_SERVER        - 授权服务完整地址（优先级最高），如 http://192.168.1.100:9800
  LICENSE_SERVER_IP     - 授权服务 IP，默认 host.docker.internal
  LICENSE_SERVER_PORT   - 授权服务端口，默认 9800
  PRODUCT               - 产品标识: offline_asr / streaming_asr / streaming_tts / chatbot
  CLIENT_SECRET         - 客户端密钥（覆盖默认值）
  LICENSE_CHECK_HOUR    - 每日检查小时（24h），默认 23
  LICENSE_CHECK_MINUTE  - 每日检查分钟，默认 0
  LICENSE_RETRY_INTERVAL - 失败重试间隔（秒），默认 300
  LICENSE_MAX_FAILURES  - 最大连续失败次数，默认 3
  LICENSE_TIMEOUT       - 请求超时（秒），默认 10
"""
import os
import sys
import time
import threading
import logging
import datetime
from typing import Optional, Dict, Any

import requests

logger = logging.getLogger(__name__)

# 产品标识与默认密钥映射
PRODUCT_SECRETS = {
    "offline_asr": "secret_offline_asr_2024",
    "streaming_asr": "secret_streaming_asr_2024",
    "streaming_tts": "secret_streaming_tts_2024",
    "chatbot": "secret_chatbot_2024",
}

# 每日检查时间（24小时制）—— 可通过环境变量覆盖
CHECK_HOUR = int(os.environ.get("LICENSE_CHECK_HOUR", "23"))
CHECK_MINUTE = int(os.environ.get("LICENSE_CHECK_MINUTE", "0"))

# 失败重试间隔（秒）—— 可通过环境变量覆盖
RETRY_INTERVAL = int(os.environ.get("LICENSE_RETRY_INTERVAL", "300"))

# 最大连续失败次数 —— 可通过环境变量覆盖
MAX_FAILURES = int(os.environ.get("LICENSE_MAX_FAILURES", "3"))

# 请求超时（秒）—— 可通过环境变量覆盖
REQUEST_TIMEOUT = int(os.environ.get("LICENSE_TIMEOUT", "10"))


class ASRLicenseClient:
    """ASR服务授权客户端"""

    def __init__(self, product: str, license_server: str = None, client_secret: str = None):
        self.product = product
        # License 服务器地址：优先完整 URL，其次 IP+端口 组合，最后默认值
        if license_server:
            self.license_server = license_server.rstrip("/")
        else:
            env_full = os.environ.get("LICENSE_SERVER")
            if env_full:
                self.license_server = env_full.rstrip("/")
            else:
                server_ip = os.environ.get("LICENSE_SERVER_IP", "host.docker.internal")
                server_port = os.environ.get("LICENSE_SERVER_PORT", "9800")
                self.license_server = f"http://{server_ip}:{server_port}"
        self.client_secret = (
            client_secret or os.environ.get("CLIENT_SECRET")
            or PRODUCT_SECRETS.get(product, f"secret_{product}_2024")
        )

        self.customer: Optional[str] = None
        self.expire_date: Optional[str] = None
        self.features: Dict[str, Any] = {}
        self.license_valid: bool = False

        self._check_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ============================================================
    # 公开接口
    # ============================================================

    def verify(self) -> bool:
        """启动时验证授权，通过后启动每日检查"""
        logger.info(f"[License] 启动验证: product={self.product}, server={self.license_server}")

        if not self._do_verify():
            logger.error("[License] 启动授权验证失败，服务退出")
            return False

        self.license_valid = True
        logger.info(f"[License] 授权通过 | 客户={self.customer} 到期={self.expire_date}")
        for k, v in self.features.items():
            logger.info(f"  {k}: {v}")

        self._start_daily_check()
        return True

    def stop(self):
        """停止后台检查"""
        self._stop_event.set()
        if self._check_thread and self._check_thread.is_alive():
            self._check_thread.join(timeout=5)

    def is_valid(self) -> bool:
        return self.license_valid

    def get_feature(self, key: str, default=None):
        return self.features.get(key, default)

    # ============================================================
    # 内部方法
    # ============================================================

    def _do_verify(self) -> bool:
        """向授权服务发起一次验证"""
        try:
            resp = requests.post(
                f"{self.license_server}/verify",
                json={
                    "product": self.product,
                    "clientSecret": self.client_secret,
                },
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                return False

            data = resp.json()
            if not data.get("valid"):
                return False

            # 更新信息
            self.customer = data.get("customer", self.customer)
            self.expire_date = data.get("expireDate", self.expire_date)
            self.features = data.get("features", self.features)
            return True

        except Exception:
            return False

    def _start_daily_check(self):
        """启动每日23:00后台检查线程"""
        if self._check_thread and self._check_thread.is_alive():
            return
        self._stop_event.clear()
        self._check_thread = threading.Thread(
            target=self._daily_check_loop, daemon=True, name="license-daily-check"
        )
        self._check_thread.start()
        logger.info(f"[License] 每日检查已启动（每天 {CHECK_HOUR:02d}:{CHECK_MINUTE:02d}）")

    def _daily_check_loop(self):
        """每日检查循环"""
        while not self._stop_event.is_set():
            # 计算距离下次检查时间的秒数
            now = datetime.datetime.now()
            next_check = now.replace(hour=CHECK_HOUR, minute=CHECK_MINUTE, second=0, microsecond=0)
            if next_check <= now:
                # 今天已过，等到明天
                next_check += datetime.timedelta(days=1)

            wait_seconds = (next_check - now).total_seconds()
            logger.info(f"[License] 下次检查时间: {next_check.strftime('%Y-%m-%d %H:%M')}（{wait_seconds/3600:.1f}小时后）")

            # 等待到检查时间（每秒检查一次是否要停止）
            if self._stop_event.wait(timeout=wait_seconds):
                break

            # 执行检查（含重试）
            self._run_check_with_retry()

    def _run_check_with_retry(self):
        """执行检查，失败则重试，连续失败超过阈值则停止服务"""
        failures = 0

        while failures < MAX_FAILURES:
            if self._do_verify():
                logger.info(f"[License] 每日检查通过（第{failures + 1}次尝试）")
                self.license_valid = True
                return

            failures += 1
            logger.warning(f"[License] 每日检查失败（{failures}/{MAX_FAILURES}）")

            if failures >= MAX_FAILURES:
                break

            # 等待5分钟后重试
            logger.info(f"[License] {RETRY_INTERVAL // 60}分钟后重试...")
            if self._stop_event.wait(timeout=RETRY_INTERVAL):
                return

        # 连续失败超过阈值，停止服务
        logger.error(f"[License] 连续{MAX_FAILURES}次验证失败，停止服务")
        self.license_valid = False
        os._exit(1)


# ============================================================
# 全局实例管理
# ============================================================

_license_client: Optional[ASRLicenseClient] = None


def init_license_client(product: str = None) -> Optional[ASRLicenseClient]:
    """初始化全局授权客户端并执行验证"""
    global _license_client

    if _license_client is not None:
        return _license_client

    product = product or os.environ.get("PRODUCT", "offline_asr")
    client = ASRLicenseClient(product)

    if client.verify():
        _license_client = client
        return client
    else:
        return None


def get_license_client() -> Optional[ASRLicenseClient]:
    """获取全局授权客户端实例"""
    return _license_client


# ============================================================
# 独立测试
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    print("测试授权客户端...")

    client = ASRLicenseClient(
        product="offline_asr",
        license_server="http://localhost:9800",
    )

    if client.verify():
        print(f"\n授权通过: {client.features}")
        print("等待每日检查（Ctrl+C退出）...")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            client.stop()
            print("\n已停止")
    else:
        print("\n授权验证失败")

"""中台工具层（履约中台 TMS/WMS 侧）。

这一层只做两件事：发请求、把 HTTP 错误翻译成"能直接说给用户听的话"。
业务判断（比如"这单已经报过理赔了""已派送来不及改址"）留在中台，
不在这里重复实现——两处都判最容易出现"前后端说法不一致"。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from app.conf.settings import settings
from app.core.log import get_logger

logger = get_logger("commerce")


@dataclass
class CallResult:
    ok: bool
    status: int = 0
    data: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    error: str = ""          # 面向用户的中文失败原因
    retryable: bool = False
    elapsed: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CommerceClient:
    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        self.base_url = (base_url or settings.commerce.base_url).rstrip("/")
        self.timeout = timeout or settings.commerce.timeout

    async def _request(
        self, method: str, path: str, *, json: dict | None = None, soft_404: str = ""
    ) -> CallResult:
        started = time.perf_counter()
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.request(method, url, json=json)

            elapsed = time.perf_counter() - started
            if resp.status_code == 200:
                body = resp.json()
                return CallResult(
                    ok=True, status=200, data=body.get("data") or {}, elapsed=elapsed
                )

            detail = ""
            try:
                detail = str(resp.json().get("detail") or "")
            except Exception:
                detail = resp.text[:120]

            # 404 分两种：查不到这类数据（软失败，说清楚就行）与资源不存在
            soft = resp.status_code == 404 and bool(soft_404)
            logger.info(f"[commerce] {method} {path} -> {resp.status_code} {detail[:60]}")
            return CallResult(
                ok=False,
                status=resp.status_code,
                error=soft_404 if soft else (detail or "查询失败，稍后再试"),
                retryable=resp.status_code >= 500,
                elapsed=elapsed,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            elapsed = time.perf_counter() - started
            logger.warning(f"[commerce] {method} {path} 连接失败：{type(exc).__name__}")
            return CallResult(
                ok=False,
                status=0,
                error="履约系统暂时连不上，请稍后再试。",
                retryable=True,
                elapsed=elapsed,
            )

    # ---------- 读 ----------
    async def shipment_status(self, tracking_number: str) -> CallResult:
        return await self._request(
            "GET",
            f"/shipments/{tracking_number}/status",
            soft_404=f"没查到运单 {tracking_number}，麻烦确认一下运单号。",
        )

    async def shipment_detail(self, tracking_number: str) -> CallResult:
        return await self._request(
            "GET",
            f"/shipments/{tracking_number}",
            soft_404=f"没查到运单 {tracking_number}，麻烦确认一下运单号。",
        )

    async def shipment_tracking(self, tracking_number: str) -> CallResult:
        return await self._request(
            "GET",
            f"/shipments/{tracking_number}/tracking",
            soft_404="这张运单还没有轨迹，仓库可能还没出库。",
        )

    async def customer_shipments(self, customer_id: str) -> CallResult:
        return await self._request("GET", f"/customers/{customer_id}/shipments")

    # ---------- 写 ----------
    async def submit_claim(
        self, tracking_number: str, claim_type: str, reason: str, operator: str = ""
    ) -> CallResult:
        return await self._request(
            "POST",
            f"/shipments/{tracking_number}/claims",
            json={
                "submitted_by": operator or "assistant",
                "claim_type": claim_type,
                "reason": reason,
            },
        )

    async def change_address(
        self, tracking_number: str, new_address: str, operator: str = ""
    ) -> CallResult:
        return await self._request(
            "POST",
            f"/shipments/{tracking_number}/address-changes",
            json={"submitted_by": operator or "assistant", "new_address": new_address},
        )

    async def urge_delivery(
        self, tracking_number: str, reason: str = "用户催派送", operator: str = ""
    ) -> CallResult:
        return await self._request(
            "POST",
            f"/shipments/{tracking_number}/delivery-urges",
            json={"submitted_by": operator or "assistant", "reason": reason},
        )


commerce_client = CommerceClient()

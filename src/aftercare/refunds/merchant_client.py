"""商家 HTTP 边界；调查只使用 MerchantClient，退款执行器使用独立子类。"""

import json
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from aftercare.refunds.policy import OrderSnapshot, ShipmentSnapshot
from aftercare.runtime.steps import PayloadTooLarge, bounded_json


class MerchantError(Exception):
    def __init__(self, code: str, retryable=False):
        self.code, self.retryable = code, retryable
        super().__init__(code)


class MerchantClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def read(self, name: str, order_id: str, customer_id: str, timeout: float) -> dict:
        suffix = "/shipment" if name == "get_shipment" else ""
        if name not in {"get_order", "get_shipment"}:
            raise ValueError("只允许订单与物流查询")
        try:
            async with self.client.stream(
                "GET",
                f"/orders/{quote(order_id, safe='')}{suffix}",
                params={"customer_id": customer_id},
                timeout=timeout,
            ) as response:
                if response.status_code == 404:
                    raise MerchantError("ORDER_NOT_FOUND")
                if response.status_code == 429 or response.status_code >= 500:
                    raise MerchantError("MERCHANT_UNAVAILABLE", True)
                if response.status_code != 200:
                    raise MerchantError("MERCHANT_PROTOCOL_ERROR")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 16 * 1024:
                        raise PayloadTooLarge
            model = OrderSnapshot if name == "get_order" else ShipmentSnapshot
            value = model.model_validate_json(bytes(body))
            if name == "get_order" and (value.id != order_id or value.customer_id != customer_id):
                raise MerchantError("ORDER_NOT_FOUND")
            if name == "get_shipment" and value.order_id != order_id:
                raise MerchantError("MERCHANT_PROTOCOL_ERROR")
            return bounded_json(value.model_dump(mode="json"), 16 * 1024)
        except httpx.RequestError:
            raise MerchantError("MERCHANT_UNAVAILABLE", True) from None
        except ValidationError:
            raise MerchantError("MERCHANT_PROTOCOL_ERROR") from None


class RefundMerchantClient(MerchantClient):
    async def _refund_request(self, method, key, timeout, payload=None):
        path = f"/refunds/by-key/{quote(key, safe='')}" if method == "GET" else "/refunds"
        try:
            async with self.client.stream(
                method,
                path,
                timeout=timeout,
                json=payload,
                headers={"Idempotency-Key": key} if method == "POST" else None,
            ) as response:
                if method == "GET" and response.status_code == 404:
                    return None
                if response.status_code not in {200, 404, 409, 422}:
                    raise MerchantError("REFUND_RESULT_UNCERTAIN", True)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 16 * 1024:
                        raise MerchantError("REFUND_RESPONSE_TOO_LARGE")
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (httpx.RequestError, ValueError):
            raise MerchantError("REFUND_RESULT_UNCERTAIN", True) from None

    async def lookup_refund(self, key, timeout):
        return await self._refund_request("GET", key, timeout)

    async def post_refund(self, key, payload, timeout):
        return await self._refund_request("POST", key, timeout, payload)

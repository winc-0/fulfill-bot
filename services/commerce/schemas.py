"""接口出入参。字段名与前端约定保持一致，改这里等于改契约。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


class ApiResponse(BaseModel):
    code: int = 0
    message: str = "ok"
    data: Any = None


class ShipmentSummaryData(BaseModel):
    tracking_number: str
    goods_desc: str
    status: str
    carrier: str
    promise_time: str
    created_at: datetime


class CustomerShipmentsData(BaseModel):
    customer_id: str
    shipments: list[ShipmentSummaryData]


class ShipmentDetailData(BaseModel):
    tracking_number: str
    status: str
    status_desc: str
    carrier: str
    origin_warehouse: str
    destination: str
    goods_desc: str
    weight_kg: Decimal
    insured_amount: Decimal
    freight: Decimal
    promise_time: str
    created_at: datetime
    receiver_name: str
    receiver_phone_masked: str
    receiver_address: str


class ShipmentStatusData(BaseModel):
    tracking_number: str
    status: str
    status_desc: str
    carrier: str
    promise_time: str


class TrackingTraceData(BaseModel):
    time: datetime
    node: str
    desc: str


class TrackingData(BaseModel):
    tracking_number: str
    carrier: str
    status: str
    status_desc: str
    traces: list[TrackingTraceData]


class ClaimRequestBody(BaseModel):
    submitted_by: str = Field(default="assistant")
    claim_type: str = Field(default="破损")
    reason: str = Field(default="用户反馈包裹异常")


class AddressChangeBody(BaseModel):
    submitted_by: str = Field(default="assistant")
    new_address: str = Field(default="")


class DeliveryUrgeBody(BaseModel):
    submitted_by: str = Field(default="assistant")
    reason: str = Field(default="用户催派送")


class OperationResultData(BaseModel):
    request_type: str
    request_id: str
    tracking_number: str
    status: str
    status_desc: str

"""模拟履约中台（WMS/TMS 侧）的 HTTP 接口。

契约（与前端、客服侧工具层三方一致）：
    成功统一返回 {code: 0, message: "ok", data: ...}
    失败走 HTTP 状态码 + {"detail": "..."}，由调用方决定怎么降级。

业务规则刻意留在中台：改址截点、理赔去重、催派前置条件。
这些规则只有一处实现，避免"中台说能改、客服说不能改"这类前后端口径打架。
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from services.commerce.db import get_db
from services.commerce.models import (
    ADDRESS_CHANGE_CUTOFF,
    STATUS_FLOW,
    AddressChangeRequest,
    ClaimRequest,
    Customer,
    DeliveryUrgeRequest,
    Shipment,
)
from services.commerce.schemas import (
    AddressChangeBody,
    ApiResponse,
    ClaimRequestBody,
    CustomerShipmentsData,
    DeliveryUrgeBody,
    OperationResultData,
    ShipmentDetailData,
    ShipmentStatusData,
    ShipmentSummaryData,
    TrackingData,
    TrackingTraceData,
)

router = APIRouter()

# 没到"已出库"就不可能出异常件，也不该催派送——件还在仓里
NOT_SHIPPED = {"已下单", "仓库备货"}
# 已经结束的运单，不能再发起任何工单
CLOSED = {"已妥投"}


def _wrap(data) -> ApiResponse:
    return ApiResponse(data=data)


def _shipment_or_404(db: Session, tracking_number: str) -> Shipment:
    row = db.query(Shipment).filter(Shipment.tracking_number == tracking_number).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"没查到运单 {tracking_number}，麻烦确认一下运单号。")
    return row


def _after(db: Session, tracking_number: str) -> Shipment:
    """需要轨迹的接口统一走这里：没轨迹就是软 404。"""
    row = _shipment_or_404(db, tracking_number)
    return row


def _new_id(prefix: str) -> str:
    return f"{prefix}{datetime.now():%Y%m%d%H%M%S}{uuid4().hex[:6].upper()}"


@router.get("/health", response_model=ApiResponse, tags=["系统"], summary="健康检查")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return _wrap({"status": "ok"})


@router.get("/customers/{customer_id}/shipments", response_model=ApiResponse, tags=["客户"],
            summary="客户最近运单")
def customer_shipments(customer_id: str, db: Session = Depends(get_db)):
    customer = db.query(Customer).filter(Customer.customer_id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail=f"客户 {customer_id} 不存在。")
    rows = (
        db.query(Shipment)
        .filter(Shipment.customer_id == customer.id)
        .order_by(Shipment.created_at.desc())
        .limit(5)
        .all()
    )
    return _wrap(
        CustomerShipmentsData(
            customer_id=customer.customer_id,
            shipments=[
                ShipmentSummaryData(
                    tracking_number=r.tracking_number,
                    goods_desc=r.goods_desc,
                    status=r.status,
                    carrier=r.carrier,
                    promise_time=r.promise_time,
                    created_at=r.created_at,
                )
                for r in rows
            ],
        )
    )


@router.get("/shipments/{tracking_number}", response_model=ApiResponse, tags=["运单"], summary="运单详情")
def shipment_detail(tracking_number: str, db: Session = Depends(get_db)):
    row = _shipment_or_404(db, tracking_number)
    return _wrap(
        ShipmentDetailData(
            tracking_number=row.tracking_number,
            status=row.status,
            status_desc=row.status_desc,
            carrier=row.carrier,
            origin_warehouse=row.origin_warehouse,
            destination=row.destination,
            goods_desc=row.goods_desc,
            weight_kg=row.weight_kg,
            insured_amount=row.insured_amount,
            freight=row.freight,
            promise_time=row.promise_time,
            created_at=row.created_at,
            receiver_name=row.receiver_name,
            receiver_phone_masked=row.receiver_phone_masked,
            receiver_address=row.receiver_address,
        )
    )


@router.get("/shipments/{tracking_number}/status", response_model=ApiResponse, tags=["运单"],
            summary="运单状态")
def shipment_status(tracking_number: str, db: Session = Depends(get_db)):
    row = _shipment_or_404(db, tracking_number)
    return _wrap(
        ShipmentStatusData(
            tracking_number=row.tracking_number,
            status=row.status,
            status_desc=row.status_desc,
            carrier=row.carrier,
            promise_time=row.promise_time,
        )
    )


@router.get("/shipments/{tracking_number}/tracking", response_model=ApiResponse, tags=["运单"],
            summary="物流轨迹")
def shipment_tracking(tracking_number: str, db: Session = Depends(get_db)):
    row = _after(db, tracking_number)
    traces = sorted(row.traces, key=lambda t: t.trace_time, reverse=True)
    if not traces:
        raise HTTPException(status_code=404, detail="这张运单还没有轨迹，仓库可能还没出库。")
    return _wrap(
        TrackingData(
            tracking_number=row.tracking_number,
            carrier=row.carrier,
            status=row.status,
            status_desc=row.status_desc,
            traces=[TrackingTraceData(time=t.trace_time, node=t.node, desc=t.trace_desc) for t in traces],
        )
    )


@router.post("/shipments/{tracking_number}/claims", response_model=ApiResponse, tags=["运单"],
             summary="提交异常件理赔")
def create_claim(tracking_number: str, body: ClaimRequestBody, db: Session = Depends(get_db)):
    row = _shipment_or_404(db, tracking_number)

    if row.status in NOT_SHIPPED:
        raise HTTPException(
            status_code=400,
            detail=f"运单当前状态为“{row.status}”，还没出库，暂时不支持报异常。",
        )

    existing = (
        db.query(ClaimRequest)
        .filter(ClaimRequest.shipment_id == row.id)
        .order_by(ClaimRequest.created_at.desc())
        .first()
    )
    if existing and existing.status in {"待审核", "已受理"}:
        raise HTTPException(status_code=409, detail=f"运单 {tracking_number} 已存在进行中的理赔申请。")

    claim_id = _new_id("CLM")
    claim = ClaimRequest(
        claim_id=claim_id,
        shipment_id=row.id,
        operator=body.submitted_by,
        claim_type=body.claim_type,
        reason=body.reason,
        status="待审核",
        status_desc="理赔申请已提交，等待定责与核定赔付金额。",
        created_at=datetime.now(),
    )
    db.add(claim)
    db.commit()
    return _wrap(
        OperationResultData(
            request_type="claim",
            request_id=claim_id,
            tracking_number=row.tracking_number,
            status="submitted",
            status_desc=claim.status_desc,
        )
    )


@router.post("/shipments/{tracking_number}/address-changes", response_model=ApiResponse, tags=["运单"],
             summary="改派送地址")
def create_address_change(tracking_number: str, body: AddressChangeBody, db: Session = Depends(get_db)):
    row = _shipment_or_404(db, tracking_number)

    if row.status in CLOSED:
        raise HTTPException(status_code=400, detail="这张运单已妥投，不能再改派送地址了。")
    if STATUS_FLOW.index(row.status) >= STATUS_FLOW.index(ADDRESS_CHANGE_CUTOFF):
        raise HTTPException(
            status_code=409,
            detail=f"运单已到“{row.status}”，快递员正在派送，改址来不及了。",
        )
    if not body.new_address:
        raise HTTPException(status_code=400, detail="需要新的派送地址才能提交改址。")

    change_id = _new_id("CHG")
    change = AddressChangeRequest(
        change_id=change_id,
        shipment_id=row.id,
        operator=body.submitted_by,
        new_address=body.new_address,
        status="submitted",
        status_desc="改址申请已提交，网点会按新地址派送。",
        created_at=datetime.now(),
    )
    db.add(change)
    db.commit()
    return _wrap(
        OperationResultData(
            request_type="address_change",
            request_id=change_id,
            tracking_number=row.tracking_number,
            status="submitted",
            status_desc=change.status_desc,
        )
    )


@router.post("/shipments/{tracking_number}/delivery-urges", response_model=ApiResponse, tags=["运单"],
             summary="催派送")
def create_delivery_urge(tracking_number: str, body: DeliveryUrgeBody, db: Session = Depends(get_db)):
    row = _shipment_or_404(db, tracking_number)

    if row.status in CLOSED:
        raise HTTPException(status_code=400, detail="这张运单已妥投，不需要再催派送了。")
    if row.status in NOT_SHIPPED:
        raise HTTPException(
            status_code=400,
            detail=f"运单还在“{row.status}”，尚未出库，催派送暂时不适用。",
        )

    urge_id = _new_id("URG")
    urge = DeliveryUrgeRequest(
        urge_id=urge_id,
        shipment_id=row.id,
        operator=body.submitted_by,
        reason=body.reason,
        status="submitted",
        status_desc="催派工单已下发网点，会优先安排派送。",
        created_at=datetime.now(),
    )
    db.add(urge)
    db.commit()
    return _wrap(
        OperationResultData(
            request_type="delivery_urge",
            request_id=urge_id,
            tracking_number=row.tracking_number,
            status="submitted",
            status_desc=urge.status_desc,
        )
    )

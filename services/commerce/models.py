"""模拟履约中台（WMS/TMS 侧）的领域模型。

说明：这是一个**被调用方**，代表"已经存在的业务系统"。
客服智能体不直接碰库，只通过 HTTP 调它的接口——线上的真实形态就是这样，
把这里换成一个真实 TMS，客服侧一行代码都不用改。

领域：第三方仓配服务商的运单履约。一张运单从仓库出库，经承运商干线运输，
到末端派送，全程可查轨迹；中途可改派送地址、可催派送、出问题可报异常件理赔。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Customer(Base):
    """寄件客户（货主）。"""

    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    nickname: Mapped[str] = mapped_column(String(100))
    level: Mapped[str] = mapped_column(String(32))          # 战略 / 重点 / 普通 / 散单
    mobile_masked: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime)

    shipments: Mapped[list["Shipment"]] = relationship(back_populates="customer")


class Shipment(Base):
    """运单。"""

    __tablename__ = "shipments"

    id: Mapped[int] = mapped_column(primary_key=True)
    tracking_number: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    status: Mapped[str] = mapped_column(String(32))          # 见 STATUS_FLOW
    status_desc: Mapped[str] = mapped_column(String(255))
    carrier: Mapped[str] = mapped_column(String(64))         # 承运商
    origin_warehouse: Mapped[str] = mapped_column(String(64))
    destination: Mapped[str] = mapped_column(String(255))
    goods_desc: Mapped[str] = mapped_column(String(255))     # 托寄物
    weight_kg: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    insured_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))   # 保价金额，0 表示未保价
    freight: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    promise_time: Mapped[str] = mapped_column(String(64))    # 承诺时效，如"次日 18:00 前"
    created_at: Mapped[datetime] = mapped_column(DateTime)
    receiver_name: Mapped[str] = mapped_column(String(64))
    receiver_phone_masked: Mapped[str] = mapped_column(String(32))
    receiver_address: Mapped[str] = mapped_column(String(255))
    extra_json: Mapped[dict] = mapped_column(JSON, default=dict)

    customer: Mapped[Customer] = relationship(back_populates="shipments")
    traces: Mapped[list["TrackingTrace"]] = relationship(back_populates="shipment")
    claim_requests: Mapped[list["ClaimRequest"]] = relationship(back_populates="shipment")
    address_changes: Mapped[list["AddressChangeRequest"]] = relationship(back_populates="shipment")
    delivery_urges: Mapped[list["DeliveryUrgeRequest"]] = relationship(back_populates="shipment")


class TrackingTrace(Base):
    """物流轨迹节点。"""

    __tablename__ = "tracking_traces"

    id: Mapped[int] = mapped_column(primary_key=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey("shipments.id"))
    trace_time: Mapped[datetime] = mapped_column(DateTime)
    node: Mapped[str] = mapped_column(String(64))            # 如"华北1仓""北京分拨中心"
    trace_desc: Mapped[str] = mapped_column(String(255))

    shipment: Mapped[Shipment] = relationship(back_populates="traces")


class ClaimRequest(Base):
    """异常件理赔申请（破损 / 丢失 / 延误）。"""

    __tablename__ = "claim_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    claim_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey("shipments.id"))
    operator: Mapped[str] = mapped_column(String(64))
    claim_type: Mapped[str] = mapped_column(String(32))      # 破损 / 丢失 / 延误
    reason: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32))          # 待审核 / 已受理 / 已驳回
    status_desc: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime)

    shipment: Mapped[Shipment] = relationship(back_populates="claim_requests")


class AddressChangeRequest(Base):
    """改派送地址申请。"""

    __tablename__ = "address_change_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    change_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey("shipments.id"))
    operator: Mapped[str] = mapped_column(String(64))
    new_address: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32))
    status_desc: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime)

    shipment: Mapped[Shipment] = relationship(back_populates="address_changes")


class DeliveryUrgeRequest(Base):
    """催派送工单。"""

    __tablename__ = "delivery_urge_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    urge_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey("shipments.id"))
    operator: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32))
    status_desc: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime)

    shipment: Mapped[Shipment] = relationship(back_populates="delivery_urges")


# 运单状态机：顺序即正常流转顺序，用于生成轨迹与判断可否改址/拦截
STATUS_FLOW = [
    "已下单",
    "仓库备货",
    "已出库",
    "干线运输中",
    "到达派送网点",
    "派送中",
    "已妥投",
]

# 已到这个状态（含）之后不允许再改派送地址——件已经在派送路上了
ADDRESS_CHANGE_CUTOFF = "派送中"

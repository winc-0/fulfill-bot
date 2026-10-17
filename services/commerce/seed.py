"""造一批可复现的仓配履约演示数据。

刻意设计成"每种会话场景都有对应数据"：
- 仓库备货中的运单 → 还没有轨迹，/tracking 走软 404，用来演示降级
- 干线运输中的运单 → 有完整轨迹可念，可催派送、可改派送地址
- 派送中的运单 → 改址会被拒（已过截点 409），用来演示"业务失败怎么说人话"
- 已妥投的运单 → 可报异常件理赔
- 已有一笔理赔在审核的运单 → 再次提交会 409，同样用于演示业务失败

数据全部写死而非随机：评测脚本要断言具体运单号的行为，随机会让结论不可复现。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from services.commerce.db import SessionLocal, init_schema
from services.commerce.models import (
    ClaimRequest,
    Customer,
    Shipment,
    TrackingTrace,
)

CUSTOMERS = [
    ("c1001", "李先生", "战略客户", "138****1234"),
    ("c1002", "王女士", "重点客户", "139****5678"),
    ("c1003", "张先生", "普通客户", "137****2468"),
]

# (tracking_number, customer_id, status, status_desc, carrier, origin_warehouse,
#  destination, goods_desc, weight, insured, freight, promise, created_at,
#  receiver, phone, address)
SHIPMENTS = [
    ("YD20260410001", "c1001", "干线运输中", "已离开华北1仓，发往北京分拨中心。", "顺达速运", "华北1仓",
     "北京市朝阳区望京 SOHO", "3C 数码配件", Decimal("2.40"), Decimal("5000.00"), Decimal("18.00"),
     "次日 18:00 前", datetime(2026, 4, 10, 10, 0, 0), "李然", "138****1234",
     "北京市朝阳区望京 SOHO T1 座 15 层"),

    ("YD20260408002", "c1001", "派送中", "快递员已取件，正在派送途中。", "顺达速运", "华北1仓",
     "北京市海淀区中关村南大街", "办公用品", Decimal("6.80"), Decimal("0.00"), Decimal("12.00"),
     "当日 20:00 前", datetime(2026, 4, 8, 15, 30, 0), "李然", "138****1234",
     "北京市海淀区中关村南大街 5 号"),

    ("YD20260405003", "c1001", "已妥投", "本人签收，签收时间 4 月 6 日 14:12。", "安捷物流", "华北2仓",
     "北京市丰台区南三环西路", "小家电", Decimal("12.50"), Decimal("800.00"), Decimal("26.00"),
     "隔日 18:00 前", datetime(2026, 4, 5, 11, 20, 0), "李然", "138****1234",
     "北京市丰台区南三环西路 16 号院"),

    ("YD20260403004", "c1002", "已妥投", "前台代收，签收时间 4 月 4 日 09:40。", "安捷物流", "华东3仓",
     "上海市浦东新区世纪大道", "服饰箱包", Decimal("3.20"), Decimal("1200.00"), Decimal("15.00"),
     "隔日 18:00 前", datetime(2026, 4, 3, 9, 0, 0), "王倩", "139****5678",
     "上海市浦东新区世纪大道 100 号"),

    ("YD20260401005", "c1002", "仓库备货", "仓库正在拣货打包，预计今日出库。", "安捷物流", "华东3仓",
     "上海市徐汇区漕溪北路", "美妆个护", Decimal("1.10"), Decimal("0.00"), Decimal("10.00"),
     "隔日 18:00 前", datetime(2026, 4, 1, 16, 45, 0), "王倩", "139****5678",
     "上海市徐汇区漕溪北路 88 号"),

    ("YD20260328006", "c1003", "到达派送网点", "包裹滞留在网点，正在安排二次派送。", "顺达速运", "华南1仓",
     "广州市天河区珠江新城", "生鲜冷链", Decimal("9.60"), Decimal("300.00"), Decimal("42.00"),
     "次日 12:00 前", datetime(2026, 3, 28, 8, 10, 0), "张伟", "137****2468",
     "广州市天河区珠江新城华夏路 10 号"),
]

# tracking_number -> [(trace_time, node, desc), ...]  由新到旧，接口按此顺序返回
TRACES = {
    "YD20260410001": [
        (datetime(2026, 4, 10, 20, 15), "华北1仓", "已装车发出，下一站北京分拨中心"),
        (datetime(2026, 4, 10, 18, 40), "华北1仓", "已完成分拣并出库"),
        (datetime(2026, 4, 10, 11, 5), "华北1仓", "仓库已接单，开始拣货"),
    ],
    "YD20260408002": [
        (datetime(2026, 4, 9, 9, 20), "中关村网点", "快递员 王师傅 已取件，正在派送"),
        (datetime(2026, 4, 9, 7, 50), "北京分拨中心", "已到达派送网点"),
        (datetime(2026, 4, 8, 22, 10), "北京分拨中心", "已到达分拨中心"),
        (datetime(2026, 4, 8, 16, 30), "华北1仓", "已出库"),
    ],
    "YD20260405003": [
        (datetime(2026, 4, 6, 14, 12), "丰台网点", "已妥投，本人签收"),
        (datetime(2026, 4, 6, 8, 30), "丰台网点", "快递员 赵师傅 正在派送"),
        (datetime(2026, 4, 5, 19, 0), "北京分拨中心", "已到达派送网点"),
        (datetime(2026, 4, 5, 12, 40), "华北2仓", "已出库"),
    ],
    "YD20260403004": [
        (datetime(2026, 4, 4, 9, 40), "浦东网点", "已妥投，前台代收"),
        (datetime(2026, 4, 3, 21, 15), "上海分拨中心", "已到达派送网点"),
        (datetime(2026, 4, 3, 10, 30), "华东3仓", "已出库"),
    ],
    "YD20260328006": [
        (datetime(2026, 3, 29, 18, 20), "珠江新城网点", "派送未成功，收件人不在，安排次日再派"),
        (datetime(2026, 3, 29, 10, 5), "珠江新城网点", "快递员 陈师傅 正在派送"),
        (datetime(2026, 3, 28, 22, 30), "广州分拨中心", "已到达派送网点"),
        (datetime(2026, 3, 28, 9, 0), "华南1仓", "已出库"),
    ],
}

# 预置一笔审核中的理赔单：YD20260403004 再次提交理赔会 409
SEED_CLAIMS = [
    ("CLM20260404001", "YD20260403004", "破损", "外箱破损，内件刮伤", "待审核",
     "客服已受理，等待定责结果。", datetime(2026, 4, 4, 10, 20, 0)),
]


def seed(session: Session | None = None) -> None:
    """写入种子数据。已存在运单则整批跳过，重复执行安全。"""

    def _run(s: Session) -> None:
        if s.query(Shipment).count() > 0:
            return

        customers: dict[str, Customer] = {}
        for cid, name, level, mobile in CUSTOMERS:
            row = Customer(
                customer_id=cid,
                nickname=name,
                level=level,
                mobile_masked=mobile,
                created_at=datetime(2026, 1, 5, 9, 0, 0),
            )
            s.add(row)
            customers[cid] = row
        s.flush()

        shipments: dict[str, Shipment] = {}
        for (
            tn, cid, status, desc, carrier, warehouse, dest, goods,
            weight, insured, freight, promise, created, receiver, phone, address,
        ) in SHIPMENTS:
            row = Shipment(
                tracking_number=tn,
                customer_id=customers[cid].id,
                status=status,
                status_desc=desc,
                carrier=carrier,
                origin_warehouse=warehouse,
                destination=dest,
                goods_desc=goods,
                weight_kg=weight,
                insured_amount=insured,
                freight=freight,
                promise_time=promise,
                created_at=created,
                receiver_name=receiver,
                receiver_phone_masked=phone,
                receiver_address=address,
            )
            s.add(row)
            shipments[tn] = row
        s.flush()

        for tn, rows in TRACES.items():
            shipment = shipments.get(tn)
            if not shipment:
                continue
            for t_time, node, t_desc in rows:
                s.add(
                    TrackingTrace(
                        shipment_id=shipment.id,
                        trace_time=t_time,
                        node=node,
                        trace_desc=t_desc,
                    )
                )

        for claim_id, tn, ctype, reason, status, desc, created in SEED_CLAIMS:
            shipment = shipments.get(tn)
            if not shipment:
                continue
            s.add(
                ClaimRequest(
                    claim_id=claim_id,
                    shipment_id=shipment.id,
                    operator="seed",
                    claim_type=ctype,
                    reason=reason,
                    status=status,
                    status_desc=desc,
                    created_at=created,
                )
            )

        s.commit()

    if session is not None:
        _run(session)
        return

    init_schema()
    with SessionLocal() as s:
        _run(s)


def reset() -> None:
    """清空重建。演示前想要干净状态就调它。"""
    init_schema(drop_all=True)
    seed()


__all__ = ["seed", "reset"]

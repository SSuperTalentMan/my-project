"""示例数据生成与导入（架构文档 §14.3「Agent 自动生成」兜底方案）。

生成规则刻意做得**有业务含义**，而不是纯随机：
- 平台占比不均（抖店 > 淘宝 > 京东 > 拼多多 > 天猫）；
- 部分商品被赋予「高退货倾向」（模拟质量/描述不符问题），
  这样「退货率最高的商品」这类问题才有真实可解释的答案；
- 周末订单量高于工作日、大促日（每月 15 日）有尖峰；
- 库存与销量挂钩，制造少量低于安全库存的 SKU（缺货预警场景）。

输出两路：
1. ``data/sample_*.csv`` —— 与 §13 目录结构一致，便于人工查看或换库导入；
2. MySQL 表 —— 幂等导入（``--reset`` 会先清空再灌）。

若已下载 Olist 等公开数据集，可用 ``load_csv_dir()`` 直接导入其 CSV。
"""

from __future__ import annotations

import csv
import datetime as dt
import random
from pathlib import Path
from typing import Any, Dict, List, Sequence

from commercepivot.core.config import DATA_DIR
from commercepivot.core.logging import get_logger

log = get_logger("commercepivot.db.seed")

# ------------------------------------------------------------------ 基础字典
PLATFORMS = [("抖店", 0.35), ("淘宝", 0.25), ("京东", 0.20), ("拼多多", 0.12), ("天猫", 0.08)]
REGIONS = ["广东", "浙江", "江苏", "山东", "河南", "四川", "北京", "上海", "湖北", "福建"]
WAREHOUSES = ["杭州仓", "广州仓", "天津仓"]
AD_PLATFORMS = ["巨量引擎", "京东京准", "阿里万相台", "多多推广"]

# (商品名, 类目, 价格, 主要平台, 退货倾向 0~1, 日均销量权重)
PRODUCT_CATALOG: List[tuple[str, str, float, str, float, float]] = [
    ("云感记忆枕", "家居", 149.00, "抖店", 0.26, 3.2),
    ("全棉四件套 1.8m", "家居", 399.00, "天猫", 0.12, 1.8),
    ("北欧简约沙发毯", "家居", 129.00, "淘宝", 0.09, 2.1),
    ("折叠晾衣架", "家居", 69.00, "拼多多", 0.07, 2.8),
    ("硅藻泥地垫", "家居", 59.00, "拼多多", 0.18, 2.4),
    ("香薰蜡烛礼盒", "家居", 89.00, "抖店", 0.06, 1.6),
    ("纯棉基础T恤", "服饰", 79.00, "淘宝", 0.19, 4.2),
    ("轻薄羽绒服", "服饰", 499.00, "天猫", 0.15, 1.4),
    ("高腰阔腿牛仔裤", "服饰", 199.00, "抖店", 0.22, 2.6),
    ("运动速干短裤", "服饰", 99.00, "京东", 0.08, 1.9),
    ("羊毛混纺针织衫", "服饰", 259.00, "天猫", 0.11, 1.3),
    ("无线蓝牙耳机 Pro", "数码", 299.00, "京东", 0.13, 2.2),
    ("65W 氮化镓充电器", "数码", 129.00, "京东", 0.05, 2.9),
    ("1080P 高清摄像头", "数码", 219.00, "淘宝", 0.17, 1.2),
    ("便携蓝牙音箱", "数码", 179.00, "抖店", 0.14, 1.7),
    ("智能手环 S6", "数码", 249.00, "拼多多", 0.20, 2.0),
    ("机械键盘 87 键", "数码", 359.00, "京东", 0.09, 1.1),
    ("玻尿酸保湿面霜", "美妆", 159.00, "天猫", 0.10, 2.3),
    ("氨基酸洁面乳", "美妆", 69.00, "抖店", 0.08, 3.1),
    ("防晒喷雾 SPF50", "美妆", 89.00, "淘宝", 0.21, 2.5),
    ("修护精华液", "美妆", 299.00, "天猫", 0.07, 1.5),
    ("每日坚果 750g", "食品", 79.90, "拼多多", 0.06, 3.4),
    ("手冲咖啡豆 500g", "食品", 118.00, "淘宝", 0.09, 1.8),
    ("低糖燕麦饼干", "食品", 39.90, "京东", 0.05, 2.7),
    ("阳澄湖大闸蟹礼券", "食品", 588.00, "天猫", 0.24, 0.6),
    ("婴儿纸尿裤 L 码", "母婴", 129.00, "京东", 0.11, 2.6),
    ("宝宝辅食机", "母婴", 269.00, "天猫", 0.16, 0.9),
    ("儿童保温水杯", "母婴", 89.00, "拼多多", 0.13, 1.9),
    ("瑜伽垫加厚款", "运动", 129.00, "抖店", 0.12, 2.2),
    ("可调节哑铃 20kg", "运动", 399.00, "京东", 0.10, 0.8),
    ("户外登山双肩包", "运动", 329.00, "淘宝", 0.14, 1.1),
    ("折叠露营椅", "户外", 159.00, "拼多多", 0.17, 1.6),
    ("便携帐篷 3-4 人", "户外", 499.00, "淘宝", 0.23, 0.7),
    ("速干防晒衣 UPF50+", "户外", 139.00, "抖店", 0.09, 2.0),
    ("智能马桶盖", "家电", 899.00, "京东", 0.15, 0.5),
    ("桌面空气循环扇", "家电", 229.00, "淘宝", 0.10, 1.4),
]

ORDER_STATUS = [("已完成", 0.60), ("已发货", 0.15), ("已付款", 0.12), ("已退款", 0.08), ("已取消", 0.05)]
AFTER_SALE_STATUS = [("已完成", 0.55), ("处理中", 0.25), ("待审核", 0.13), ("已拒绝", 0.07)]


def _weighted(rng: random.Random, pairs: Sequence[tuple[Any, float]]) -> Any:
    total = sum(w for _, w in pairs)
    r = rng.random() * total
    acc = 0.0
    for value, weight in pairs:
        acc += weight
        if r <= acc:
            return value
    return pairs[-1][0]


def generate(
    days: int = 166,
    end_date: dt.date | None = None,
    base_orders_per_day: int = 40,
    seed: int = 20260912,
) -> Dict[str, List[Dict[str, Any]]]:
    """生成全量示例数据。返回各表的行列表。"""
    rng = random.Random(seed)
    end = end_date or dt.date(2026, 9, 13)
    start = end - dt.timedelta(days=days - 1)

    products: List[Dict[str, Any]] = []
    for idx, (name, category, price, main_platform, _, _) in enumerate(PRODUCT_CATALOG, start=1):
        products.append(
            {
                "id": idx,
                "name": name,
                "category": category,
                "price": round(price, 2),
                "platform": main_platform,
                "created_at": f"{start.isoformat()} 10:00:00",
            }
        )

    # 买家池：不要每单随机生成一个新 ID。
    # 9 万 ID 空间随机取样 n 次的期望碰撞只有 n²/(2·90000) 量级 —— 1400 单也才
    # 十来个重复买家，于是「买家数」几乎恒等于「订单量」，按平台分组的表里
    # 这两列数字一模一样，看着像指标算错（实际 SQL 是对的）。
    # 改成从固定买家池按长尾权重抽样，让复购率与老客占比接近真实电商。
    buyer_pool_size = max(60, int(days * base_orders_per_day * 0.35))
    buyer_pool = [f"U{10000 + i}" for i in range(buyer_pool_size)]
    buyer_weights = [1.0 / (i + 1) ** 0.55 for i in range(buyer_pool_size)]

    orders: List[Dict[str, Any]] = []
    order_items: List[Dict[str, Any]] = []
    after_sales: List[Dict[str, Any]] = []
    seq = 0
    order_id = 0
    after_sale_id = 0

    for day_offset in range(days):
        day = start + dt.timedelta(days=day_offset)
        # 周末 +30%，每月 15 日大促 +120%
        daily = base_orders_per_day
        if day.weekday() >= 5:
            daily = int(daily * 1.3)
        if day.day == 15:
            daily = int(daily * 2.2)
        daily = max(3, int(rng.gauss(daily, daily * 0.18)))

        for _ in range(daily):
            seq += 1
            order_id += 1
            product = rng.choice(products)
            platform = _weighted(rng, PLATFORMS)
            # 商品主平台权重更高，模拟「同款多平台铺货」
            if rng.random() < 0.45:
                platform = product["platform"]
            quantity = rng.choices([1, 2, 3, 4], weights=[70, 18, 8, 4])[0]
            unit_price = round(product["price"] * rng.uniform(0.88, 1.0), 2)
            amount = round(unit_price * quantity, 2)
            status = _weighted(rng, ORDER_STATUS)
            hour = rng.choice([9, 10, 11, 13, 14, 15, 16, 18, 19, 20, 21])
            minute = rng.randint(0, 59)
            order_date = f"{day.isoformat()} {hour:02d}:{minute:02d}:{rng.randint(0, 59):02d}"
            region = rng.choice(REGIONS)

            orders.append(
                {
                    "id": order_id,
                    "order_no": f"SO{day.strftime('%Y%m%d')}{seq:06d}",
                    "user_id": rng.choices(buyer_pool, weights=buyer_weights)[0],
                    "product_id": product["id"],
                    "amount": amount,
                    "status": status,
                    "platform": platform,
                    "order_date": order_date,
                    "region": region,
                }
            )
            order_items.append(
                {
                    "order_id": order_id,
                    "product_id": product["id"],
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "subtotal": amount,
                }
            )

            # ---- 售后：以商品自身的退货倾向为概率，叠加平台差异
            propensity = PRODUCT_CATALOG[product["id"] - 1][4]
            if platform == "拼多多":
                propensity *= 1.15
            if platform == "天猫":
                propensity *= 0.85
            if status in ("已退款", "已完成") and rng.random() < propensity:
                after_sale_id += 1
                type_ = rng.choices(["退货", "换货", "仅退款"], weights=[70, 16, 14])[0]
                created = day + dt.timedelta(days=rng.randint(1, 12))
                if created > end:
                    created = end
                refund = round(amount * (1.0 if type_ == "退货" else rng.uniform(0.3, 1.0)), 2)
                after_sales.append(
                    {
                        "id": after_sale_id,
                        "order_id": order_id,
                        "type": type_,
                        "status": _weighted(rng, AFTER_SALE_STATUS),
                        "refund_amount": refund,
                        "created_at": f"{created.isoformat()} {rng.randint(9, 21):02d}:{rng.randint(0, 59):02d}:00",
                    }
                )

    # ---- 库存：按近期销量反推，制造少量预警 SKU
    inventory: List[Dict[str, Any]] = []
    sold_by_product: Dict[int, int] = {}
    for item in order_items:
        sold_by_product[item["product_id"]] = sold_by_product.get(item["product_id"], 0) + item["quantity"]

    inv_id = 0
    for product in products:
        sold = sold_by_product.get(product["id"], 0)
        daily = max(0.5, sold / days)
        for warehouse in WAREHOUSES:
            inv_id += 1
            safety = int(max(10, daily * rng.uniform(5, 9)))
            if rng.random() < 0.14:
                quantity = int(safety * rng.uniform(0.15, 0.9))  # 预警
            else:
                quantity = int(safety * rng.uniform(1.2, 4.5))
            inventory.append(
                {
                    "id": inv_id,
                    "sku": f"CP-{product['category'][:2]}-{product['id']:03d}-{warehouse[0]}",
                    "product_id": product["id"],
                    "warehouse": warehouse,
                    "quantity": quantity,
                    "safety_stock": safety,
                    "updated_at": f"{end.isoformat()} 08:30:00",
                }
            )

    # ---- 广告：按平台 + 日粒度
    ad_reports: List[Dict[str, Any]] = []
    ad_roi_base = {"巨量引擎": 2.6, "京东京准": 3.4, "阿里万相台": 2.9, "多多推广": 1.8}
    for day_offset in range(days):
        day = start + dt.timedelta(days=day_offset)
        for platform in AD_PLATFORMS:
            spend = round(rng.uniform(1800, 5200) * (1.6 if day.day == 15 else 1.0), 2)
            roi = max(0.4, rng.gauss(ad_roi_base[platform], 0.45))
            impressions = int(spend * rng.uniform(28, 46))
            clicks = int(impressions * rng.uniform(0.012, 0.032))
            ad_reports.append(
                {
                    "platform": platform,
                    "report_date": day.isoformat(),
                    "impressions": impressions,
                    "clicks": clicks,
                    "cost": spend,
                    "revenue": round(spend * roi, 2),
                }
            )

    return {
        "products": products,
        "orders": orders,
        "order_items": order_items,
        "after_sales": after_sales,
        "inventory": inventory,
        "ad_reports": ad_reports,
    }


# ------------------------------------------------------------------ CSV 输出
CSV_FILES = {
    "products": "sample_products.csv",
    "orders": "sample_orders.csv",
    "order_items": "sample_order_items.csv",
    "after_sales": "sample_after_sales.csv",
    "inventory": "sample_inventory.csv",
    "ad_reports": "sample_ad_reports.csv",
}


def write_csv(data: Dict[str, List[Dict[str, Any]]], outdir: Path | None = None) -> List[str]:
    outdir = outdir or DATA_DIR
    outdir.mkdir(parents=True, exist_ok=True)
    written: List[str] = []
    for table, filename in CSV_FILES.items():
        rows = data.get(table) or []
        path = outdir / filename
        if not rows:
            continue
        with open(path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        written.append(str(path))
        log.info("CSV 已写出", file=str(path), rows=len(rows))
    return written


# ------------------------------------------------------------------ 导入 MySQL
_TABLES_ORDER = ("products", "orders", "order_items", "after_sales", "inventory", "ad_reports")

_INSERT_SQL = {
    "products": "INSERT INTO products (id, name, category, price, platform, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
    "orders": (
        "INSERT INTO orders (id, order_no, user_id, product_id, amount, status, platform, order_date, region) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
    ),
    "order_items": "INSERT INTO order_items (order_id, product_id, quantity, unit_price, subtotal) VALUES (%s,%s,%s,%s,%s)",
    "after_sales": "INSERT INTO after_sales (id, order_id, type, status, refund_amount, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
    "inventory": "INSERT INTO inventory (id, sku, product_id, warehouse, quantity, safety_stock, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
    "ad_reports": "INSERT INTO ad_reports (platform, report_date, impressions, clicks, cost, revenue) VALUES (%s,%s,%s,%s,%s,%s)",
}

_COLUMNS = {
    "products": ("id", "name", "category", "price", "platform", "created_at"),
    "orders": ("id", "order_no", "user_id", "product_id", "amount", "status", "platform", "order_date", "region"),
    "order_items": ("order_id", "product_id", "quantity", "unit_price", "subtotal"),
    "after_sales": ("id", "order_id", "type", "status", "refund_amount", "created_at"),
    "inventory": ("id", "sku", "product_id", "warehouse", "quantity", "safety_stock", "updated_at"),
    "ad_reports": ("platform", "report_date", "impressions", "clicks", "cost", "revenue"),
}


def reset_tables() -> None:
    from commercepivot.db.mysql import get_mysql

    pool = get_mysql()
    with pool.acquire() as conn:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            for table in reversed(_TABLES_ORDER):
                cur.execute(f"TRUNCATE TABLE {table}")
            for extra in ("tickets",):
                try:
                    cur.execute(f"TRUNCATE TABLE {extra}")
                except Exception:  # noqa: BLE001
                    pass
            cur.execute("SET FOREIGN_KEY_CHECKS = 1")
    log.info("业务表已清空", tables=list(_TABLES_ORDER))


def load_into_mysql(data: Dict[str, List[Dict[str, Any]]], batch: int = 1000) -> Dict[str, int]:
    from commercepivot.db.mysql import get_mysql

    pool = get_mysql()
    counts: Dict[str, int] = {}
    for table in _TABLES_ORDER:
        rows = data.get(table) or []
        if not rows:
            counts[table] = 0
            continue
        cols = _COLUMNS[table]
        payload = [tuple(row.get(c) for c in cols) for row in rows]
        total = 0
        for i in range(0, len(payload), batch):
            total += pool.execute_many(_INSERT_SQL[table], payload[i : i + batch])
        counts[table] = len(payload)
        log.info("表已导入", table=table, rows=len(payload))
    return counts


def load_csv_dir(directory: Path | str) -> Dict[str, int]:
    """导入外部数据集（如 Olist）的 CSV 目录。

    目录内需包含 ``sample_orders.csv`` 等与 ``CSV_FILES`` 同名的文件，
    或直接放 ``olist_orders_dataset.csv`` 等原文件 —— 后者需要自行改列名，
    本函数只做「列名对齐后批量导入」，不做语义映射。
    """
    directory = Path(directory)
    mapping = {v: k for k, v in CSV_FILES.items()}
    data: Dict[str, List[Dict[str, Any]]] = {}
    for path in sorted(directory.glob("*.csv")):
        table = mapping.get(path.name)
        if not table:
            continue
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            data[table] = list(csv.DictReader(fh))
        log.info("读取 CSV", file=path.name, table=table, rows=len(data[table]))
    if not data:
        raise FileNotFoundError(
            f"{directory} 下没有可识别的 CSV（需要 {list(CSV_FILES.values())}）"
        )
    return load_into_mysql(data)


__all__ = [
    "AD_PLATFORMS",
    "CSV_FILES",
    "PLATFORMS",
    "PRODUCT_CATALOG",
    "REGIONS",
    "WAREHOUSES",
    "generate",
    "load_csv_dir",
    "load_into_mysql",
    "reset_tables",
    "write_csv",
]

"""退收冲销的纯规则。

口径（固定，不随页面或调用方切换）：
- 仓批扣回按“原车次入库时账面干物质”扣，即原磅单对应的净重/净粮/干物质/折标/金额；
- 农户责任按原应付凭证整单红冲，净额为零；
- 复检时仓批水分/杂质与入库账面不一致形成的差额，不摊到任何其他车次，
  单列为“仓批差额”，由该仓批（仓储环节）承担并留待仓批损耗复核。

这样仓批的干物质恒等式在扣回前后不变（扣回项就是当初入库项），
农户责任恰好等于已开凭证，差额有明确归属且不会污染同仓其他车次。
"""
from decimal import Decimal as D, localcontext
from app.errors import DomainError

DRAFT = "草稿"
POSTED = "已生效"
FAILED = "冲销失败"
CALIBER = "按原车次干物质扣回"
DIFF_REASON = "复检水分/杂质差（退收时复测）"
QTY_FIELDS = ("net_kg", "clean_kg", "dry_kg", "standard_kg", "amount")
PRECISION = 60


def dec(value):
    return D(str(value))


def show(value):
    """零值不显示指数形式（0E-25），保留到分或原始精度的格式化结果。"""
    if value != 0:
        return str(value)
    return str(value) if value.as_tuple().exponent >= -2 else "0"


def precise(func):
    """折标重是无理小数，累加与相减必须在比存储精度更高的上下文里做，避免尾差。"""
    def wrapped(*args, **kwargs):
        with localcontext() as ctx:
            ctx.prec = PRECISION
            return func(*args, **kwargs)
    return wrapped


@precise
def total(rows, field):
    return sum((dec(row.get(field, 0)) for row in rows), D(0))


def physical_dry(return_net_kg, return_impurity, return_moisture):
    """退收时按仓批复测水杂折算的物理干物质。只用于计量差额，不用于农户结算。"""
    net, impurity, moisture = dec(return_net_kg), dec(return_impurity), dec(return_moisture)
    if net <= 0:
        raise DomainError("RANGE", "退收净重必须大于零")
    if not 0 <= impurity < 100 or not 0 <= moisture < 100:
        raise DomainError("RANGE", "退收水分或杂质超出范围")
    clean = net * (1 - impurity / D(100))
    return clean * (1 - moisture / D(100))


def variance(book_dry_kg, return_net_kg, return_impurity, return_moisture, bin_name):
    """复测物理干物质与账面干物质之差。归属固定为该仓批。"""
    physical = physical_dry(return_net_kg, return_impurity, return_moisture)
    diff = physical - dec(book_dry_kg)
    return {
        "caliber": CALIBER,
        "physical_dry_kg": str(physical),
        "diff_kg": str(diff),
        "diff_owner": f"仓批 {bin_name}",
        "diff_reason": DIFF_REASON,
    }


def red_voucher(original, return_id):
    """红字凭证：逐项取反原凭证，金额按原已开凭证的分币金额整单冲回。"""
    return {
        "voucher_no": f"RV-{return_id}",
        "kind": "红字",
        "run_id": original["run_id"],
        "truck_id": original["truck_id"],
        "bin": original["bin"],
        "amount": str(-dec(original["amount"])),
        "net_kg": str(-dec(original["net_kg"])),
        "clean_kg": str(-dec(original["clean_kg"])),
        "dry_kg": str(-dec(original["dry_kg"])),
        "standard_kg": str(-dec(original["standard_kg"])),
        "moisture": original.get("moisture", ""),
        "impurity": original.get("impurity", ""),
        "price": original.get("price", ""),
        "reverses": original["voucher_no"],
        "return_id": return_id,
    }


def reconcile(original, red):
    """红冲必须与原凭证逐项对平，金额净额精确为零。"""
    net = {field: dec(original[field]) + dec(red[field]) for field in QTY_FIELDS}
    ok = all(value == 0 for value in net.values())
    if not ok:
        raise DomainError("REVERSAL", "红冲金额或数量与原凭证未对平")
    return {
        "status": "已对平",
        "currency": "CNY",
        "original_amount": show(dec(original["amount"]).quantize(D(".01"))),
        "reversal_amount": show(dec(red["amount"]).quantize(D(".01"))),
        "net_amount": show(dec(net["amount"]).quantize(D(".01"))),
        "net_standard_kg": show(net["standard_kg"]),
        "net_dry_kg": show(net["dry_kg"]),
        "net_net_kg": show(net["net_kg"]),
        "net_clean_kg": show(net["clean_kg"]),
    }


@precise
def bin_after(current, book):
    """仓批新版本 = 当前版本 - 原车次账面入库项。逐项精确相减。"""
    result = {field: dec(current[field]) - dec(book[field]) for field in QTY_FIELDS}
    if any(value < 0 for value in result.values()):
        raise DomainError("STATE", "仓批余额不足以扣回该车次，拒绝扣减")
    result["count"] = int(current["count"]) - 1
    return result

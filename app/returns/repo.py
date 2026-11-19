"""退收单、仓批版本、凭证的持久化。

三条硬约束：
1. 草稿只在 returns 表，不写仓批版本；扣减只发生在 post 的单个事务内。
2. return_effects 以 return_id 为主键，重复提交直接返回既有结果，不重复扣减。
3. post 在同一事务里写仓批新版本与红字凭证；任一步失败整体回滚，
   旧版仓批和原凭证保持原样，退收单标记为“冲销失败”。

仓批按 (run_id, bin) 界定，退收只影响原批次内的那一仓，不会串到别的批次。
"""
import hashlib
import json
from datetime import datetime, timezone
from app import store
from app.errors import DomainError
from app.returns import rules


def _now():
    return datetime.now(timezone.utc).isoformat()


def _db():
    db = store.connection()
    store.ensure_schema(db)
    return db


def return_id_for(run_id, truck_id, net_kg, moisture, impurity):
    """退收单号由原批次、原车次与复测数据派生；数值先归一，1000.0 与 1000 视为同单。"""
    parts = (run_id, truck_id) + tuple(format(rules.dec(v).normalize(), "f")
                                       for v in (net_kg, moisture, impurity))
    key = "|".join(str(v) for v in parts)
    return "RT-" + hashlib.sha256(key.encode()).hexdigest()[:16]


def run_records(run):
    return {row["id"]: row for row in run.get("records", [])}


def original_voucher(run, truck_id):
    """原应付凭证：取自已采信磅单与结算分币结果，一经登记不再改写。"""
    row = run_records(run).get(truck_id)
    if row is None:
        raise DomainError("ORPHAN", f"原车次 {truck_id} 不在该批次采信记录中", 404)
    missing = [field for field in ("amount", "net_kg", "clean_kg", "dry_kg", "standard_kg")
               if field not in row]
    if missing:
        # 宁可拒绝，也不把缺失字段当 0 冲销，否则仓批会少扣。
        raise DomainError("STATE", f"该批次缺少结算字段 {','.join(missing)}，无法开具应付凭证")
    return {
        "voucher_no": f"PV-{run['manifest']['run_id'][:8]}-{truck_id}",
        "kind": "原凭证",
        "run_id": run["manifest"]["run_id"],
        "truck_id": truck_id,
        "bin": row["bin"],
        "amount": str(row["amount"]),
        "net_kg": str(row["net_kg"]),
        "clean_kg": str(row["clean_kg"]),
        "dry_kg": str(row["dry_kg"]),
        "standard_kg": str(row["standard_kg"]),
        "moisture": str(row.get("moisture", "")),
        "impurity": str(row.get("impurity", "")),
        "price": str(run.get("rules", {}).get("price", "")),
    }


def book_bin(run, bin_name):
    """入库时的仓批基线（版本 1），由批次采信记录直接汇总。"""
    rows = [r for r in run.get("records", []) if r.get("bin") == bin_name]
    if not rows:
        raise DomainError("ORPHAN", f"仓批 {bin_name} 不在该批次采信记录中", 404)
    return {
        "run_id": run["manifest"]["run_id"], "bin": bin_name, "version": 1,
        "source": "入库", "return_id": None, "count": len(rows),
        "net_kg": str(rules.total(rows, "net_kg")),
        "clean_kg": str(rules.total(rows, "clean_kg")),
        "dry_kg": str(rules.total(rows, "dry_kg")),
        "standard_kg": str(rules.total(rows, "standard_kg")),
        "amount": str(rules.total(rows, "amount")),
    }


def latest_bin(db, run, bin_name, persist=False):
    row = db.execute(
        "SELECT payload FROM bin_versions WHERE run_id=? AND bin=? ORDER BY version DESC LIMIT 1",
        (run["manifest"]["run_id"], bin_name)).fetchone()
    if row is not None:
        return json.loads(row["payload"])
    baseline = book_bin(run, bin_name)
    if persist:
        # 首次扣回时把入库基线固化为版本 1，之后不再重算。
        db.execute("INSERT OR IGNORE INTO bin_versions(run_id,bin,version,return_id,payload,created) "
                   "VALUES(?,?,?,?,?,?)",
                   (baseline["run_id"], baseline["bin"], 1, None,
                    json.dumps(baseline, ensure_ascii=False), _now()))
    return baseline


def bin_history(run_id, bin_name):
    """已落账的仓批版本。版本 1 为入库基线（首次扣回时固化），之后每次退收 +1。"""
    with _db() as db:
        rows = db.execute(
            "SELECT payload FROM bin_versions WHERE run_id=? AND bin=? ORDER BY version",
            (run_id, bin_name)).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def _effect(db, return_id):
    row = db.execute("SELECT payload FROM return_effects WHERE return_id=?", (return_id,)).fetchone()
    return json.loads(row["payload"]) if row else None


def get_return(return_id):
    with _db() as db:
        row = db.execute("SELECT * FROM returns WHERE id=?", (return_id,)).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "退收单不存在", 404)
        draft = json.loads(row["payload"])
        effect = _effect(db, return_id)
    draft["state"] = row["state"]
    draft["posted"] = effect
    if effect is not None:
        draft["bin_before"] = effect["bin_before"]
        draft["bin_after"] = effect["bin_after"]
        draft["reconcile"] = effect["reconcile"]
        draft["bin_history"] = bin_history(draft["run_id"], draft["bin"])
    else:
        draft["bin_history"] = bin_history(draft["run_id"], draft["bin"])
    return draft


def list_returns(run_id=None):
    with _db() as db:
        sql = "SELECT id,run_id,truck_id,bin,state,created FROM returns"
        args = ()
        if run_id:
            sql += " WHERE run_id=?"; args = (run_id,)
        rows = db.execute(sql + " ORDER BY created DESC,id LIMIT 100", args).fetchall()
    return [dict(row) for row in rows]


def create_draft(run, truck_id, net_kg, moisture, impurity, reason=""):
    """幂等创建草稿：相同原车次与相同复测数据得到同一退收单，不重复建单。"""
    net_kg, moisture, impurity = rules.dec(net_kg), rules.dec(moisture), rules.dec(impurity)
    original = original_voucher(run, truck_id)
    rid = return_id_for(run["manifest"]["run_id"], truck_id, net_kg, moisture, impurity)
    red = rules.red_voucher(original, rid)
    draft = {
        "return_id": rid,
        "run_id": run["manifest"]["run_id"],
        "truck_id": truck_id,
        "bin": original["bin"],
        "reason": reason,
        "caliber": rules.CALIBER,
        "return_measure": {
            "net_kg": str(net_kg), "moisture": str(moisture), "impurity": str(impurity),
            "physical_dry_kg": str(rules.physical_dry(net_kg, impurity, moisture)),
        },
        "original": original,
        "deduction": {field: original[field] for field in rules.QTY_FIELDS},
        "variance": rules.variance(original["dry_kg"], net_kg, impurity, moisture, original["bin"]),
        "red_voucher": red,
        "reconcile": rules.reconcile(original, red),
        "created": _now(),
        "state": rules.DRAFT,
    }
    with _db() as db:
        db.execute("INSERT OR IGNORE INTO vouchers(voucher_no,run_id,truck_id,return_id,kind,payload,created) "
                   "VALUES(?,?,?,?,?,?,?)",
                   (original["voucher_no"], draft["run_id"], truck_id, None, "原凭证",
                    json.dumps(original, ensure_ascii=False), _now()))
        stored = json.loads(db.execute("SELECT payload FROM vouchers WHERE voucher_no=?",
                                       (original["voucher_no"],)).fetchone()["payload"])
        if stored != original:
            raise DomainError("CONFLICT", "原凭证与已登记版本不一致，拒绝改写")
        db.execute("INSERT OR IGNORE INTO returns(id,run_id,truck_id,bin,state,payload,created) "
                   "VALUES(?,?,?,?,?,?,?)",
                   (rid, draft["run_id"], truck_id, draft["bin"], rules.DRAFT,
                    json.dumps(draft, ensure_ascii=False), draft["created"]))
    return get_return(rid)


def preview_bin(run, draft):
    """当前仓批版本（只读）与扣回后的预览，草稿不落仓账。"""
    with _db() as db:
        before = latest_bin(db, run, draft["bin"])
        effect = _effect(db, draft["return_id"])
    if effect is not None:
        return before, effect["bin_after"]
    return before, _next_bin(before, draft["deduction"], draft["return_id"])


def _next_bin(before, deduction, return_id):
    values = rules.bin_after(before, deduction)
    return {
        "run_id": before["run_id"], "bin": before["bin"],
        "version": int(before["version"]) + 1, "source": "退收扣回",
        "return_id": return_id, "count": values["count"],
        **{field: str(values[field]) for field in rules.QTY_FIELDS},
    }


def post(return_id):
    """正式冲销：单事务扣仓批一次 + 开红字凭证一次；失败回滚并标记冲销失败。"""
    try:
        return _post(return_id)
    except DomainError as exc:
        mark_failed(return_id, exc)
        raise
    except Exception as exc:
        mark_failed(return_id, DomainError("ERROR", f"冲销事务失败：{exc}", 500))
        raise


def _post(return_id):
    db = _db()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM returns WHERE id=?", (return_id,)).fetchone()
        if row is None:
            raise DomainError("NOT_FOUND", "退收单不存在", 404)
        effect = _effect(db, return_id)
        if effect is not None:  # 重试：已生效，直接复用，不重复扣减
            db.rollback()
            return get_return(return_id)
        draft = json.loads(row["payload"])
        if row["state"] == rules.POSTED:
            db.rollback()
            return get_return(return_id)
        run = store.fetch(draft["run_id"])
        stored = db.execute("SELECT payload FROM vouchers WHERE voucher_no=? AND kind='原凭证'",
                            (draft["original"]["voucher_no"],)).fetchone()
        if stored is None:
            raise DomainError("ORPHAN", "缺少原凭证，无法冲销")
        original = json.loads(stored["payload"])
        if original != draft["original"]:
            raise DomainError("CONFLICT", "原凭证已登记且不可改写")
        if db.execute("SELECT 1 FROM vouchers WHERE voucher_no=? AND kind='红字'",
                      (f"RV-{return_id}",)).fetchone():
            db.rollback()
            return get_return(return_id)
        other = db.execute("SELECT return_id FROM vouchers WHERE kind='红字' "
                           "AND json_extract(payload,'$.reverses')=?",
                           (original["voucher_no"],)).fetchone()
        if other is not None:
            raise DomainError("STATE", "原凭证已由其他退收单冲销，禁止重复冲销", 409)
        red = rules.red_voucher(original, return_id)
        reconcile = rules.reconcile(original, red)  # 净额不守恒则抛错
        before = latest_bin(db, run, draft["bin"], persist=True)
        after = _next_bin(before, draft["deduction"], return_id)
        record = {
            "return_id": return_id, "run_id": draft["run_id"], "truck_id": draft["truck_id"],
            "bin": draft["bin"], "original": original, "red_voucher": red,
            "reconcile": reconcile, "variance": draft["variance"],
            "bin_before": before, "bin_after": after, "posted": _now(),
        }
        db.execute("INSERT INTO vouchers(voucher_no,run_id,truck_id,return_id,kind,payload,created) "
                   "VALUES(?,?,?,?,?,?,?)",
                   (red["voucher_no"], draft["run_id"], draft["truck_id"], return_id, "红字",
                    json.dumps(red, ensure_ascii=False), _now()))
        db.execute("INSERT INTO bin_versions(run_id,bin,version,return_id,payload,created) "
                   "VALUES(?,?,?,?,?,?)",
                   (after["run_id"], after["bin"], after["version"], return_id,
                    json.dumps(after, ensure_ascii=False), _now()))
        db.execute("INSERT INTO return_effects(return_id,run_id,return_bin,bin_version,red_voucher_no,payload,created) "
                   "VALUES(?,?,?,?,?,?,?)",
                   (return_id, draft["run_id"], draft["bin"], after["version"], red["voucher_no"],
                    json.dumps(record, ensure_ascii=False), _now()))
        db.execute("UPDATE returns SET state=? WHERE id=?", (rules.POSTED, return_id))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return get_return(return_id)


def mark_failed(return_id, error):
    """冲销失败仅改退收单状态；仓批与凭证保持旧版（事务已回滚）。已生效的草稿不降级。"""
    with _db() as db:
        row = db.execute("SELECT state,payload FROM returns WHERE id=?", (return_id,)).fetchone()
        if row is None or row["state"] == rules.POSTED:
            return
        draft = json.loads(row["payload"])
        draft["state"] = rules.FAILED
        draft["failure"] = {"code": getattr(error, "code", "ERROR"),
                            "message": getattr(error, "message", str(error))}
        db.execute("UPDATE returns SET state=?,payload=? WHERE id=?",
                   (rules.FAILED, json.dumps(draft, ensure_ascii=False), return_id))

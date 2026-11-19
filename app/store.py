import os,sqlite3,json
from pathlib import Path
from app.errors import DomainError
SCHEMA = (
 "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY,payload TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,review TEXT NOT NULL DEFAULT '待复核')",
 """CREATE TABLE IF NOT EXISTS returns (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL, truck_id TEXT NOT NULL,
        bin TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL,
        created TEXT NOT NULL)""",
 """CREATE TABLE IF NOT EXISTS vouchers (
        voucher_no TEXT PRIMARY KEY, run_id TEXT NOT NULL, truck_id TEXT NOT NULL,
        return_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL,
        created TEXT NOT NULL)""",
 """CREATE TABLE IF NOT EXISTS bin_versions (
        run_id TEXT NOT NULL, bin TEXT NOT NULL, version INTEGER NOT NULL,
        return_id TEXT, payload TEXT NOT NULL, created TEXT NOT NULL,
        PRIMARY KEY(run_id, bin, version))""",
 """CREATE TABLE IF NOT EXISTS return_effects (
        return_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, return_bin TEXT NOT NULL,
        bin_version INTEGER NOT NULL, red_voucher_no TEXT NOT NULL,
        payload TEXT NOT NULL, created TEXT NOT NULL)""",
)
_ready=set()
def connection():
    path=Path(os.environ.get("DATA_DIR","data"));path.mkdir(parents=True,exist_ok=True)
    file=path/"runs.sqlite"
    db=sqlite3.connect(file,timeout=15)
    db.row_factory=sqlite3.Row
    ensure_schema(db)
    return db
def ensure_schema(db):
    # 建表要拿写锁，逐次执行很慢；进程内按库路径缓存，但先确认 runs 表还在
    # （测试会删库重建同一路径，不能只看路径）。
    key=db.execute("PRAGMA database_list").fetchone()[2]
    if key in _ready and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone():
        return db
    for statement in SCHEMA: db.execute(statement)
    db.commit();_ready.add(key)
    return db
def save(result):
    with connection() as db:
        db.execute("INSERT OR IGNORE INTO runs(id,payload) VALUES(?,?)",(result["manifest"]["run_id"],json.dumps(result,ensure_ascii=False)))
    return fetch(result["manifest"]["run_id"])
def fetch(run_id):
    with connection() as db: row=db.execute("SELECT * FROM runs WHERE id=?",(run_id,)).fetchone()
    if row is None: raise DomainError("NOT_FOUND","批次不存在",404)
    result=json.loads(row["payload"]);result["version"]=row["version"];result["review"]=row["review"]
    return result
def history():
    with connection() as db: rows=db.execute("SELECT id,version,review FROM runs ORDER BY rowid DESC LIMIT 100").fetchall()
    return [dict(row) for row in rows]
def review(run_id,version,decision):
    if decision not in ("通过","退回"): raise DomainError("REVIEW","请选择通过或退回")
    if not isinstance(version,int): raise DomainError("STALE","版本必须为整数")
    with connection() as db:
        cur=db.execute("UPDATE runs SET review=?,version=version+1 WHERE id=? AND version=?",(decision,run_id,version))
        if cur.rowcount!=1: raise DomainError("STALE","复核版本已更新",409)
    return fetch(run_id)

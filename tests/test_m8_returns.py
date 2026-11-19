import json
import pytest
from app import store
from app.errors import DomainError
from app.pipeline import run
from app.returns import repo, rules


@pytest.fixture
def batch(isolate):
    result = run()
    return result, result["manifest"]["run_id"]


@pytest.fixture
def client_batch(client):
    result = client.post("/api/runs", json={}).json
    return client, result


def make(client, batch, truck_id, net="1000", moisture="20", impurity="3"):
    reply = client.post("/api/returns", json={
        "run_id": batch["manifest"]["run_id"], "truck_id": truck_id,
        "net_kg": net, "moisture": moisture, "impurity": impurity,
        "reason": "复检杂质超标"})
    assert reply.status_code == 200, reply.json
    return reply.json


def test_draft_does_not_touch_bin(batch):
    result, _ = batch
    item = repo.create_draft(result, "00001", "1000", "20", "3")
    assert item["state"] == rules.DRAFT
    assert repo.bin_history(result["manifest"]["run_id"], "仓1") == []


def test_bin_deduction_uses_original_truck(batch):
    result, _ = batch
    truck = result["records"][0]
    item = repo.create_draft(result, truck["id"], "1000", "20", "3")
    before = repo.book_bin(result, "仓1")
    posted = repo.post(item["return_id"])
    after = posted["bin_after"]
    for field in ("net_kg", "clean_kg", "dry_kg", "standard_kg", "amount"):
        assert rules.dec(before[field]) - rules.dec(after[field]) == rules.dec(truck[field])
    assert after["count"] == before["count"] - 1


def test_red_reversal_nets_to_zero(batch):
    result, _ = batch
    truck = result["records"][0]
    item = repo.create_draft(result, truck["id"], "1000", "20", "3")
    posted = repo.post(item["return_id"])
    rec = posted["reconcile"]
    assert rec["net_amount"] == "0.00"
    assert rules.dec(rec["original_amount"]) == -rules.dec(rec["reversal_amount"])
    assert rules.dec(rec["original_amount"]) == rules.dec(truck["amount"])


def test_original_voucher_not_rewritten(batch):
    result, _ = batch
    truck = result["records"][0]
    item = repo.create_draft(result, truck["id"], "1000", "20", "3")
    before = store.connection().execute(
        "SELECT payload FROM vouchers WHERE voucher_no=?",
        (item["original"]["voucher_no"],)).fetchone()["payload"]
    posted = repo.post(item["return_id"])
    after = store.connection().execute(
        "SELECT payload FROM vouchers WHERE voucher_no=?",
        (item["original"]["voucher_no"],)).fetchone()["payload"]
    assert before == after
    assert json.loads(after)["amount"] == truck["amount"]
    assert posted["red_voucher"]["reverses"] == item["original"]["voucher_no"]


def test_retry_does_not_double_deduct(batch):
    result, _ = batch
    item = repo.create_draft(result, "00001", "1000", "20", "3")
    first = repo.post(item["return_id"])
    second = repo.post(item["return_id"])
    assert second["bin_after"] == first["bin_after"]
    history = repo.bin_history(result["manifest"]["run_id"], "仓1")
    assert [row["version"] for row in history] == [1, 2]


def test_same_draft_is_idempotent(batch):
    result, _ = batch
    a = repo.create_draft(result, "00001", "1000", "20", "3")
    b = repo.create_draft(result, "00001", "1000", "20", "3")
    c = repo.create_draft(result, "00001", "1000.000", "20.0", "3.0")
    assert a["return_id"] == b["return_id"] == c["return_id"]
    d = repo.create_draft(result, "00001", "990", "21", "3")
    assert d["return_id"] != a["return_id"]


def test_second_reversal_of_same_voucher_blocked(batch):
    result, _ = batch
    a = repo.create_draft(result, "00001", "1000", "20", "3")
    b = repo.create_draft(result, "00001", "990", "21", "3")
    repo.post(a["return_id"])
    with pytest.raises(DomainError) as exc:
        repo.post(b["return_id"])
    assert exc.value.code == "STATE"
    item = repo.get_return(b["return_id"])
    assert item["state"] == rules.FAILED
    assert item["posted"] is None
    assert item["bin_history"][-1]["version"] == 2  # 只扣过一次
    row = store.connection().execute(
        "SELECT 1 FROM vouchers WHERE voucher_no=?", (f"RV-{b['return_id']}",)).fetchone()
    assert row is None  # 失败时不留红字凭证


def test_failed_post_rolls_back_bin_and_voucher(batch, monkeypatch):
    result, run_id = batch
    item = repo.create_draft(result, "00001", "1000", "20", "3")
    original = store.connection().execute(
        "SELECT payload FROM vouchers WHERE voucher_no=?",
        (item["original"]["voucher_no"],)).fetchone()["payload"]

    original_next = repo._next_bin
    def boom(before, deduction, return_id):
        raise DomainError("STATE", "模拟扣减失败")

    monkeypatch.setattr(repo, "_next_bin", boom)
    with pytest.raises(DomainError):
        repo.post(item["return_id"])
    monkeypatch.setattr(repo, "_next_bin", original_next)

    failed = repo.get_return(item["return_id"])
    assert failed["state"] == rules.FAILED
    assert failed["bin_history"] == []  # 没有新版本落账
    assert store.connection().execute(
        "SELECT payload FROM vouchers WHERE voucher_no=?",
        (item["original"]["voucher_no"],)).fetchone()["payload"] == original
    assert store.connection().execute(
        "SELECT 1 FROM vouchers WHERE voucher_no=?", (f"RV-{item['return_id']}",)).fetchone() is None
    assert store.connection().execute(
        "SELECT 1 FROM bin_versions WHERE run_id=?", (run_id,)).fetchone() is None
    recovered = repo.post(item["return_id"])
    assert recovered["state"] == rules.POSTED


def test_variance_owner_is_bin(batch):
    result, _ = batch
    truck = result["records"][0]
    item = repo.create_draft(result, truck["id"], "1000", "20", "3")
    var = item["variance"]
    assert var["diff_owner"] == f"仓批 {truck['bin']}"
    assert var["caliber"] == rules.CALIBER
    assert rules.dec(var["physical_dry_kg"]) == rules.physical_dry("1000", "3", "20")
    assert rules.dec(var["diff_kg"]) == rules.dec(var["physical_dry_kg"]) - rules.dec(truck["dry_kg"])


def test_variance_not_charged_to_other_trucks(batch):
    result, _ = batch
    truck = result["records"][0]
    item = repo.create_draft(result, truck["id"], "1000", "20", "3")
    posted = repo.post(item["return_id"])
    others = [r for r in result["records"] if r["bin"] == truck["bin"] and r["id"] != truck["id"]]
    before = sum(rules.dec(r["amount"]) for r in others)
    after_bin = rules.dec(posted["bin_after"]["amount"])
    assert after_bin == sum(rules.dec(r["amount"]) for r in result["records"]
                            if r["bin"] == truck["bin"]) - rules.dec(truck["amount"])
    assert rules.dec(posted["bin_after"]["amount"]) == before


def test_sequential_returns_conserve_bin(batch):
    result, run_id = batch
    trucks = [r for r in result["records"] if r["bin"] == "仓1"][:2]
    first = repo.post(repo.create_draft(result, trucks[0]["id"], "1000", "20", "3")["return_id"])
    second = repo.post(repo.create_draft(result, trucks[1]["id"], "1000", "16", "2")["return_id"])
    assert first["state"] == second["state"] == rules.POSTED
    history = repo.bin_history(run_id, "仓1")
    assert [row["version"] for row in history] == [1, 2, 3]
    remaining = [r for r in result["records"] if r["bin"] == "仓1" and r["id"] not in
                 {trucks[0]["id"], trucks[1]["id"]}]
    for field in ("net_kg", "clean_kg", "dry_kg", "standard_kg", "amount"):
        # 折标重是无限小数，扣除后应精确等于剩余车次之和（60 位精度累加）
        assert rules.dec(history[-1][field]) == rules.total(remaining, field)
    assert history[-1]["count"] == len(remaining)


def test_variance_does_not_alter_amount(batch):
    result, _ = batch
    first, second = result["records"][0], result["records"][1]
    # 复测水杂只影响差额展示，不改变扣回数量与金额：扣回始终按原车次账面值。
    post_a = repo.post(repo.create_draft(result, first["id"], "1000", "20", "1")["return_id"])
    draft_b = repo.create_draft(result, second["id"], "1000", "25", "5")
    assert rules.dec(draft_b["deduction"]["amount"]) == rules.dec(second["amount"])
    assert rules.dec(draft_b["variance"]["diff_kg"]) != 0
    assert rules.dec(post_a["bin_before"]["amount"]) - rules.dec(post_a["bin_after"]["amount"]) == rules.dec(first["amount"])


def test_physical_dry_rejects_bad_measure():
    with pytest.raises(DomainError):
        rules.physical_dry("0", "0", "14")
    with pytest.raises(DomainError):
        rules.physical_dry("1000", "0", "120")


def test_bin_shortfall_rejected():
    with pytest.raises(DomainError):
        rules.bin_after({"count": 1, "net_kg": "10", "clean_kg": "10", "dry_kg": "8",
                         "standard_kg": "9", "amount": "20"},
                        {"net_kg": "100", "clean_kg": "100", "dry_kg": "80",
                         "standard_kg": "90", "amount": "200"})


def test_unsettled_batch_cannot_voucher(batch):
    result, _ = batch
    unsold = run(stop=5, persist=False)
    truck = unsold["records"][0]["id"]
    with pytest.raises(DomainError) as exc:
        repo.create_draft(unsold, truck, "1000", "20", "3")
    assert exc.value.code == "STATE"
    assert result["records"][0]["amount"]


def test_unknown_truck_rejected(batch):
    result, _ = batch
    with pytest.raises(DomainError) as exc:
        repo.create_draft(result, "不存在", "1000", "20", "3")
    assert exc.value.code == "ORPHAN"


def test_http_full_flow(client_batch):
    client, batch = client_batch
    truck = batch["records"][0]["id"]
    item = make(client, batch, truck)
    assert client.get(f"/m/8?return={item['return_id']}").status_code == 200
    assert client.get(f"/m/8?run={batch['manifest']['run_id']}").status_code == 200
    posted = client.post(f"/api/returns/{item['return_id']}/post", json={})
    assert posted.status_code == 200
    assert posted.json["state"] == rules.POSTED
    detail = client.get(f"/api/returns/{item['return_id']}").json
    assert detail["bin_after"]["version"] == 2
    assert client.get(f"/api/returns?run={batch['manifest']['run_id']}").json[0]["state"] == rules.POSTED
    vouchers = client.get(f"/api/returns/{item['return_id']}/vouchers").json
    assert vouchers["red"]["amount"] == "-" + vouchers["original"]["amount"]
    assert vouchers["reconcile"]["net_amount"] == "0.00"


def test_http_missing_fields(client_batch):
    client, batch = client_batch
    assert client.post("/api/returns", json={"run_id": batch["manifest"]["run_id"]}).status_code == 422
    assert client.post("/api/returns", json={}).status_code == 422


def test_page_shows_bin_versions(client_batch):
    client, batch = client_batch
    item = make(client, batch, batch["records"][0]["id"])
    client.post(f"/api/returns/{item['return_id']}/post", json={})
    text = client.get(f"/m/8?return={item['return_id']}").get_data(as_text=True)
    assert "退收扣回" in text and "入库" in text and "红字" in text

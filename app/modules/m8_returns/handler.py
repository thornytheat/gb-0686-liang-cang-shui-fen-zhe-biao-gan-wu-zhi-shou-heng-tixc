from flask import Blueprint, request, jsonify
from app import store
from app.errors import DomainError
from app.returns import repo
bp = Blueprint("m8", __name__)


def _payload():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise DomainError("BODY", "请求必须是对象")
    return payload


@bp.get("/api/returns")
def list_returns():
    return jsonify(repo.list_returns(request.args.get("run")))


@bp.post("/api/returns")
def create_return():
    payload = _payload()
    run_id = payload.get("run_id")
    if not run_id:
        raise DomainError("MISSING", "缺少批次号")
    for key in ("truck_id", "net_kg", "moisture", "impurity"):
        if payload.get(key) in (None, ""):
            raise DomainError("MISSING", f"缺少必填值 {key}")
    result = store.fetch(run_id)
    item = repo.create_draft(result, str(payload["truck_id"]), payload["net_kg"],
                             payload["moisture"], payload["impurity"],
                             payload.get("reason", ""))
    return jsonify(item)


@bp.get("/api/returns/<return_id>")
def get_return(return_id):
    return jsonify(repo.get_return(return_id))


@bp.post("/api/returns/<return_id>/post")
def post_return(return_id):
    return jsonify(repo.post(return_id))


@bp.get("/api/returns/<return_id>/vouchers")
def vouchers(return_id):
    item = repo.get_return(return_id)
    effect = item.get("posted") or {}
    return jsonify(original=item["original"], red=effect.get("red_voucher"),
                   reconcile=item.get("reconcile"))


@bp.get("/api/bins/<run_id>/<bin>/history")
def bin_history(run_id, bin):
    return jsonify(repo.bin_history(run_id, bin))

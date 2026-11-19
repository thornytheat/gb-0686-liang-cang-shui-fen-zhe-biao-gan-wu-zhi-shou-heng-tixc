"""M8 退收冲销：登记退收单、预览仓批扣回、正式冲销并出具红字凭证。"""
from flask import render_template
from app import store
from app.config import TITLE, MODULES, COLUMNS
from app.errors import DomainError
from app.pipeline import run
from app.returns import repo


def view(return_id=None, run_id=None):
    error = None
    item = None
    preview = None
    try:
        if return_id:
            item = repo.get_return(return_id)
            batch = store.fetch(item["run_id"])
            if item["state"] != "已生效":
                preview = repo.preview_bin(batch, item)
        else:
            batch = store.fetch(run_id) if run_id else run()
    except DomainError as exc:
        error, batch = exc.message, store.fetch(run_id) if run_id else run()
    return render_template("m8.html", title=TITLE, modules=MODULES, columns=COLUMNS, stage=8,
                           item=item, result=batch, error=error, preview=preview,
                           returns=repo.list_returns(batch["manifest"]["run_id"]),
                           trucks=batch.get("records", [])), 200 if error is None else 422

"""Previewed, single-use, audited ZFS administration through the root helper."""
import json
import logging
from contextlib import contextmanager
import shlex
from datetime import datetime, timedelta

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update

from .collector import SSHCollector
from .config import REMOTE_HELPER_PATH
from .experience import effective_role
from .insights import session
from .maintenance import MaintenanceBusy, maintenance_lock, invalidate_inventory_cache
from .models import MaintenanceAction, Server
from .scheduler import trigger_now
from .security import require_csrf, require_secure_maintenance


@contextmanager
def action_lock(server_id):
    try:
        with maintenance_lock(server_id):
            yield
    except MaintenanceBusy as exc:
        raise HTTPException(409, str(exc)) from exc


def admin(request):
    require_secure_maintenance(request)
    if effective_role(request.state.current_user) != "admin":
        raise HTTPException(403, "ZFS administration requires an administrator.")


def helper_call(server, mode, payload=None, fingerprint=None):
    args = ["sudo", "-n", REMOTE_HELPER_PATH, "actions", mode]
    if payload is not None:
        args.append(json.dumps(payload, separators=(",", ":")))
    if fingerprint:
        args.append(fingerprint)
    with SSHCollector(server) as ssh:
        result = ssh.run(shlex.join(args), timeout=150)
    if result.get("stdout_truncated") or result.get("stderr_truncated"):
        raise ValueError("Helper response exceeded the output limit; refresh and inspect pool status.")
    return result


def helper_json(server, mode, payload=None):
    result = helper_call(server, mode, payload)
    if result["exit"]:
        raise ValueError((result.get("stderr") or result.get("stdout") or "Remote helper failed")[:4000])
    return json.loads(result["stdout"])


def install(app, templates):
    @app.get("/servers/{server_id}/zfs-actions")
    def page(request: Request, server_id: int, result_id: int | None = None, db=Depends(session)):
        admin(request)
        server = db.get(Server, server_id)
        if server is None:
            raise HTTPException(404)
        inventory, error, result = None, "", None
        try:
            inventory = helper_json(server, "inventory")
            if inventory.get("protocol") != 1:
                raise ValueError("Update the remote helper to enable ZFS administration.")
        except Exception as exc:
            inventory = None
            error = str(exc)
        if result_id:
            result = db.scalar(select(MaintenanceAction).where(MaintenanceAction.id == result_id, MaintenanceAction.server_id == server_id))
        return templates.TemplateResponse(request=request, name="zfs_actions.html", context={
            "server": server, "inventory": inventory, "error": error, "result": result})

    @app.post("/servers/{server_id}/zfs-actions/preview", dependencies=[Depends(require_csrf)])
    def preview(request: Request, server_id: int, action: str = Form(...), pool: str = Form(...),
                target: str = Form(""), disks: list[str] = Form([]), layout: str = Form("mirror"),
                role: str = Form("data"), ashift: str = Form("12"), new_pool: str = Form(""),
                property: str = Form(""), value: str = Form(""), db=Depends(session)):
        admin(request)
        server = db.get(Server, server_id)
        if server is None:
            raise HTTPException(404)
        payload = dict(action=action, pool=pool, target=target, disks=disks, layout=layout,
                       role=role, ashift=ashift, new_pool=new_pool, property=property, value=value)
        if len(json.dumps(payload)) > 16384:
            raise HTTPException(400, "Action request is too large")
        try:
            with action_lock(server.id):
                plan = helper_json(server, "preview", payload)
        except HTTPException as exc:
            return templates.TemplateResponse(request=request, name="zfs_action_preview.html", status_code=exc.status_code,
                context={"server": server, "error": str(exc.detail), "plan": None})
        except Exception as exc:
            return templates.TemplateResponse(request=request, name="zfs_action_preview.html", status_code=400,
                context={"server": server, "error": str(exc), "plan": None})
        row = MaintenanceAction(server_id=server.id, actor=request.state.current_user.username,
            action="zpool_" + action, pool=pool, old_device=plan.get("target_id", target), new_device=", ".join(disks),
            command=plan["command"], state="preview", success=False, output=json.dumps(plan))
        db.add(row)
        db.commit()
        return templates.TemplateResponse(request=request, name="zfs_action_preview.html",
            context={"server": server, "plan": plan, "action_id": row.id, "error": ""})

    @app.post("/servers/{server_id}/zfs-actions/execute", dependencies=[Depends(require_csrf)])
    def execute(request: Request, server_id: int, action_id: int = Form(...),
                confirm_text: str = Form(...), db=Depends(session)):
        admin(request)
        server = db.get(Server, server_id)
        if server is None:
            raise HTTPException(404)

        def problem(status, message, plan=None):
            return templates.TemplateResponse(request=request, name="zfs_action_preview.html", status_code=status,
                context={"server": server, "error": message, "plan": plan, "action_id": action_id})

        row = db.get(MaintenanceAction, action_id)
        if row is None or row.server_id != server_id or row.actor != request.state.current_user.username:
            raise HTTPException(404)
        if row.state != "preview" or row.created_at < datetime.utcnow() - timedelta(minutes=10):
            return problem(409, "This preview expired or was already submitted. Generate a new preview.")
        plan = json.loads(row.output)
        if confirm_text != plan["confirmation"]:
            return problem(400, "Confirmation text does not match the preview", plan)
        try:
            with action_lock(server_id):
                claimed = db.execute(update(MaintenanceAction).where(MaintenanceAction.id == action_id,
                    MaintenanceAction.state == "preview",
                    MaintenanceAction.created_at >= datetime.utcnow() - timedelta(minutes=10)).values(state="executing"))
                if claimed.rowcount != 1:
                    db.rollback()
                    return problem(409, "This action expired or was already submitted")
                db.commit()  # Persist before remote execution: a retry must never repeat a mutation.
                try:
                    result = helper_call(server, "execute", plan["request"], plan["fingerprint"])
                    row.state = "accepted" if result["exit"] == 0 else ("unknown" if result["exit"] in {-1, 124, 255} else "failed")
                    row.success = result["exit"] == 0
                    row.exit_code = result["exit"]
                    row.output = "\n".join([result.get("stdout", ""), result.get("stderr", "")])[-20000:]
                    if row.state == "unknown":
                        row.output = "Execution outcome is unknown. Inspect pool status before retrying.\n" + row.output
                except Exception as exc:
                    row.state = "unknown"
                    row.success = False
                    row.output = "Execution outcome is unknown. Inspect pool status before retrying. " + str(exc)[:19000]
                row.completed_at = datetime.utcnow()
                db.commit()
                invalidate_inventory_cache(server_id)
                try:
                    trigger_now(server_id)
                except Exception:
                    logging.getLogger(__name__).exception("Could not schedule refresh after ZFS action %s", action_id)
        except HTTPException as exc:
            return problem(exc.status_code, str(exc.detail))
        return RedirectResponse(f"/servers/{server_id}/zfs-actions?result_id={action_id}", status_code=303)

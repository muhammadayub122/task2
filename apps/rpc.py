import json
import logging

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .services import (
    BusinessError,
    cancel_transfer,
    confirm_transfer,
    create_transfer,
    get_error_message,
    jsonrpc_error,
    jsonrpc_success,
    log_rpc_call,
    transfer_history,
    transfer_state,
)

logger = logging.getLogger(__name__)

try:
    from jsonrpcserver import method  # type: ignore

    JSONRPCSERVER_AVAILABLE = True
except ImportError:
    JSONRPCSERVER_AVAILABLE = False

    def method(name=None):  # type: ignore
        def decorator(func):
            func.jsonrpc_name = name or func.__name__
            return func

        return decorator


@method(name="transfer.create")
def rpc_transfer_create(params: dict) -> dict:
    logger.info("RPC Method called: transfer.create")
    return create_transfer(params)


@method(name="transfer.confirm")
def rpc_transfer_confirm(params: dict) -> dict:
    logger.info("RPC Method called: transfer.confirm")
    return confirm_transfer(params)


@method(name="transfer.cancel")
def rpc_transfer_cancel(params: dict) -> dict:
    logger.info("RPC Method called: transfer.cancel")
    return cancel_transfer(params)


@method(name="transfer.state")
def rpc_transfer_state(params: dict) -> dict:
    logger.info("RPC Method called: transfer.state")
    return transfer_state(params)


@method(name="transfer.history")
def rpc_transfer_history(params: dict) -> list[dict]:
    logger.info("RPC Method called: transfer.history")
    return transfer_history(params)


METHODS = {
    "transfer.create": rpc_transfer_create,
    "transfer.confirm": rpc_transfer_confirm,
    "transfer.cancel": rpc_transfer_cancel,
    "transfer.state": rpc_transfer_state,
    "transfer.history": rpc_transfer_history,
}


@csrf_exempt
def jsonrpc_endpoint(request: HttpRequest) -> JsonResponse:
    if request.method != "POST":
        return JsonResponse(jsonrpc_error(None, 32713, get_error_message(32713)), status=405)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return JsonResponse(jsonrpc_error(None, 32706, "Invalid JSON body."), status=400)

    request_id = payload.get("id")
    method_name = payload.get("method")
    params = payload.get("params", {})
    log_rpc_call(str(method_name), payload)

    if method_name not in METHODS:
        return JsonResponse(jsonrpc_error(request_id, 32714, get_error_message(32714)), status=404)

    try:
        result = METHODS[method_name](params)
        return JsonResponse(jsonrpc_success(request_id, result))
    except BusinessError as exc:
        logger.warning("Business error on %s: %s", method_name, exc)
        return JsonResponse(jsonrpc_error(request_id, exc.code, exc.message), status=400)
    except Exception:
        logger.exception("Unhandled RPC error on %s", method_name)
        return JsonResponse(
            jsonrpc_error(request_id, 32706, get_error_message(32706)),
            status=500,
        )

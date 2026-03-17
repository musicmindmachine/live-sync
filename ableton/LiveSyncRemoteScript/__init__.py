from .debug_log import log_debug
from .LiveSyncRemoteScript import LiveSyncRemoteScript


def create_instance(c_instance):
    log_debug("create_instance called")
    try:
        return LiveSyncRemoteScript(c_instance)
    except Exception as error:
        log_debug("create_instance failed: %s" % error)
        raise

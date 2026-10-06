"""Small Windows Credential Manager adapter for the optional Redmine key."""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import re
import sys
from ctypes import wintypes

try:
    from redmine_client import RedmineError
    from redmine_policy import normalize_base_url
except ModuleNotFoundError:
    from scripts.redmine_client import RedmineError
    from scripts.redmine_policy import normalize_base_url


def target_name(base_url: str) -> str:
    canonical = normalize_base_url(base_url)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f"Flow1C:Redmine:{digest}"


def transaction_backup_target_name(transaction_id: str) -> str:
    token = str(transaction_id)
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise RedmineError("Redmine credential recovery identifier is invalid.")
    return f"Flow1C:Redmine:Transaction:{token}"


def _api() -> tuple[object, type[ctypes.Structure]]:
    if sys.platform != "win32":
        raise RedmineError("Windows Credential Manager is available only on Windows; set FLOW1C_REDMINE_API_KEY in the agent environment.")

    class FileTime(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class Credential(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR), ("LastWritten", FileTime), ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.c_void_p), ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
        ]

    return ctypes.WinDLL("Advapi32.dll", use_last_error=True), Credential


def _read_target(target: str) -> str | None:
    api, Credential = _api()
    pointer = ctypes.POINTER(Credential)()
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(Credential))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    if not api.CredReadW(target, 1, 0, ctypes.byref(pointer)):
        error = ctypes.get_last_error()
        if error == 1168:
            return None
        raise RedmineError(f"Windows Credential Manager could not read the Redmine key (error {error}).")
    try:
        credential = pointer.contents
        if not credential.CredentialBlob or credential.CredentialBlobSize % 2:
            raise RedmineError("Windows Credential Manager contains an invalid Redmine key.")
        return ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize).decode("utf-16-le")
    finally:
        api.CredFree(pointer)


def read_api_key(base_url: str) -> str | None:
    if sys.platform != "win32":
        return None
    return _read_target(target_name(base_url))


def read_transaction_backup(transaction_id: str) -> str | None:
    if sys.platform != "win32":
        return None
    return _read_target(transaction_backup_target_name(transaction_id))


def _write_target(target: str, api_key: str) -> None:
    secret = str(api_key)
    encoded = secret.encode("utf-16-le")
    if not secret or len(encoded) > 5120:
        raise RedmineError("Redmine API key is empty or exceeds the Windows credential size limit.")
    api, Credential = _api()
    blob = ctypes.create_string_buffer(encoded)
    credential = Credential()
    credential.Type = 1  # CRED_TYPE_GENERIC
    credential.TargetName = target
    credential.CredentialBlobSize = len(encoded)
    credential.CredentialBlob = ctypes.cast(blob, ctypes.c_void_p)
    credential.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE; still scoped to the current Windows user
    credential.UserName = "Flow1C"
    api.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    if not api.CredWriteW(ctypes.byref(credential), 0):
        error = ctypes.get_last_error()
        raise RedmineError(f"Windows Credential Manager could not save the Redmine key (error {error}).")


def write_api_key(base_url: str, api_key: str) -> None:
    if sys.platform != "win32":
        raise RedmineError("Windows Credential Manager is available only on Windows; set FLOW1C_REDMINE_API_KEY in the agent environment.")
    _write_target(target_name(base_url), api_key)


def write_transaction_backup(transaction_id: str, api_key: str) -> None:
    if sys.platform != "win32":
        raise RedmineError("Windows Credential Manager recovery storage is available only on Windows.")
    _write_target(transaction_backup_target_name(transaction_id), api_key)


def verify_api_key(base_url: str, expected_api_key: str) -> bool:
    """Verify a saved credential without returning or formatting its value."""
    stored = read_api_key(base_url)
    return stored is not None and hmac.compare_digest(stored.encode("utf-8"), str(expected_api_key).encode("utf-8"))


def _delete_target(target: str) -> bool:
    api, _ = _api()
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    if not api.CredDeleteW(target, 1, 0):
        error = ctypes.get_last_error()
        if error == 1168:
            return False
        raise RedmineError(f"Windows Credential Manager could not remove the Redmine key (error {error}).")
    return True


def delete_api_key(base_url: str) -> bool:
    """Delete a credential, returning False when it was already absent."""
    if sys.platform != "win32":
        return False
    return _delete_target(target_name(base_url))


def delete_transaction_backup(transaction_id: str) -> bool:
    if sys.platform != "win32":
        return False
    return _delete_target(transaction_backup_target_name(transaction_id))

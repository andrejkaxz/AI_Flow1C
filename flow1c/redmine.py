"""Redmine configuration transactions and attachment operations."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterable

from flow1c import context as runtime
from flow1c import intake as intake_service
from flow1c import storage as storage
from flow1c import work_items as work_items
from flow1c.errors import WorkflowError
from flow1c.results import OperationResult
from flow1c.workflow import state as gate_state
from scripts import redmine_client as ext_scripts_redmine_client
from scripts import redmine_credentials
from scripts import redmine_policy as ext_scripts_redmine_policy


class RedmineOperationError(WorkflowError):
    """Safe, machine-readable failure from Redmine setup or disconnect."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        recoverable: bool,
        next_action: str,
        previous_connection_preserved: bool,
        additional_errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        primary = {
            "code": code,
            "message": message,
            "recoverable": recoverable,
            "next_action": next_action,
            "previous_connection_preserved": previous_connection_preserved,
        }
        self.payload = {
            "schema_version": 1,
            "state": "FAILED",
            "code": code,
            "configured": None,
            "verified": False,
            "previous_connection_preserved": previous_connection_preserved,
            "errors": (
                list(additional_errors)
                if code == "REDMINE_ROLLBACK_FAILED" and additional_errors
                else [primary, *(additional_errors or [])]
            ),
            "next_action": next_action,
        }


def redmine_settings(local: dict[str, Any] | None = None, *, product_root: Path) -> dict[str, Any]:
    local = (
        local
        if local is not None
        else storage.read_json(product_root / runtime.LOCAL_CONFIG_FILE, {})
    )
    value = local.get("redmine", {}) if isinstance(local, dict) else {}
    return value if isinstance(value, dict) else {}


def redmine_api_key(base_url: str) -> tuple[str, str]:
    from_environment = os.environ.get("FLOW1C_REDMINE_API_KEY", "").strip()
    if from_environment:
        return (from_environment, "environment")
    try:
        stored = redmine_credentials.read_api_key(base_url)
    except (
        ext_scripts_redmine_client.RedmineError,
        ext_scripts_redmine_policy.RedminePolicyError,
    ) as exc:
        raise WorkflowError(str(exc)) from exc
    if stored:
        return (stored, "windows-credential-manager")
    raise WorkflowError(
        "Redmine API key is not configured. On Windows run `flow1c.py redmine configure --url <HTTPS URL>`; other environments must set FLOW1C_REDMINE_API_KEY for the agent process."
    )


def redmine_client_from_config(
    *, recover: bool = True, product_root: Path
) -> tuple[ext_scripts_redmine_client.RedmineClient, str, str]:
    recovery_warnings = recover_redmine_transaction(product_root=product_root) if recover else []
    settings = redmine_settings(product_root=product_root)
    raw_url = str(settings.get("base_url", "")).strip()
    if not raw_url:
        raise WorkflowError(
            "Redmine is not configured. Run `flow1c.py redmine configure --url <HTTPS URL>`."
        )
    try:
        base_url = ext_scripts_redmine_policy.normalize_base_url(raw_url)
        api_key, source = redmine_api_key(base_url)
        client = ext_scripts_redmine_client.RedmineClient(base_url, api_key)
        client.recovery_warnings = recovery_warnings
        return (client, base_url, source)
    except (
        ext_scripts_redmine_client.RedmineError,
        ext_scripts_redmine_policy.RedminePolicyError,
    ) as exc:
        raise WorkflowError(str(exc)) from exc


def redmine_cleanup_state_path(*, product_root: Path) -> Path:
    return product_root / ".workspace" / "redmine-pending-cleanup.json"


def redmine_transaction_state_path(*, product_root: Path) -> Path:
    return product_root / ".workspace" / "redmine-configure-transaction.json"


def redmine_pending_cleanup_urls(*, product_root: Path) -> list[str]:
    state = storage.read_json(redmine_cleanup_state_path(product_root=product_root), {})
    values = state.get("urls", []) if isinstance(state, dict) else []
    result: list[str] = []
    for value in values if isinstance(values, list) else []:
        try:
            normalized = ext_scripts_redmine_policy.normalize_base_url(str(value))
        except (
            ext_scripts_redmine_client.RedmineError,
            ext_scripts_redmine_policy.RedminePolicyError,
        ):
            continue
        if normalized not in result:
            result.append(normalized)
    return result


def save_redmine_pending_cleanup_urls(urls: Iterable[str], *, product_root: Path) -> None:
    path = redmine_cleanup_state_path(product_root=product_root)
    cleaned: list[str] = []
    for value in urls:
        normalized = ext_scripts_redmine_policy.normalize_base_url(value)
        if normalized not in cleaned:
            cleaned.append(normalized)
    if cleaned:
        storage.write_json(path, {"schema_version": 1, "urls": cleaned})
    elif path.exists():
        path.unlink()


def clear_redmine_transaction_state(*, product_root: Path) -> None:
    path = redmine_transaction_state_path(product_root=product_root)
    if path.exists():
        path.unlink()


def recover_redmine_transaction(*, product_root: Path) -> list[dict[str, Any]]:
    """Finish cleanup after activation or roll back credentials before activation."""
    path = redmine_transaction_state_path(product_root=product_root)
    if not path.exists():
        return []
    try:
        transaction = storage.read_json(path)
        if not isinstance(transaction, dict) or transaction.get("schema_version") != 1:
            raise ValueError("invalid state")
        transaction_id = str(transaction.get("transaction_id", ""))
        backup_target = redmine_credentials.transaction_backup_target_name(transaction_id)
        transaction_phase = str(transaction.get("phase", "prepared"))
        if transaction_phase not in {"prepared", "activated"}:
            raise ValueError("invalid transaction phase")
        new_url = ext_scripts_redmine_policy.normalize_base_url(str(transaction.get("new_url", "")))
        old_url_raw = str(transaction.get("old_url", "") or "")
        old_url = (
            ext_scripts_redmine_policy.normalize_base_url(old_url_raw) if old_url_raw else None
        )
        old_url_invalid = transaction.get("old_url_invalid") is True
        credential_managed = transaction.get("credential_managed") is True
        credential_backup = transaction.get("credential_backup") is True
        _ = backup_target
        config_path = product_root / runtime.LOCAL_CONFIG_FILE
        local = storage.read_json(config_path, {})
        if not isinstance(local, dict):
            raise ValueError("invalid local configuration")
        active_raw = str(
            redmine_settings(local, product_root=product_root).get("base_url", "")
        ).strip()
        try:
            active_url = (
                ext_scripts_redmine_policy.normalize_base_url(active_raw) if active_raw else None
            )
        except (
            ext_scripts_redmine_client.RedmineError,
            ext_scripts_redmine_policy.RedminePolicyError,
        ):
            active_url = None
    except (WorkflowError, ValueError, TypeError):
        raise RedmineOperationError(
            "REDMINE_ROLLBACK_FAILED",
            "An interrupted Redmine transaction could not be read safely; no credential was changed.",
            recoverable=True,
            next_action="Inspect `.workspace/redmine-configure-transaction.json` and the active local configuration, then retry redmine cleanup.",
            previous_connection_preserved=True,
        ) from None
    if credential_managed and sys.platform != "win32":
        raise RedmineOperationError(
            "REDMINE_ROLLBACK_FAILED",
            "An interrupted transaction requires Windows Credential Manager recovery.",
            recoverable=True,
            next_action="Run a Redmine command on the Windows profile that owns the Credential Manager entries.",
            previous_connection_preserved=active_url != new_url,
        ) from None
    warnings: list[dict[str, Any]] = []
    retain_transaction = False
    pending_readable = True
    try:
        pending_urls = redmine_pending_cleanup_urls(product_root=product_root)
    except WorkflowError:
        pending_urls = []
        pending_readable = False
    transaction_activated = active_url == new_url and (
        old_url != new_url or transaction_phase == "activated"
    )
    if transaction_activated:
        if old_url_invalid:
            warnings.append(
                {
                    "code": "REDMINE_OLD_CREDENTIAL_CLEANUP_FAILED",
                    "message": "The new connection is active, but the previous invalid URL could not identify its saved credential for cleanup.",
                    "recoverable": True,
                    "next_action": "Review Windows Credential Manager after correcting the previous Redmine URL.",
                }
            )
        if old_url and old_url != new_url and (sys.platform == "win32"):
            try:
                redmine_credentials.delete_api_key(old_url)
                pending_urls = [value for value in pending_urls if value != old_url]
            except Exception:
                if old_url not in pending_urls:
                    pending_urls.append(old_url)
                retain_transaction = True
                warnings.append(
                    {
                        "code": "REDMINE_OLD_CREDENTIAL_CLEANUP_FAILED",
                        "message": "The new connection is active, but the previous saved credential could not be removed during recovery.",
                        "recoverable": True,
                        "next_action": "Run `redmine cleanup` to retry removal from the local recovery record.",
                    }
                )
        if pending_readable:
            try:
                save_redmine_pending_cleanup_urls(pending_urls, product_root=product_root)
                retain_transaction = False
            except Exception:
                retain_transaction = True
                warnings.append(
                    {
                        "code": "REDMINE_CLEANUP_STATE_WRITE_FAILED",
                        "message": "The connection is active, but the cleanup recovery record could not be updated.",
                        "recoverable": True,
                        "next_action": "Repair `.workspace/redmine-pending-cleanup.json` and run `redmine cleanup`.",
                    }
                )
        if not pending_readable:
            retain_transaction = True
        if credential_backup:
            try:
                redmine_credentials.delete_transaction_backup(transaction_id)
            except Exception:
                retain_transaction = True
                warnings.append(
                    {
                        "code": "REDMINE_CLEANUP_STATE_WRITE_FAILED",
                        "message": "The connection is active, but a temporary credential backup could not be removed.",
                        "recoverable": True,
                        "next_action": "Run `redmine cleanup` again on Windows to finish transaction recovery.",
                    }
                )
    elif credential_managed:
        try:
            backup = (
                redmine_credentials.read_transaction_backup(transaction_id)
                if credential_backup
                else None
            )
            if backup is not None:
                redmine_credentials.write_api_key(new_url, backup)
                if not redmine_credentials.verify_api_key(new_url, backup):
                    raise ext_scripts_redmine_client.RedmineError(
                        "Credential restoration could not be verified."
                    )
                redmine_credentials.delete_transaction_backup(transaction_id)
            elif not credential_backup:
                redmine_credentials.delete_api_key(new_url)
                if redmine_credentials.read_api_key(new_url) is not None:
                    raise ext_scripts_redmine_client.RedmineError(
                        "Credential rollback could not be verified."
                    )
        except Exception:
            raise RedmineOperationError(
                "REDMINE_ROLLBACK_FAILED",
                "The interrupted Redmine transaction could not restore its credential state.",
                recoverable=True,
                next_action="Check Windows Credential Manager access and rerun a Redmine command to retry recovery.",
                previous_connection_preserved=active_url != new_url,
            ) from None
    if not retain_transaction:
        try:
            clear_redmine_transaction_state(product_root=product_root)
        except OSError:
            warnings.append(
                {
                    "code": "REDMINE_CLEANUP_STATE_WRITE_FAILED",
                    "message": "Redmine transaction recovery completed, but its temporary state file could not be removed.",
                    "recoverable": True,
                    "next_action": "Run `redmine cleanup` again to remove the completed recovery record.",
                }
            )
    if not pending_readable:
        warnings.append(
            {
                "code": "REDMINE_CLEANUP_STATE_READ_FAILED",
                "message": "The interrupted transaction was recovered, but an existing credential cleanup record could not be read.",
                "recoverable": True,
                "next_action": "Repair `.workspace/redmine-pending-cleanup.json` and run `redmine cleanup`.",
            }
        )
    return warnings


def redmine_config_snapshot(*, product_root: Path) -> tuple[Path, bytes | None, dict[str, Any]]:
    path = product_root / runtime.LOCAL_CONFIG_FILE
    try:
        original = path.read_bytes() if path.exists() else None
        local = json.loads(original.decode("utf-8-sig")) if original is not None else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RedmineOperationError(
            "REDMINE_CONFIG_WRITE_FAILED",
            "The local configuration could not be read, so Redmine settings were left unchanged.",
            recoverable=True,
            next_action="Repair the local JSON configuration and retry redmine configure or disconnect.",
            previous_connection_preserved=True,
        ) from None
    if not isinstance(local, dict):
        raise RedmineOperationError(
            "REDMINE_CONFIG_WRITE_FAILED",
            "The local configuration must contain a JSON object; Redmine settings were left unchanged.",
            recoverable=True,
            next_action="Repair the local JSON configuration and retry the Redmine operation.",
            previous_connection_preserved=True,
        )
    return (path, original, storage.sanitize_json_value(local))


def restore_redmine_config_snapshot(path: Path, original: bytes | None) -> None:
    if original is None:
        if path.exists():
            path.unlink()
    else:
        storage.atomic_write_bytes(path, original)


def redmine_error_record(
    code: str, message: str, next_action: str, *, previous_connection_preserved: bool = True
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "recoverable": True,
        "next_action": next_action,
        "previous_connection_preserved": previous_connection_preserved,
    }


def raise_redmine_transaction_error(
    code: str,
    message: str,
    next_action: str,
    *,
    previous_connection_preserved: bool,
    rollback_failures: list[str] | None = None,
) -> None:
    if rollback_failures:
        original = redmine_error_record(
            code, message, next_action, previous_connection_preserved=previous_connection_preserved
        )
        recovery_action = "Inspect the reported state, restore the preserved connection if needed, and retry redmine cleanup."
        rollback_records = [
            redmine_error_record(
                "REDMINE_ROLLBACK_FAILED",
                detail,
                recovery_action,
                previous_connection_preserved=previous_connection_preserved,
            )
            for detail in rollback_failures
        ]
        raise RedmineOperationError(
            "REDMINE_ROLLBACK_FAILED",
            "Redmine setup failed and automatic rollback was incomplete.",
            recoverable=True,
            next_action=recovery_action,
            previous_connection_preserved=previous_connection_preserved,
            additional_errors=[original, *rollback_records],
        ) from None
    raise RedmineOperationError(
        code,
        message,
        recoverable=True,
        next_action=next_action,
        previous_connection_preserved=previous_connection_preserved,
    ) from None


def restore_redmine_credential(base_url: str, previous_value: str | None) -> None:
    if previous_value is None:
        redmine_credentials.delete_api_key(base_url)
        if redmine_credentials.read_api_key(base_url) is not None:
            raise ext_scripts_redmine_client.RedmineError(
                "Credential cleanup could not be verified."
            )
    else:
        redmine_credentials.write_api_key(base_url, previous_value)
        if not redmine_credentials.verify_api_key(base_url, previous_value):
            raise ext_scripts_redmine_client.RedmineError(
                "Credential restoration could not be verified."
            )


def previous_redmine_connection_preserved(
    previous_url: str | None, requested_url: str, rollback_failed: bool
) -> bool:
    return not rollback_failed or previous_url != requested_url


def redmine_configure(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    recovery_warnings = recover_redmine_transaction(product_root=product_root)
    if redmine_transaction_state_path(product_root=product_root).exists():
        raise RedmineOperationError(
            "REDMINE_ROLLBACK_FAILED",
            "A previous Redmine transaction still needs recovery; this configure request made no changes.",
            recoverable=True,
            next_action="Run `redmine cleanup` on Windows to finish recovery, then retry redmine configure.",
            previous_connection_preserved=True,
        ) from None
    config_path, original_config, local = redmine_config_snapshot(product_root=product_root)
    previous_settings = redmine_settings(local, product_root=product_root)
    raw_previous_url = str(previous_settings.get("base_url", "")).strip()
    previous_url: str | None = None
    previous_url_invalid = False
    if raw_previous_url:
        try:
            previous_url = ext_scripts_redmine_policy.normalize_base_url(raw_previous_url)
        except (
            ext_scripts_redmine_client.RedmineError,
            ext_scripts_redmine_policy.RedminePolicyError,
        ):
            previous_url_invalid = True
    try:
        base_url = ext_scripts_redmine_policy.normalize_base_url(args.url)
    except (
        ext_scripts_redmine_client.RedmineError,
        ext_scripts_redmine_policy.RedminePolicyError,
    ) as exc:
        raise_redmine_transaction_error(
            "REDMINE_VALIDATION_FAILED",
            str(exc),
            "Correct the HTTPS Redmine URL and retry; the existing configuration is unchanged.",
            previous_connection_preserved=True,
        )
    api_key = os.environ.get("FLOW1C_REDMINE_API_KEY", "").strip()
    save_to_credential_manager = not bool(api_key)
    if save_to_credential_manager:
        if sys.platform != "win32":
            raise_redmine_transaction_error(
                "REDMINE_VALIDATION_FAILED",
                "No Redmine API key source is available on this platform.",
                "Set FLOW1C_REDMINE_API_KEY in the agent environment and retry; the key is never accepted as a command-line argument or saved in project files.",
                previous_connection_preserved=True,
            )
        try:
            api_key = getpass.getpass("Redmine API key (input is hidden): ").strip()
        except (EOFError, OSError):
            raise_redmine_transaction_error(
                "REDMINE_VALIDATION_FAILED",
                "The hidden API-key prompt did not complete.",
                "Retry in an interactive terminal or set FLOW1C_REDMINE_API_KEY in the agent environment.",
                previous_connection_preserved=True,
            )
    if not api_key:
        raise_redmine_transaction_error(
            "REDMINE_VALIDATION_FAILED",
            "Redmine API key is empty.",
            "Provide a valid key through the hidden prompt or FLOW1C_REDMINE_API_KEY.",
            previous_connection_preserved=True,
        )
    try:
        client = ext_scripts_redmine_client.RedmineClient(base_url, api_key)
        client.current_user()
    except Exception:
        raise_redmine_transaction_error(
            "REDMINE_VALIDATION_FAILED",
            "Redmine did not verify the current user with the supplied API key.",
            "Check the HTTPS URL, API key, REST API setting, and account access; the previous connection is unchanged.",
            previous_connection_preserved=True,
        )
    previous_new_url_credential: str | None = None
    credential_snapshot_available = False
    if save_to_credential_manager:
        try:
            previous_new_url_credential = redmine_credentials.read_api_key(base_url)
            credential_snapshot_available = True
        except Exception:
            raise_redmine_transaction_error(
                "REDMINE_CREDENTIAL_WRITE_FAILED",
                "The existing credential state could not be checked, so no credential or configuration was changed.",
                "Check Windows Credential Manager access and retry; the previous connection is unchanged.",
                previous_connection_preserved=True,
            )
    transaction_id = uuid.uuid4().hex
    transaction_state = {
        "schema_version": 1,
        "transaction_id": transaction_id,
        "phase": "prepared",
        "old_url": previous_url,
        "old_url_invalid": previous_url_invalid,
        "new_url": base_url,
        "credential_managed": save_to_credential_manager,
        "credential_backup": save_to_credential_manager and previous_new_url_credential is not None,
    }
    try:
        storage.write_json(
            redmine_transaction_state_path(product_root=product_root), transaction_state
        )
    except Exception:
        raise_redmine_transaction_error(
            "REDMINE_CONFIG_WRITE_FAILED",
            "Redmine transaction recovery state could not be written; no credential or configuration was changed.",
            "Check `.workspace` permissions and retry; the previous connection is unchanged.",
            previous_connection_preserved=True,
        )
    backup_staged = False
    if transaction_state["credential_backup"]:
        try:
            redmine_credentials.write_transaction_backup(
                transaction_id, previous_new_url_credential or ""
            )
            backup_staged = True
        except Exception:
            rollback_failures: list[str] = []
            try:
                redmine_credentials.delete_transaction_backup(transaction_id)
            except Exception:
                rollback_failures.append("The temporary credential backup could not be removed.")
            try:
                clear_redmine_transaction_state(product_root=product_root)
            except Exception:
                rollback_failures.append("The interrupted transaction record could not be removed.")
            raise_redmine_transaction_error(
                "REDMINE_CREDENTIAL_WRITE_FAILED",
                "The existing credential could not be staged safely for recovery.",
                "Check Windows Credential Manager access and retry; the previous connection was preserved.",
                previous_connection_preserved=True,
                rollback_failures=rollback_failures,
            )
    credential_write_attempted = False
    if save_to_credential_manager:
        credential_write_attempted = True
        try:
            redmine_credentials.write_api_key(base_url, api_key)
        except Exception:
            failures: list[str] = []
            credential_restored = False
            try:
                restore_redmine_credential(base_url, previous_new_url_credential)
                credential_restored = True
            except Exception:
                failures.append(
                    "The credential for the requested URL could not be restored or removed."
                )
            if credential_restored and backup_staged:
                try:
                    redmine_credentials.delete_transaction_backup(transaction_id)
                except Exception:
                    failures.append("The temporary credential backup could not be removed.")
            if credential_restored and (not failures):
                try:
                    clear_redmine_transaction_state(product_root=product_root)
                except Exception:
                    failures.append("The interrupted transaction record could not be removed.")
            raise_redmine_transaction_error(
                "REDMINE_CREDENTIAL_WRITE_FAILED",
                "Windows Credential Manager could not save the new API key.",
                "Check Credential Manager access and retry; the previous connection was preserved.",
                previous_connection_preserved=previous_redmine_connection_preserved(
                    previous_url, base_url, not credential_restored
                ),
                rollback_failures=failures,
            )
        try:
            verified = redmine_credentials.verify_api_key(base_url, api_key)
        except Exception:
            verified = False
        if not verified:
            failures = []
            credential_restored = False
            try:
                restore_redmine_credential(base_url, previous_new_url_credential)
                credential_restored = True
            except Exception:
                failures.append(
                    "The credential for the requested URL could not be restored or removed."
                )
            if credential_restored and backup_staged:
                try:
                    redmine_credentials.delete_transaction_backup(transaction_id)
                except Exception:
                    failures.append("The temporary credential backup could not be removed.")
            if credential_restored and (not failures):
                try:
                    clear_redmine_transaction_state(product_root=product_root)
                except Exception:
                    failures.append("The interrupted transaction record could not be removed.")
            raise_redmine_transaction_error(
                "REDMINE_CREDENTIAL_VERIFY_FAILED",
                "The saved API key could not be read back and verified.",
                "Check Credential Manager access and retry; the previous connection was preserved.",
                previous_connection_preserved=previous_redmine_connection_preserved(
                    previous_url, base_url, not credential_restored
                ),
                rollback_failures=failures,
            )
    updated_local = dict(local)
    updated_settings = dict(previous_settings)
    updated_settings["base_url"] = base_url
    updated_local["redmine"] = updated_settings
    config_changed = updated_local != local
    try:
        if config_changed:
            storage.write_json(config_path, updated_local)
        transaction_state["phase"] = "activated"
        storage.write_json(
            redmine_transaction_state_path(product_root=product_root), transaction_state
        )
    except Exception:
        rollback_failures = []
        config_restored = False
        try:
            restore_redmine_config_snapshot(config_path, original_config)
            config_restored = True
        except Exception:
            rollback_failures.append("The original local configuration could not be restored.")
        credential_restored = not credential_write_attempted
        if config_restored and credential_write_attempted and credential_snapshot_available:
            try:
                restore_redmine_credential(base_url, previous_new_url_credential)
                credential_restored = True
            except Exception:
                rollback_failures.append(
                    "The newly written credential could not be restored or removed."
                )
        if config_restored and credential_restored and backup_staged:
            try:
                redmine_credentials.delete_transaction_backup(transaction_id)
            except Exception:
                rollback_failures.append("The temporary credential backup could not be removed.")
        if config_restored and credential_restored and (not rollback_failures):
            try:
                clear_redmine_transaction_state(product_root=product_root)
            except Exception:
                rollback_failures.append("The interrupted transaction record could not be removed.")
        raise_redmine_transaction_error(
            "REDMINE_CONFIG_WRITE_FAILED",
            "The Redmine configuration could not be activated; rollback was attempted.",
            "Retry after checking local file permissions; inspect the configuration and Credential Manager if rollback failed.",
            previous_connection_preserved=not rollback_failures
            or (
                "The original local configuration could not be restored." not in rollback_failures
                and previous_url != base_url
            ),
            rollback_failures=rollback_failures,
        )
    warnings: list[dict[str, Any]] = list(recovery_warnings)
    pending_state_readable = True
    try:
        pending_urls = redmine_pending_cleanup_urls(product_root=product_root)
    except WorkflowError:
        pending_urls = []
        pending_state_readable = False
        warnings.append(
            {
                "code": "REDMINE_CLEANUP_STATE_READ_FAILED",
                "message": "The new connection is active, but the previous credential cleanup record could not be read.",
                "recoverable": True,
                "next_action": "Repair `.workspace/redmine-pending-cleanup.json` and run `redmine cleanup`.",
            }
        )
    if previous_url_invalid and raw_previous_url:
        warnings.append(
            {
                "code": "REDMINE_OLD_CREDENTIAL_CLEANUP_FAILED",
                "message": "The new connection is active, but the previous URL was invalid and its saved credential could not be identified for cleanup.",
                "recoverable": True,
                "next_action": "Review the previous Redmine credential in Windows Credential Manager after correcting its URL.",
            }
        )
    elif previous_url and previous_url != base_url and (sys.platform == "win32"):
        try:
            redmine_credentials.delete_api_key(previous_url)
            pending_urls = [value for value in pending_urls if value != previous_url]
        except Exception:
            if previous_url not in pending_urls:
                pending_urls.append(previous_url)
            warnings.append(
                {
                    "code": "REDMINE_OLD_CREDENTIAL_CLEANUP_FAILED",
                    "message": "The new connection is active, but the previous saved credential could not be removed.",
                    "recoverable": True,
                    "next_action": "Run `redmine cleanup` to retry removal from the local recovery record.",
                }
            )
    pending_state_saved = False
    try:
        if pending_state_readable:
            save_redmine_pending_cleanup_urls(pending_urls, product_root=product_root)
            pending_state_saved = True
    except Exception:
        if not any((item["code"] == "REDMINE_OLD_CREDENTIAL_CLEANUP_FAILED" for item in warnings)):
            warnings.append(
                {
                    "code": "REDMINE_CLEANUP_STATE_WRITE_FAILED",
                    "message": "The new connection is active, but the cleanup recovery record could not be updated.",
                    "recoverable": True,
                    "next_action": "Review the old credential in Windows Credential Manager; do not repeat configure solely to repair the record.",
                }
            )
    if (
        pending_urls
        and pending_state_readable
        and (
            not any(
                (item.get("code") == "REDMINE_OLD_CREDENTIAL_CLEANUP_FAILED" for item in warnings)
            )
        )
    ):
        warnings.append(
            {
                "code": "REDMINE_OLD_CREDENTIAL_CLEANUP_FAILED",
                "message": "One or more previous saved Redmine credentials are still pending cleanup.",
                "recoverable": True,
                "next_action": "Run `redmine cleanup` to retry removal from the local recovery record.",
            }
        )
    keep_transaction = not pending_state_saved
    if backup_staged:
        try:
            redmine_credentials.delete_transaction_backup(transaction_id)
        except Exception:
            keep_transaction = True
            warnings.append(
                {
                    "code": "REDMINE_CLEANUP_STATE_WRITE_FAILED",
                    "message": "The new connection is active, but its temporary credential backup could not be removed.",
                    "recoverable": True,
                    "next_action": "Run a Redmine command again to retry transaction cleanup.",
                }
            )
    if not keep_transaction:
        try:
            clear_redmine_transaction_state(product_root=product_root)
        except OSError:
            warnings.append(
                {
                    "code": "REDMINE_CLEANUP_STATE_WRITE_FAILED",
                    "message": "The new connection is active, but its transaction recovery file could not be removed.",
                    "recoverable": True,
                    "next_action": "Run a Redmine command again to retry transaction cleanup.",
                }
            )
    state = "CONNECTED_WITH_WARNING" if warnings else "CONNECTED"
    _value = {
        "schema_version": 1,
        "state": state,
        "configured": True,
        "verified": True,
        "previous_connection_preserved": True,
        "warnings": warnings,
        "next_action": warnings[0]["next_action"] if warnings else None,
        "base_url": base_url,
        "credential_source": (
            "environment" if not save_to_credential_manager else "windows-credential-manager"
        ),
        "next": "The agent can now fetch issues and import their supported attachments.",
    }
    return OperationResult(_value, 0)


def redmine_status(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    recovery_warnings = recover_redmine_transaction(product_root=product_root)
    settings = redmine_settings(product_root=product_root)
    raw_url = str(settings.get("base_url", "")).strip()
    if not raw_url:
        result = {
            "state": "NOT_CONFIGURED",
            "configured": False,
            "next_action": "Run redmine configure with the Redmine HTTPS URL.",
        }
    else:
        try:
            base_url = ext_scripts_redmine_policy.normalize_base_url(raw_url)
        except (
            ext_scripts_redmine_client.RedmineError,
            ext_scripts_redmine_policy.RedminePolicyError,
        ) as exc:
            result = {"state": "INVALID", "configured": True, "error": str(exc)}
        else:
            try:
                _, source = redmine_api_key(base_url)
                result = {
                    "state": "READY",
                    "configured": True,
                    "base_url": base_url,
                    "credential_source": source,
                }
            except WorkflowError as exc:
                result = {
                    "state": "NEEDS_SECRET",
                    "configured": True,
                    "base_url": base_url,
                    "error": str(exc),
                }
    warnings = list(recovery_warnings)
    try:
        pending_urls = redmine_pending_cleanup_urls(product_root=product_root)
    except WorkflowError:
        pending_urls = []
        warnings.append(
            {
                "code": "REDMINE_CLEANUP_STATE_READ_FAILED",
                "message": "The Redmine credential cleanup record could not be read.",
                "recoverable": True,
                "next_action": "Repair `.workspace/redmine-pending-cleanup.json` and run `redmine cleanup`.",
            }
        )
    if pending_urls and (
        not any((item.get("code") == "REDMINE_OLD_CREDENTIAL_CLEANUP_FAILED" for item in warnings))
    ):
        warnings.append(
            {
                "code": "REDMINE_OLD_CREDENTIAL_CLEANUP_FAILED",
                "message": "One or more previous saved Redmine credentials are still pending cleanup.",
                "recoverable": True,
                "next_action": "Run `redmine cleanup` to retry removal from the local recovery record.",
            }
        )
    if warnings:
        result["warnings"] = warnings
    _value = result
    return OperationResult(_value, 0 if result["state"] == "READY" else 1)


def redmine_test(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    client, base_url, source = redmine_client_from_config(product_root=product_root)
    try:
        client.current_user()
    except ext_scripts_redmine_client.RedmineError as exc:
        raise WorkflowError(str(exc)) from exc
    result = {
        "state": "CONNECTED",
        "base_url": base_url,
        "credential_source": source,
        "verified": True,
    }
    if getattr(client, "recovery_warnings", None):
        result["warnings"] = client.recovery_warnings
    _value = result
    return OperationResult(_value, 0)


def redmine_issue_summary(issue: dict[str, Any], base_url: str) -> dict[str, Any]:
    description = storage.sanitize_text(str(issue.get("description", "")))
    subject = storage.sanitize_text(str(issue.get("subject", "")))
    description_limit = 12000
    subject_limit = 500
    return {
        "id": issue["id"],
        "url": f"{base_url}/issues/{issue['id']}",
        "subject": subject[:subject_limit],
        "subject_truncated": len(subject) > subject_limit,
        "description": description[:description_limit],
        "description_truncated": len(description) > description_limit,
        "status": storage.sanitize_text(str(issue.get("status", "")))[:200],
        "project": storage.sanitize_text(str(issue.get("project", "")))[:200],
    }


def redmine_standard_attachment_summaries(issue: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for attachment in issue.get("attachments", []):
        if not isinstance(attachment, dict):
            continue
        result.append(
            {
                "id": attachment.get("id"),
                "name": storage.sanitize_text(str(attachment.get("filename") or "attachment"))[
                    :500
                ],
                "size": (
                    attachment.get("filesize")
                    if isinstance(attachment.get("filesize"), int)
                    else None
                ),
                "mime_type": storage.sanitize_text(
                    str(attachment.get("content_type") or "application/octet-stream")
                )[:128],
            }
        )
    return result


def redmine_dms_error(exc: ext_scripts_redmine_client.RedmineError) -> dict[str, Any]:
    return {
        "code": exc.code,
        "message": storage.sanitize_text(str(exc))[:1000],
        "recoverable": exc.code not in {"REDMINE_DMSF_INVALID_ID", "REDMINE_DMSF_NOT_ATTACHED"},
        "next_action": "Check the Redmine issue, DMSF permissions and file metadata, then retry the same selection.",
    }


def redmine_upload_error(exc: ext_scripts_redmine_client.RedmineError) -> dict[str, Any]:
    """Return a stable, secret-free error record for the confirmed DMSF write path."""
    next_actions = {
        "REDMINE_DMSF_UPLOAD_NOT_CONFIRMED": "Ask for explicit confirmation of this exact issue and local file, then retry with --confirmed.",
        "REDMINE_DMSF_UPLOAD_FILE_NOT_FOUND": "Check the absolute local file path and select the intended ordinary file.",
        "REDMINE_DMSF_UPLOAD_UNSUPPORTED_TYPE": "Choose a file with an extension allowed by the DMSF upload policy.",
        "REDMINE_DMSF_UPLOAD_SIZE_LIMIT": "Choose a file no larger than 500 MiB and check the Redmine server limit.",
        "REDMINE_DMSF_UPLOAD_ACCESS_DENIED": "Check issue visibility and the API user's DMSF upload and issue-link permissions.",
        "REDMINE_DMSF_UNAVAILABLE": "Check that DMSF REST API and issue attachment support are enabled for this project.",
        "REDMINE_DMSF_ISSUE_ATTACH_UNAVAILABLE": "Enable DMSF attachments for this exact issue/project or upgrade the plugin before retrying.",
        "REDMINE_DMSF_UPLOAD_NAME_CONFLICT": "Choose a different file name or obtain a separate explicit revision decision.",
        "REDMINE_DMSF_COMMIT_UNCERTAIN": "Inspect Redmine and DMSF manually; do not retry automatically.",
        "REDMINE_DMSF_UPLOAD_VERIFICATION_FAILED": "Inspect the issue and DMSF manually; do not retry automatically.",
    }
    return {
        "code": exc.code,
        "message": storage.sanitize_text(str(exc))[:1000],
        "next_action": next_actions.get(
            exc.code,
            "Check Redmine availability and permissions, then retry only when the result is known.",
        ),
    }


def redmine_upload(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    try:
        issue_id = ext_scripts_redmine_policy.issue_number(args.issue)
    except ext_scripts_redmine_policy.RedminePolicyError as exc:
        raise WorkflowError(str(exc)) from exc
    requested_path = Path(str(args.file)).expanduser()
    try:
        absolute_path = requested_path.resolve(strict=False)
    except OSError:
        absolute_path = requested_path.absolute()
    issue_summary: dict[str, Any] = {"id": issue_id}
    project_id: int | None = None
    local_file: dict[str, Any] = {
        "path": str(absolute_path),
        "name": absolute_path.name,
        "size": None,
    }
    try:
        client, base_url, _ = redmine_client_from_config(recover=False, product_root=product_root)
        issue = client.issue(issue_id)
        issue_summary = redmine_issue_summary(issue, base_url)
        project_id = issue.get("project_id") if isinstance(issue.get("project_id"), int) else None
        upload = client.upload_dmsf_file(issue, absolute_path, confirmed=bool(args.confirmed))
        preflight = upload.get("preflight", {}) if isinstance(upload, dict) else {}
        local_file = {
            "path": str(preflight.get("path", absolute_path)),
            "name": str(preflight.get("name", absolute_path.name)),
            "size": preflight.get("size"),
        }
        project_id = preflight.get("project_id", project_id)
        result: dict[str, Any] = {
            "schema_version": 1,
            "state": upload.get("state", "ERROR"),
            "issue": issue_summary,
            "project_id": project_id,
            "local_file": local_file,
            "dmsf_file": upload.get("dms_file"),
            "verification": upload.get(
                "verification", {"issue_reloaded": False, "dmsf_attached": False}
            ),
            "warnings": upload.get("warnings", []),
            "errors": upload.get("errors", []),
        }
        if result["state"] == "NEEDS_CONFIRMATION":
            result["next_action"] = (
                "After explicit consent for this exact issue and file, repeat the command with --confirmed. No Redmine change was made."
            )
            exit_code = 1
        elif result["state"] == "UPLOADED":
            result["next_action"] = None
            exit_code = 0
        else:
            result["next_action"] = (
                upload.get("next_action")
                or "Inspect Redmine and DMSF manually; do not retry automatically."
            )
            exit_code = 2
    except ext_scripts_redmine_client.RedmineError as exc:
        result = {
            "schema_version": 1,
            "state": "ERROR",
            "issue": issue_summary,
            "project_id": project_id,
            "local_file": local_file,
            "dmsf_file": None,
            "verification": {"issue_reloaded": False, "dmsf_attached": False},
            "warnings": [],
            "errors": [redmine_upload_error(exc)],
            "next_action": redmine_upload_error(exc)["next_action"],
        }
        exit_code = 2
    _value = result
    return OperationResult(_value, exit_code)


def redmine_revise(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    """Update the revision of one explicitly selected issue DMSF document."""
    try:
        issue_id = ext_scripts_redmine_policy.issue_number(args.issue)
        file_id = ext_scripts_redmine_policy.dmsf_identifier(args.dms_file, label="DMSF file ID")
        previous_id = ext_scripts_redmine_policy.dmsf_identifier(
            args.expected_revision, label="DMSF revision ID"
        )
    except ext_scripts_redmine_policy.RedminePolicyError as exc:
        raise WorkflowError(str(exc)) from exc
    result: dict[str, Any] = {
        "schema_version": 1,
        "state": "ERROR",
        "issue": {"id": issue_id},
        "dmsf_file": {"id": file_id},
        "local_file": {"path": str(Path(args.file).resolve(strict=False))},
    }
    try:
        client, base_url, _ = redmine_client_from_config(recover=False, product_root=product_root)
        issue = client.issue(issue_id)
        result["issue"] = redmine_issue_summary(issue, base_url)
        revision = client.revise_dmsf_file(
            issue,
            file_id,
            args.file,
            expected_revision_id=previous_id,
            confirmed=bool(args.confirmed),
        )
        result.update(
            {
                "state": revision["state"],
                "preflight": revision.get("preflight"),
                "dmsf_file": revision.get("dms_file", {"id": file_id}),
                "verification": revision.get("verification"),
                "errors": revision.get("errors", []),
            }
        )
        result["next_action"] = (
            "Confirm the exact file, issue and existing DMSF ID before using --confirmed."
            if revision["state"] == "NEEDS_CONFIRMATION"
            else (
                "Inspect the issue and DMSF manually; do not retry automatically."
                if revision["state"] == "COMMITTED_UNVERIFIED"
                else None
            )
        )
        exit_code = 0 if revision["state"] == "REVISED" else 1
    except ext_scripts_redmine_client.RedmineError as exc:
        result["errors"] = [redmine_upload_error(exc)]
        result["next_action"] = result["errors"][0]["next_action"]
        exit_code = 2
    _value = result
    return OperationResult(_value, exit_code)


def redmine_dms_inventory(
    client: Any, issue: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if hasattr(client, "dmsf_file_inventory"):
        try:
            files, failures = client.dmsf_file_inventory(issue)
            return (
                files,
                [
                    {
                        "code": str(item.get("code", "REDMINE_DMSF_INVALID_METADATA")),
                        "message": storage.sanitize_text(
                            str(item.get("message", "DMSF metadata could not be read"))
                        )[:1000],
                        "recoverable": True,
                        "next_action": "Check DMSF permissions and file metadata, then retry the same issue.",
                        **(
                            {"dms_file_id": item["dms_file_id"]}
                            if item.get("dms_file_id") is not None
                            else {}
                        ),
                    }
                    for item in failures
                ],
            )
        except ext_scripts_redmine_client.RedmineError as exc:
            return ([], [redmine_dms_error(exc)])
    try:
        return (
            (client.current_dmsf_files(issue), [])
            if hasattr(client, "current_dmsf_files")
            else ([], [])
        )
    except ext_scripts_redmine_client.RedmineError as exc:
        return ([], [redmine_dms_error(exc)])


def redmine_files(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    try:
        issue_id = ext_scripts_redmine_policy.issue_number(args.issue)
    except ext_scripts_redmine_policy.RedminePolicyError as exc:
        raise WorkflowError(str(exc)) from exc
    client, base_url, _ = redmine_client_from_config(recover=False, product_root=product_root)
    try:
        issue = client.issue(issue_id)
    except ext_scripts_redmine_client.RedmineError as exc:
        result = {
            "schema_version": 1,
            "state": "ERROR",
            "issue": {"id": issue_id},
            "errors": [redmine_dms_error(exc)],
        }
        _value = result
        return OperationResult(_value, 2)
    standard = redmine_standard_attachment_summaries(issue)
    dms_files: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    dms_files, warnings = redmine_dms_inventory(client, issue)
    if any((item["code"] == "REDMINE_DMSF_UNAVAILABLE" for item in warnings)):
        state = "DMSF_NOT_AVAILABLE"
    elif not standard and (not dms_files):
        state = "ERROR" if warnings else "NO_FILES"
    elif dms_files:
        state = "SELECTION_REQUIRED"
    else:
        state = "FILES_AVAILABLE"
    result = {
        "schema_version": 1,
        "state": state,
        "issue": {"id": issue_id, "url": f"{base_url}/issues/{issue_id}"},
        "standard_attachments": standard,
        "dms_files": dms_files,
        "warnings": warnings,
    }
    _value = result
    return OperationResult(_value, 1 if warnings else 0)


def redmine_relations(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    try:
        issue_id = ext_scripts_redmine_policy.issue_number(args.issue)
    except ext_scripts_redmine_policy.RedminePolicyError as exc:
        raise WorkflowError(str(exc)) from exc
    client, _, _ = redmine_client_from_config(recover=False, product_root=product_root)
    try:
        inventory = client.related_issues(issue_id)
    except ext_scripts_redmine_client.RedmineError as exc:
        result = {
            "schema_version": 1,
            "state": "ERROR",
            "issue": {"id": issue_id},
            "relation_count": 0,
            "relations": [],
            "errors": [{"code": exc.code, "message": storage.sanitize_text(str(exc))[:1000]}],
        }
        _value = result
        return OperationResult(_value, 2)
    partial = any((record["error"] is not None for record in inventory["relations"]))
    result = {
        "schema_version": 1,
        "state": "PARTIAL" if partial else "COMPLETE",
        "issue": inventory["issue"],
        "relation_count": len(inventory["relations"]),
        "relations": inventory["relations"],
    }
    _value = result
    return OperationResult(_value, 1 if partial else 0)


def redmine_disconnect(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    recover_redmine_transaction(product_root=product_root)
    config_path, original_config, local = redmine_config_snapshot(product_root=product_root)
    settings = redmine_settings(local, product_root=product_root)
    raw_url = str(settings.get("base_url", "")).strip()
    had_redmine_section = "redmine" in local
    active_url: str | None = None
    invalid_url = False
    if raw_url:
        try:
            active_url = ext_scripts_redmine_policy.normalize_base_url(raw_url)
        except (
            ext_scripts_redmine_client.RedmineError,
            ext_scripts_redmine_policy.RedminePolicyError,
        ):
            invalid_url = True
    local.pop("redmine", None)
    try:
        if had_redmine_section:
            storage.write_json(config_path, local)
    except Exception:
        rollback_failures = []
        try:
            restore_redmine_config_snapshot(config_path, original_config)
        except Exception:
            rollback_failures.append("The original local configuration could not be restored.")
        raise_redmine_transaction_error(
            "REDMINE_CONFIG_WRITE_FAILED",
            "The Redmine section could not be removed; the saved credential was left untouched.",
            "Check local file permissions and retry redmine disconnect.",
            previous_connection_preserved=not rollback_failures,
            rollback_failures=rollback_failures,
        )
    pending_state_readable = True
    try:
        candidates = redmine_pending_cleanup_urls(product_root=product_root)
    except WorkflowError:
        candidates = []
        pending_state_readable = False
    if active_url and active_url not in candidates:
        candidates.append(active_url)
    failed: list[str] = []
    removed_any = False
    warnings: list[dict[str, Any]] = []
    if invalid_url:
        warnings.append(
            {
                "code": "REDMINE_OLD_CREDENTIAL_CLEANUP_FAILED",
                "message": "The local Redmine configuration was removed, but its invalid previous URL could not identify a credential to remove.",
                "recoverable": True,
                "next_action": "Review Windows Credential Manager for the Flow1C Redmine entry associated with the previous URL.",
            }
        )
    for candidate in candidates if sys.platform == "win32" and pending_state_readable else []:
        try:
            removed_any = redmine_credentials.delete_api_key(candidate) or removed_any
        except Exception:
            failed.append(candidate)
    if failed:
        warnings.append(
            {
                "code": "REDMINE_OLD_CREDENTIAL_CLEANUP_FAILED",
                "message": "Redmine is disconnected, but one or more saved credentials could not be removed.",
                "recoverable": True,
                "next_action": "Run `redmine cleanup` to retry removal from the local recovery record.",
            }
        )
    try:
        if pending_state_readable and sys.platform == "win32":
            save_redmine_pending_cleanup_urls(failed, product_root=product_root)
        elif not pending_state_readable:
            warnings.append(
                {
                    "code": "REDMINE_CLEANUP_STATE_READ_FAILED",
                    "message": "Redmine is disconnected, but the pending credential cleanup record could not be read.",
                    "recoverable": True,
                    "next_action": "Repair `.workspace/redmine-pending-cleanup.json` and run `redmine cleanup`.",
                }
            )
    except Exception:
        warnings.append(
            {
                "code": "REDMINE_CLEANUP_STATE_WRITE_FAILED",
                "message": "Redmine is disconnected, but the credential cleanup recovery record could not be updated.",
                "recoverable": True,
                "next_action": "Review Windows Credential Manager and remove any remaining Flow1C Redmine credential manually.",
            }
        )
    if redmine_transaction_state_path(product_root=product_root).exists():
        warnings.append(
            {
                "code": "REDMINE_CLEANUP_STATE_WRITE_FAILED",
                "message": "Redmine is disconnected, but an interrupted configure transaction still needs recovery.",
                "recoverable": True,
                "next_action": "Run `redmine cleanup` on Windows to finish transaction recovery.",
            }
        )
    env_key_present = bool(os.environ.get("FLOW1C_REDMINE_API_KEY", "").strip())
    if env_key_present:
        warnings.append(
            {
                "code": "REDMINE_ENVIRONMENT_KEY_PRESERVED",
                "message": "FLOW1C_REDMINE_API_KEY remains managed by the agent environment and was not changed.",
                "recoverable": True,
                "next_action": "Remove FLOW1C_REDMINE_API_KEY from the agent environment separately if it should no longer be available.",
            }
        )
    state = "DISCONNECTED_WITH_WARNING" if warnings else "DISCONNECTED"
    _value = {
        "schema_version": 1,
        "state": state,
        "configured": False,
        "verified": False,
        "previous_connection_preserved": False,
        "credential_removed": removed_any,
        "environment_key_cleared": False,
        "warnings": warnings,
        "next_action": warnings[0]["next_action"] if warnings else None,
    }
    return OperationResult(_value, 0)


def redmine_cleanup(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    recovery_warnings = recover_redmine_transaction(product_root=product_root)
    requested_url = str(getattr(args, "url", "") or "").strip()
    try:
        candidates = redmine_pending_cleanup_urls(product_root=product_root)
        if requested_url:
            normalized = ext_scripts_redmine_policy.normalize_base_url(requested_url)
            if normalized not in candidates:
                candidates.append(normalized)
    except (
        ext_scripts_redmine_client.RedmineError,
        ext_scripts_redmine_policy.RedminePolicyError,
    ) as exc:
        raise RedmineOperationError(
            "REDMINE_VALIDATION_FAILED",
            str(exc),
            recoverable=True,
            next_action="Provide a valid HTTPS Redmine URL to retry credential cleanup.",
            previous_connection_preserved=False,
        ) from None
    except WorkflowError:
        raise RedmineOperationError(
            "REDMINE_CLEANUP_STATE_READ_FAILED",
            "The pending Redmine credential cleanup record could not be read.",
            recoverable=True,
            next_action="Repair `.workspace/redmine-pending-cleanup.json` or pass `--url` to retry a specific credential.",
            previous_connection_preserved=bool(
                redmine_settings(product_root=product_root).get("base_url")
            ),
        ) from None
    if sys.platform != "win32":
        warnings = [
            {
                "code": "REDMINE_CREDENTIAL_WRITE_FAILED",
                "message": "Windows Credential Manager cleanup is available only on Windows.",
                "recoverable": True,
                "next_action": "Run redmine cleanup on the Windows profile that owns the credential.",
            }
        ]
        _value = {
            "schema_version": 1,
            "state": "CLEANUP_WITH_WARNING",
            "configured": bool(redmine_settings(product_root=product_root).get("base_url")),
            "credential_removed": 0,
            "warnings": warnings,
            "next_action": warnings[0]["next_action"],
        }
        return OperationResult(_value, 1)
    failed: list[str] = []
    removed = 0
    warnings: list[dict[str, Any]] = list(recovery_warnings)
    for candidate in candidates:
        try:
            removed += int(bool(redmine_credentials.delete_api_key(candidate)))
        except Exception:
            failed.append(candidate)
    try:
        save_redmine_pending_cleanup_urls(failed, product_root=product_root)
    except Exception:
        warnings.append(
            {
                "code": "REDMINE_CLEANUP_STATE_WRITE_FAILED",
                "message": "Credential cleanup ran, but the recovery record could not be updated.",
                "recoverable": True,
                "next_action": "Retry redmine cleanup after checking local file permissions.",
            }
        )
    if failed:
        warnings.append(
            {
                "code": "REDMINE_OLD_CREDENTIAL_CLEANUP_FAILED",
                "message": "One or more saved Redmine credentials remain and are recorded for retry.",
                "recoverable": True,
                "next_action": "Check Windows Credential Manager access and retry redmine cleanup.",
            }
        )
    elif not redmine_transaction_state_path(product_root=product_root).exists():
        warnings = []
    _value = {
        "schema_version": 1,
        "state": "CLEANUP_WITH_WARNING" if warnings else "CLEANUP_COMPLETE",
        "configured": bool(redmine_settings(product_root=product_root).get("base_url")),
        "credential_removed": removed,
        "warnings": warnings,
        "next_action": warnings[0]["next_action"] if warnings else None,
    }
    return OperationResult(_value, 1 if warnings else 0)


def redmine_fetch(args: argparse.Namespace, *, product_root: Path) -> OperationResult:
    try:
        issue_id = ext_scripts_redmine_policy.issue_number(args.issue)
    except ext_scripts_redmine_policy.RedminePolicyError as exc:
        raise WorkflowError(str(exc)) from exc
    supplied_code = str(getattr(args, "code", "") or "").strip() or None
    gate_id = str(getattr(args, "gate_id", "") or "").strip() or None
    code: str | None = None
    if gate_id:
        gate = gate_state.load_gate(
            gate_id,
            states={
                "NEEDS_INPUT",
                "NEEDS_CONFIRMATION",
                "WAITING_USER",
                "READY",
                "READY_WITH_DEVIATIONS",
                "UNVERIFIED_DRAFT",
                "BLOCKED",
            },
            product_root=product_root,
        )
        gate_state.require_gate_tool(gate, "flow1c_redmine_fetch", product_root=product_root)
        code = str(gate.get("work_reference") or gate.get("code") or "").strip() or None
        if supplied_code and supplied_code != code:
            raise WorkflowError("Redmine target does not match the active gate work item")
    elif supplied_code:
        raise WorkflowError(
            "Importing Redmine attachments into a work item requires an active gate_id"
        )
    if code:
        work_items.load_manifest(code, product_root=product_root)
    selected_values = list(getattr(args, "dms_file", []) or [])
    all_dms = bool(getattr(args, "all_dms", False))
    revision_id = getattr(args, "dms_revision", None)
    if all_dms and selected_values:
        raise WorkflowError("--all-dms cannot be combined with --dms-file")
    if revision_id is not None and len(selected_values) != 1:
        raise WorkflowError("--dms-revision requires exactly one --dms-file")
    try:
        selected_ids = [
            ext_scripts_redmine_policy.dmsf_identifier(value) for value in selected_values
        ]
        revision_id = (
            ext_scripts_redmine_policy.dmsf_identifier(revision_id, label="DMSF revision ID")
            if revision_id is not None
            else None
        )
    except ext_scripts_redmine_policy.RedminePolicyError as exc:
        raise WorkflowError(str(exc)) from exc
    if len(set(selected_ids)) != len(selected_ids):
        raise WorkflowError("A DMSF file may be selected only once.")
    client, base_url, credential_source = redmine_client_from_config(product_root=product_root)
    try:
        issue = client.issue(issue_id)
    except ext_scripts_redmine_client.RedmineError as exc:
        raise WorkflowError(str(exc)) from exc
    issue_summary = redmine_issue_summary(issue, base_url)
    dms_files: list[dict[str, Any]] = []
    dms_warnings: list[dict[str, Any]] = []
    dms_files, dms_warnings = redmine_dms_inventory(client, issue)
    active_by_id = {item.get("id"): item for item in dms_files}
    if selected_ids and any((file_id not in active_by_id for file_id in selected_ids)):
        missing = next((file_id for file_id in selected_ids if file_id not in active_by_id))
        error = next((item for item in dms_warnings if item.get("dms_file_id") == missing), None)
        if error is None:
            error = next((item for item in dms_warnings if item.get("dms_file_id") is None), None)
        if error is None:
            error = redmine_dms_error(
                ext_scripts_redmine_client.RedmineError(
                    f"DMSF file {missing} is not currently attached to issue {issue_id}.",
                    code="REDMINE_DMSF_NOT_ATTACHED",
                )
            )
        _value = {"schema_version": 1, "state": "ERROR", "issue": issue_summary, "errors": [error]}
        return OperationResult(_value, 2)
    if revision_id is not None:
        selected_metadata = active_by_id[selected_ids[0]]
        available_revisions = {
            item.get("id")
            for item in selected_metadata.get("revisions", [])
            if isinstance(item, dict)
        }
        if revision_id not in available_revisions:
            error = ext_scripts_redmine_client.RedmineError(
                "Requested DMSF revision is not available for this file.",
                code="REDMINE_DMSF_INVALID_METADATA",
            )
            _value = {
                "schema_version": 1,
                "state": "ERROR",
                "issue": issue_summary,
                "errors": [redmine_dms_error(error)],
            }
            return OperationResult(_value, 2)
    requested_dms = (
        list(active_by_id.values())
        if all_dms
        else [active_by_id[file_id] for file_id in selected_ids]
    )
    if not issue["attachments"] and (not requested_dms):
        state = (
            "DMSF_NOT_AVAILABLE"
            if any((item.get("code") == "REDMINE_DMSF_UNAVAILABLE" for item in dms_warnings))
            else (
                "ERROR" if dms_warnings else "SELECTION_REQUIRED" if dms_files else "NO_ATTACHMENTS"
            )
        )
        result = {
            "state": state,
            "issue": issue_summary,
            "copied": [],
            "dms_files": dms_files,
            "warnings": dms_warnings,
        }
        if getattr(client, "recovery_warnings", None):
            result["warnings"].extend(client.recovery_warnings)
        _value = result
        return OperationResult(_value, 1 if dms_warnings else 0)
    workspace = product_root / ".workspace"
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="redmine-", dir=workspace) as temporary:
            temp_root = Path(temporary)
            standard_root = temp_root / "standard"
            dms_root = temp_root / "dms"
            standard_root.mkdir()
            dms_root.mkdir()
            _, attachment_metadata = (
                client.download_attachments(issue, standard_root)
                if issue["attachments"]
                else ([], [])
            )
            dms_downloads: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
            remaining_bytes = max(
                0,
                intake_service.MAX_INTAKE_BYTES
                - sum(
                    (
                        item.get("size", 0)
                        for item in attachment_metadata
                        if isinstance(item.get("size"), int)
                    )
                ),
            )
            for dms_metadata in requested_dms:
                try:
                    fresh_issue = client.issue(issue_id)
                    if not client.is_dmsf_attached(fresh_issue, dms_metadata["id"]):
                        raise ext_scripts_redmine_client.RedmineError(
                            f"DMSF file {dms_metadata['id']} is no longer attached to issue {issue_id}.",
                            code="REDMINE_DMSF_NOT_ATTACHED",
                        )
                    path, downloaded = client.download_dmsf_file(
                        dms_metadata,
                        dms_root,
                        revision_id=revision_id if len(requested_dms) == 1 else None,
                        max_bytes=remaining_bytes,
                    )
                    remaining_bytes -= downloaded["size"]
                    dms_downloads.append((path, downloaded, dms_metadata))
                except ext_scripts_redmine_client.RedmineError as exc:
                    dms_warnings.append(
                        {**redmine_dms_error(exc), "dms_file_id": dms_metadata.get("id")}
                    )
            files, skipped_paths = intake_service.enumerate_intake_files([standard_root])
            unsupported_names = sorted({Path(item).name for item in skipped_paths})
            if not files and (not dms_downloads):
                if dms_warnings:
                    _value = {
                        "schema_version": 1,
                        "state": "ERROR",
                        "issue": issue_summary,
                        "copied": [],
                        "dms_files": dms_files,
                        "warnings": dms_warnings,
                        "errors": dms_warnings,
                        "skipped_unsupported": unsupported_names,
                    }
                    return OperationResult(_value, 2)
                if dms_files:
                    _value = {
                        "schema_version": 1,
                        "state": "SELECTION_REQUIRED",
                        "issue": issue_summary,
                        "copied": [],
                        "dms_files": dms_files,
                        "warnings": [],
                        "skipped_unsupported": unsupported_names,
                    }
                    return OperationResult(_value, 0)
                result = {
                    "state": "NO_SUPPORTED_ATTACHMENTS",
                    "issue": issue_summary,
                    "downloaded": attachment_metadata,
                    "skipped_unsupported": unsupported_names,
                    "supported_types": sorted(intake_service.ALLOWED_ARTIFACT_EXTENSIONS),
                }
                if getattr(client, "recovery_warnings", None):
                    result["warnings"] = client.recovery_warnings
                _value = result
                return OperationResult(_value, 1)
            source_record = {
                "system": "redmine",
                "base_url": base_url,
                "issue_id": issue_id,
                "issue_url": f"{base_url}/issues/{issue_id}",
            }
            result = {
                "state": "ACCEPTED",
                "intake_id": None,
                "code": code,
                "destination": None,
                "copied": [],
                "skipped": [],
            }
            if files:
                result = intake_service.persist_intake_files(
                    files,
                    [],
                    code=code,
                    category="redmine_attachments",
                    received_via="redmine",
                    source_record=source_record,
                    product_root=product_root,
                )
            dms_copied: list[dict[str, Any]] = []
            for path, downloaded, metadata in dms_downloads:
                provenance = {
                    **source_record,
                    "attachment_type": "dmsf",
                    "dmsf_file_id": metadata["id"],
                    "dmsf_revision_id": downloaded.get("revision", {}).get("id"),
                    "dmsf_project_id": metadata["project_id"],
                    "source_url": downloaded.get("download_url")
                    or (
                        f"{base_url}/dmsf/files/{metadata['id']}/view?download={downloaded['revision']['id']}"
                        if revision_id is not None and len(requested_dms) == 1
                        else f"{base_url}/dmsf/files/{metadata['id']}/download"
                    ),
                }
                item_result = intake_service.persist_intake_files(
                    [(path, Path(path.name))],
                    [],
                    code=code,
                    category="redmine_attachments",
                    received_via="redmine",
                    source_record=provenance,
                    product_root=product_root,
                )
                dms_copied.extend(item_result["copied"])
                result["skipped"].extend(item_result["skipped"])
                if not result.get("destination"):
                    result.update({key: item_result[key] for key in ("intake_id", "destination")})
            result["copied"].extend(dms_copied)
            downloaded_all = attachment_metadata + [item for _, item, _ in dms_downloads]
            existing_names = result["skipped"]
            result["skipped"] = sorted(unsupported_names + existing_names)
            result.update(
                issue=issue_summary,
                downloaded=downloaded_all,
                dms_files=dms_files,
                credential_source=credential_source,
                skipped_unsupported=unsupported_names,
                skipped_existing=existing_names,
                warnings=dms_warnings,
            )
            if getattr(client, "recovery_warnings", None):
                result["warnings"].extend(client.recovery_warnings)
            if dms_warnings:
                result["state"] = "IMPORTED_WITH_WARNINGS" if result["copied"] else "ERROR"
            elif unsupported_names:
                result["state"] = "PARTIAL"
            elif dms_downloads:
                result["state"] = "IMPORTED"
            _value = result
            return OperationResult(_value, 0 if not unsupported_names and (not dms_warnings) else 1)
    except ext_scripts_redmine_client.RedmineError as exc:
        _value = {
            "schema_version": 1,
            "state": "ERROR",
            "issue": issue_summary,
            "errors": [redmine_dms_error(exc)],
        }
        return OperationResult(_value, 2)
    except OSError as exc:
        raise WorkflowError(
            f"Could not download or import Redmine attachments safely: {exc}"
        ) from exc

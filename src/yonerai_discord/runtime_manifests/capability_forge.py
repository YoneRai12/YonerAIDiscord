from __future__ import annotations

from types import MappingProxyType

from ..control_plane import RbacLevel, RiskLevel
from .types import RuntimeCapabilityDefinition, _cap


FORGE_OWNER_NOTIFICATION_CAPABILITY_ID = "cap-run-capability-forge-owner-notification"
SANDBOX_STATUS_CAPABILITY_ID = "cap-run-capability-forge-sandbox-status"
SANDBOX_DOCTOR_CAPABILITY_ID = "cap-run-capability-forge-sandbox-doctor"
SANDBOX_PLAN_CAPABILITY_ID = "cap-run-capability-forge-sandbox-plan"
SANDBOX_RUN_TEMPLATE_CAPABILITY_ID = "cap-run-capability-forge-sandbox-run-template"
SANDBOX_JOBS_CAPABILITY_ID = "cap-run-capability-forge-sandbox-jobs"
SANDBOX_RECEIPT_CAPABILITY_ID = "cap-run-capability-forge-sandbox-receipt"
SANDBOX_CANCEL_CAPABILITY_ID = "cap-run-capability-forge-sandbox-cancel"

SANDBOX_COMMAND_CAPABILITY_IDS = MappingProxyType(
    {
        "status": SANDBOX_STATUS_CAPABILITY_ID,
        "doctor": SANDBOX_DOCTOR_CAPABILITY_ID,
        "plan": SANDBOX_PLAN_CAPABILITY_ID,
        "run-template": SANDBOX_RUN_TEMPLATE_CAPABILITY_ID,
        "jobs": SANDBOX_JOBS_CAPABILITY_ID,
        "receipt": SANDBOX_RECEIPT_CAPABILITY_ID,
        "cancel": SANDBOX_CANCEL_CAPABILITY_ID,
    }
)


CAPABILITIES: tuple[RuntimeCapabilityDefinition, ...] = (
    _cap(
        FORGE_OWNER_NOTIFICATION_CAPABILITY_ID,
        "intelligence.capability-forge",
        "Recipe Forge の owner-only DM review",
        plugin="capability_forge",
        level=RbacLevel.BOT_OWNER,
        risk=RiskLevel.HIGH,
        default_enabled=False,
        owner_only=True,
    ),
    _cap(
        SANDBOX_STATUS_CAPABILITY_ID,
        "intelligence.capability-forge",
        "Execution Sandbox owner status",
        command="sandbox status",
        plugin="capability_forge",
        level=RbacLevel.BOT_OWNER,
        risk=RiskLevel.LOW,
        owner_only=True,
    ),
    _cap(
        SANDBOX_DOCTOR_CAPABILITY_ID,
        "intelligence.capability-forge",
        "Execution Sandbox owner doctor",
        command="sandbox doctor",
        plugin="capability_forge",
        level=RbacLevel.BOT_OWNER,
        risk=RiskLevel.LOW,
        owner_only=True,
    ),
    _cap(
        SANDBOX_PLAN_CAPABILITY_ID,
        "intelligence.capability-forge",
        "Execution Sandbox owner plan",
        command="sandbox plan",
        plugin="capability_forge",
        level=RbacLevel.BOT_OWNER,
        risk=RiskLevel.LOW,
        owner_only=True,
    ),
    _cap(
        SANDBOX_RUN_TEMPLATE_CAPABILITY_ID,
        "intelligence.capability-forge",
        "Execution Sandbox owner fixed-template run",
        command="sandbox run-template",
        plugin="capability_forge",
        level=RbacLevel.BOT_OWNER,
        risk=RiskLevel.HIGH,
        default_enabled=False,
        owner_only=True,
    ),
    _cap(
        SANDBOX_JOBS_CAPABILITY_ID,
        "intelligence.capability-forge",
        "Execution Sandbox owner job projection",
        command="sandbox jobs",
        plugin="capability_forge",
        level=RbacLevel.BOT_OWNER,
        risk=RiskLevel.MEDIUM,
        default_enabled=False,
        owner_only=True,
    ),
    _cap(
        SANDBOX_RECEIPT_CAPABILITY_ID,
        "intelligence.capability-forge",
        "Execution Sandbox owner receipt projection",
        command="sandbox receipt",
        plugin="capability_forge",
        level=RbacLevel.BOT_OWNER,
        risk=RiskLevel.MEDIUM,
        default_enabled=False,
        owner_only=True,
    ),
    _cap(
        SANDBOX_CANCEL_CAPABILITY_ID,
        "intelligence.capability-forge",
        "Execution Sandbox owner cancellation",
        command="sandbox cancel",
        plugin="capability_forge",
        level=RbacLevel.BOT_OWNER,
        risk=RiskLevel.HIGH,
        default_enabled=False,
        owner_only=True,
    ),
)


__all__ = [
    "CAPABILITIES",
    "FORGE_OWNER_NOTIFICATION_CAPABILITY_ID",
    "SANDBOX_CANCEL_CAPABILITY_ID",
    "SANDBOX_COMMAND_CAPABILITY_IDS",
    "SANDBOX_DOCTOR_CAPABILITY_ID",
    "SANDBOX_JOBS_CAPABILITY_ID",
    "SANDBOX_PLAN_CAPABILITY_ID",
    "SANDBOX_RECEIPT_CAPABILITY_ID",
    "SANDBOX_RUN_TEMPLATE_CAPABILITY_ID",
    "SANDBOX_STATUS_CAPABILITY_ID",
]

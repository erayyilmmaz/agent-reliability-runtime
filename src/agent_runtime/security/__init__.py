"""Authentication and audit boundaries; ARR-11 implements policies."""

from agent_runtime.security.audit import (
    NoopSecurityAuditSink,
    SecurityAuditRecord,
    SecurityAuditSink,
    SqlAlchemySecurityAuditSink,
)
from agent_runtime.security.authentication import AuthenticationResult, authenticate_api_key

__all__ = [
    "AuthenticationResult",
    "NoopSecurityAuditSink",
    "SecurityAuditRecord",
    "SecurityAuditSink",
    "SqlAlchemySecurityAuditSink",
    "authenticate_api_key",
]

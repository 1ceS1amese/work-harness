from enum import StrEnum


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AllocationMode(StrEnum):
    AUTO = "auto"
    DIRECT = "direct"
    CROSS_FUNCTIONAL = "cross_functional"


class AgentFunction(StrEnum):
    ANALYSIS = "analysis"
    RESEARCH = "research"
    EXECUTION = "execution"
    RISK = "risk"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunJobKind(StrEnum):
    START = "start"
    RESUME = "resume"


class RunJobStatus(StrEnum):
    AVAILABLE = "available"
    LEASED = "leased"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    DEAD = "dead"


class ArtifactStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class ReviewVerdict(StrEnum):
    PASS = "pass"
    REVISE = "revise"
    REJECT = "reject"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class DecisionAction(StrEnum):
    APPROVE = "approve"
    REPAIR = "repair"
    REQUEST_HUMAN = "request_human"
    FAIL = "fail"


class ToolCategory(StrEnum):
    READ = "read"
    NETWORK = "network"
    WRITE = "write"
    EXECUTE = "execute"
    EXTERNAL_COMMUNICATION = "external_communication"


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"

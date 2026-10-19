"""Exception hierarchy for promptpin."""


class PromptpinError(Exception):
    """Base error for all promptpin failures."""


class NotAGitRepoError(PromptpinError):
    """Raised when a git-backed operation runs outside a git repository."""


class PromptNotFoundError(PromptpinError):
    """Raised when a prompt file cannot be found."""


class VersionNotFoundError(PromptpinError):
    """Raised when a version ref cannot be resolved to a commit."""


class InvalidPromptError(PromptpinError):
    """Raised when a prompt file fails schema validation."""


class DeployError(PromptpinError):
    """Raised when a deploy or rollback cannot be completed."""


class LockError(PromptpinError):
    """Raised when the deploy lock cannot be acquired."""

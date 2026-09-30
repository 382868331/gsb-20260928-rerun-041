class ImportFormatError(ValueError):
    """Raised when an exchange bundle cannot be parsed as a whole."""


class ValidationError(ValueError):
    """Raised when a single record is structurally invalid."""


class ConflictError(ValueError):
    """Raised when an incoming version conflicts with stored history."""


class IdentityError(ValueError):
    """Raised when a manual patient identity operation is invalid."""

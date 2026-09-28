class MDEError(Exception):
    """Base class for exchange errors."""


class ValidationError(MDEError):
    """A record or envelope violates the mde-json format contract."""


class ConflictError(MDEError):
    """A record with the same identity and version carries different data."""

    def __init__(self, uid: str, version: int):
        super().__init__(f"conflicting payload for {uid} at version {version}")
        self.uid = uid
        self.version = version

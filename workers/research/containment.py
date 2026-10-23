"""Reconciliation confirms every owned execution family, never short-circuits."""

from uuid import UUID


class AttemptSandboxStopper:
    def __init__(self, attempt_id, native, portable):
        self.attempt_id = str(UUID(str(attempt_id)))
        self.native, self.portable = native, portable

    def stop(self, identifier):
        identifier = str(UUID(str(identifier)))
        operations = [self.native.stop]
        if identifier == self.attempt_id:
            # Root attempt teardown also discovers children after a lost response.
            operations.append(self.portable.stop_attempt)
        else:
            # A durable physical evaluation UUID addresses both sibling roles.
            operations.append(self.portable.stop)
        confirmed = []
        for operation in operations:
            try:
                confirmed.append(operation(identifier) is True)
            except Exception:  # noqa: BLE001 - one failure cannot skip another child
                confirmed.append(False)
        return all(confirmed)

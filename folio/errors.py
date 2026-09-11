"""Errors, each carrying the exit code the CLI ends with.

    0 ok   1 the tool reported an error   2 usage   3 unreachable or timed out
    4 token rejected   5 protocol trouble (unexpected reply, dropped stream)
"""


class FolioError(Exception):
    exit_code = 1


class UsageError(FolioError):
    exit_code = 2


class Unreachable(FolioError):
    exit_code = 3


class TimedOut(Unreachable):
    pass


class Unauthorized(FolioError):
    exit_code = 4


class ProtocolError(FolioError):
    exit_code = 5


class RpcError(FolioError):
    """A JSON-RPC error reply - unknown tool, malformed params."""

    def __init__(self, error):
        self.code = error.get('code')
        self.data = error.get('data')
        super().__init__(error.get('message') or f'JSON-RPC error {self.code}')

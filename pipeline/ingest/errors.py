"""Ingest exceptions.

The distinction that matters here is between a *legal* signal and a *reliability*
signal. AccessDeniedError is legal: it means a source has adopted the posture that
disqualified Vermont and Colorado (docs/source-recon.md §5), and the correct response
is to stop, not to retry with different headers. FetchError is reliability: transient,
retryable, and isolated to one document.
"""


class IngestError(Exception):
    """Base for every ingest failure."""


class AccessDeniedError(IngestError):
    """A source returned 403, or robots.txt disallows the path.

    Never retried. Halts the offending state's ingest and drives exit code 2.

    Two of thirteen candidate sources already 403 honest clients on what appears to
    be a shared CDN configuration, and either selected source could adopt the same
    posture at any time (docs/source-recon.md §8 risk 7). This is a finding, not an
    obstacle.
    """

    def __init__(self, url: str, status: int | None = None, detail: str = ""):
        self.url = url
        self.status = status
        self.detail = detail
        what = f"HTTP {status}" if status is not None else "robots.txt disallow"
        suffix = f" — {detail}" if detail else ""
        super().__init__(
            f"Access denied ({what}) for {url}{suffix}. "
            "Halting this state. Do not retry with different headers."
        )


class FetchError(IngestError):
    """A document could not be retrieved after exhausting retries.

    Document-scoped and non-fatal: the run records it, continues, and exits 1.
    """

    def __init__(self, url: str, attempts: int, detail: str, status: int | None = None):
        self.url = url
        self.attempts = attempts
        self.detail = detail
        self.status = status
        super().__init__(f"Fetch failed for {url} after {attempts} attempt(s): {detail}")


class SourceCountMismatch(IngestError):
    """Discovery resolved a different number of items than the source config expects.

    Fail loudly. A short set means the path pattern is wrong or the source changed;
    ingesting it quietly would silently narrow the corpus.
    """

    def __init__(self, state: str, what: str, expected: int, actual: int, detail: str = ""):
        self.state = state
        self.what = what
        self.expected = expected
        self.actual = actual
        suffix = f" {detail}" if detail else ""
        super().__init__(
            f"{state}: expected {expected} {what}, resolved {actual}. "
            f"Either the source changed or the resolution pattern is wrong.{suffix}"
        )


class ConfigError(IngestError):
    """config/sources.yml is missing, malformed, or incomplete."""

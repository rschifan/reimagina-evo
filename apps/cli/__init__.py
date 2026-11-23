"""CLI application scaffold."""


class UserError(ValueError):
    """A refusal the person at the terminal can act on: a malformed or unknown
    value they typed, or a run that outlived their ``--timeout``.

    ``main`` reports exactly these (and a few named library refusals) as one
    ``error:`` line with exit 2. Any other ``ValueError`` is a defect and keeps
    its traceback. A ``ValueError`` so the sub-apps that already catch that
    still do.
    """

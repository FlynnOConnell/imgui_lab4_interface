"""
The sign-in gate, the same demo credentials the pml_utilities BioHPC widget uses.

Nothing is authenticated: storage access rides on the machine's own mount or ssh keys. A real identity
provider replaces sign_in and keeps its signature.
"""

DEMO_USER = "loson"
DEMO_PASSWORD = "treadmill"


def sign_in(username: str, password: str) -> bool:
    """Whether the username and password open the app."""
    return username.strip() == DEMO_USER and password == DEMO_PASSWORD

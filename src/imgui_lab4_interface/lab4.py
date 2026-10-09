"""
The Lab4 recording identity, the key its Recording row registers under, and the sidecar written beside its data.

The key mirrors Lab4's tables: ExptGroup (experimenter, collection) -> Session (+ subject, datetime) -> Recording
(+ device). The values each field may take come from the database; see database.Schema.
"""

import dataclasses
from datetime import datetime
from typing import Optional


def parse_datetime(text: str) -> Optional[datetime]:
    """YYYY-MM-DD, optionally with HH:MM[:SS]; None when it is neither."""
    try:
        return datetime.fromisoformat(text.strip())
    except ValueError:
        return None


@dataclasses.dataclass
class Recording:
    """
    One recording's identity and the notes that travel with its data.

    Attributes:
        collection_name (str): The experiment group, unique per experimenter
        session_datetime (str): When the session started, YYYY-MM-DD HH:MM
        subject_fields (dict): Column -> value for the Subject row, used only when the subject is new to Lab4
        notes (str): The session's notes in Lab4, and the sidecar's
        extra (list): [key, value] pairs for the sidecar only; rows with an empty key are dropped
    """

    experimenter: str = ""
    collection_name: str = ""
    subject_id: str = ""
    session_datetime: str = dataclasses.field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))
    device_name: str = ""
    ui_label: str = ""
    weight_g: str = ""
    subject_fields: dict = dataclasses.field(default_factory=dict)
    notes: str = ""
    extra: list = dataclasses.field(default_factory=list)

    def label(self) -> str:
        """e.g. M231_2026-08-21_1430_Bruker 1 Imaging"""
        when = parse_datetime(self.session_datetime)
        stamp = when.strftime("%Y-%m-%d_%H%M") if when else self.session_datetime.strip()
        return f"{self.subject_id.strip() or 'subject'}_{stamp}_{self.device_name.strip() or 'device'}"

    def key(self) -> dict:
        """The primary key of Lab4's Recording table."""
        return {
            "experimenter": self.experimenter.strip(),
            "collection_name": self.collection_name.strip(),
            "subject_id": self.subject_id.strip(),
            "session_datetime": parse_datetime(self.session_datetime),
            "device_name": self.device_name.strip(),
        }

    def problems(self) -> list[str]:
        """What keeps the key from registering, whatever the database holds."""
        found = [f"{name} is empty" for name, value in (("experimenter", self.experimenter), ("experiment group", self.collection_name), ("subject", self.subject_id), ("device", self.device_name)) if not value.strip()]
        if parse_datetime(self.session_datetime) is None:
            found.append("session time is not YYYY-MM-DD HH:MM")
        if self.weight_g.strip():
            try:
                float(self.weight_g)
            except ValueError:
                found.append("weight is not a number")
        return found

    def sidecar(self, user: str) -> dict:
        """The json written beside transferred data."""
        when = parse_datetime(self.session_datetime)
        return {
            "recording": self.label(),
            "key": {**self.key(), "session_datetime": when.isoformat(sep=" ") if when else self.session_datetime.strip()},
            "ui_label": self.ui_label.strip(),
            "weight_g": self.weight_g.strip(),
            "subject": {k: v.strip() for k, v in self.subject_fields.items() if v.strip()},
            "notes": self.notes,
            "extra": {k.strip(): v for k, v in self.extra if k.strip()},
            "written_by": user,
            "written_at": datetime.now().isoformat(timespec="seconds"),
        }

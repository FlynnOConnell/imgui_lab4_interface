"""
A recording's Lab4 key and the sidecar written beside its data.

The key follows Lab4's tables: ExptGroup (experimenter, collection) -> Session (+ subject, datetime) -> Recording
(+ device).
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


def format_datetime(when: datetime) -> str:
    """YYYY-MM-DD HH:MM, with :SS only when the seconds are not zero."""
    return when.strftime("%Y-%m-%d %H:%M:%S" if when.second else "%Y-%m-%d %H:%M")


def text_of(value) -> str:
    """A database value as the form shows it; empty for NULL."""
    return (
        ""
        if value is None
        else format_datetime(value)
        if isinstance(value, datetime)
        else str(value)
    )


@dataclasses.dataclass
class Recording:
    """
    One recording's Lab4 key and notes.

    Attributes:
        collection_name (str): The experiment group, unique per experimenter
        session_datetime (str): Session start, YYYY-MM-DD HH:MM
        subject_fields (dict): Subject row values, used only for a new subject
        notes (str): Session notes, in Lab4 and the sidecar
        extra (list): [key, value] pairs for the sidecar only; empty keys are dropped
    """

    experimenter: str = ""
    collection_name: str = ""
    subject_id: str = ""
    session_datetime: str = dataclasses.field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M")
    )
    device_name: str = ""
    ui_label: str = ""
    weight_g: str = ""
    subject_fields: dict = dataclasses.field(default_factory=dict)
    notes: str = ""
    extra: list = dataclasses.field(default_factory=list)

    @classmethod
    def from_sidecar(cls, sidecar: dict) -> "Recording":
        """The recording a sidecar describes; fields an older sidecar lacks are left empty."""
        key = sidecar.get("key", {})
        when = parse_datetime(str(key.get("session_datetime", "")))
        return cls(
            experimenter=key.get("experimenter", ""),
            collection_name=key.get("collection_name", ""),
            subject_id=key.get("subject_id", ""),
            session_datetime=format_datetime(when)
            if when
            else str(key.get("session_datetime", "")),
            device_name=key.get("device_name", ""),
            ui_label=sidecar.get("ui_label", ""),
            weight_g=str(sidecar.get("weight_g", "")),
            subject_fields=dict(sidecar.get("subject", {})),
            notes=sidecar.get("notes", ""),
            extra=[[k, str(v)] for k, v in sidecar.get("extra", {}).items()],
        )

    @classmethod
    def from_lab4(
        cls, recording: dict, notes: str, subject_fields: dict
    ) -> "Recording":
        """A Lab4 recording row, its session's notes and its subject's columns other than the key."""
        return cls(
            experimenter=recording["experimenter"],
            collection_name=recording["collection_name"],
            subject_id=recording["subject_id"],
            session_datetime=text_of(recording["session_datetime"]),
            device_name=recording["device_name"],
            ui_label=text_of(recording.get("ui_label")),
            weight_g=text_of(recording.get("weight_g")),
            subject_fields=dict(subject_fields),
            notes=notes,
        )

    def label(self) -> str:
        """e.g. M231_2026-08-21_1430_Bruker 1 Imaging"""
        when = parse_datetime(self.session_datetime)
        stamp = (
            when.strftime("%Y-%m-%d_%H%M") if when else self.session_datetime.strip()
        )
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
        """Problems with the key itself, without the database."""
        found = [
            f"{name} is empty"
            for name, value in (
                ("experimenter", self.experimenter),
                ("experiment group", self.collection_name),
                ("subject", self.subject_id),
                ("device", self.device_name),
            )
            if not value.strip()
        ]
        if parse_datetime(self.session_datetime) is None:
            found.append("session time must be YYYY-MM-DD HH:MM")
        if self.weight_g.strip():
            try:
                float(self.weight_g)
            except ValueError:
                found.append("weight must be a number")
        return found

    def sidecar(self, user: str) -> dict:
        """The sidecar json."""
        when = parse_datetime(self.session_datetime)
        return {
            "recording": self.label(),
            "key": {
                **self.key(),
                "session_datetime": when.isoformat(sep=" ")
                if when
                else self.session_datetime.strip(),
            },
            "ui_label": self.ui_label.strip(),
            "weight_g": self.weight_g.strip(),
            "subject": {
                k: v.strip() for k, v in self.subject_fields.items() if v.strip()
            },
            "notes": self.notes,
            "extra": {k.strip(): v for k, v in self.extra if k.strip()},
            "written_by": user,
            "written_at": datetime.now().isoformat(timespec="seconds"),
        }

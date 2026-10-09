"""
The Lab4 recording identity, the DataJoint key data registers under, and the sidecar written beside it.

The vocabularies mirror the Lab4 pipeline (Experimenter -> Subject -> Session -> Recording) as the pml_utilities
BioHPC widget lists them.
"""

import dataclasses
from datetime import date, datetime

EXPERIMENTERS = ["loson", "jdoe", "asmith", "mkim", "rpatel"]
DEVICES = ["AOD-2P", "Bruker-2P", "Mini2P", "SLM-2P", "BehaviorMate", "DeepLabCut", "SLEAP", "Doric-1P", "LFP"]
INDICATORS = ["GCaMP6f", "GCaMP6s", "GCaMP7f", "GCaMP8f", "GCaMP8m", "GCaMP8s", "jRGECO1a"]
REGIONS = ["CA1", "CA3", "DG", "V1", "mPFC", "RSC", "S1"]


@dataclasses.dataclass
class Recording:
    """
    One recording's identity and the notes that travel with its data.

    Attributes:
        extra (list): [key, value] pairs added by hand; rows with an empty key are dropped from the sidecar
    """

    experimenter: str = EXPERIMENTERS[0]
    expt_group: str = ""
    subject: str = ""
    session_date: str = dataclasses.field(default_factory=lambda: date.today().isoformat())
    recording_idx: int = 1
    device: str = DEVICES[0]
    indicator: str = INDICATORS[0]
    region: str = REGIONS[0]
    notes: str = ""
    extra: list = dataclasses.field(default_factory=list)

    def label(self) -> str:
        """e.g. M231_2026-08-21_rec01"""
        return f"{self.subject.strip() or 'subject'}_{self.session_date.strip()}_rec{self.recording_idx:02d}"

    def key(self) -> dict:
        """The DataJoint primary key of the Recording table."""
        return {
            "experimenter": self.experimenter,
            "expt_group": self.expt_group.strip(),
            "subject_id": self.subject.strip(),
            "session_date": self.session_date.strip(),
            "recording_idx": int(self.recording_idx),
        }

    def problems(self) -> list[str]:
        """What keeps the key from registering."""
        found = []
        if not self.subject.strip():
            found.append("subject is empty")
        try:
            date.fromisoformat(self.session_date.strip())
        except ValueError:
            found.append("session date is not YYYY-MM-DD")
        return found

    def sidecar(self, user: str) -> dict:
        """The json written beside transferred data."""
        return {
            "recording": self.label(),
            "key": self.key(),
            "acquisition": {"device": self.device, "indicator": self.indicator, "region": self.region},
            "notes": self.notes,
            "extra": {k.strip(): v for k, v in self.extra if k.strip()},
            "written_by": user,
            "written_at": datetime.now().isoformat(timespec="seconds"),
        }

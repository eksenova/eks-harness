from __future__ import annotations

from typing import Literal

from pydantic import Field

from eks_harness.ir.ops.ui import LEASE, MOBILE
from eks_harness.ir.refs import RJson, RMedia
from eks_harness.ir.registry import Args, op

FakeMode = Literal["success", "error", "cancel"]
SLOT_PATTERN = r"^[a-z][a-z0-9_.-]{0,63}$"


@op("fake", name="arm", platforms=MOBILE, resources=LEASE, location="worker", pinnable=True,
    description="Arm a device fake (camera, gallery, document, NFC or a plugin slot) with media or data",
    examples=[
        {"slot": "nfc", "mode": "success", "fixture": "kimlik-cip.json"},
        {"slot": "camera", "media": {"$ref": "n0000000001", "path": []}},
    ])
class Arm(Args):
    slot: str = Field(pattern=SLOT_PATTERN, description="Fake to arm, such as camera, nfc or documents")
    mode: FakeMode = Field(default="success", description="How the fake answers: success, failure or another mode the fake supports")
    media: RMedia | None = Field(default=None, description="Media the fake returns, such as a photo for the camera")
    fixture: str | None = Field(default=None, description="Name of an app fixture file the fake returns")
    data: RJson = Field(default=None, description="Structured data the fake returns")


@op("fake", name="clear", platforms=MOBILE, resources=LEASE, location="worker", pinnable=True,
    description="Disarm one fake slot, or all of them",
    examples=[{}, {"slot": "camera"}])
class ClearFakes(Args):
    slot: str | None = Field(default=None, pattern=SLOT_PATTERN, description="Fake to disarm; every fake when unset")


__all__ = ["SLOT_PATTERN", "Arm", "ClearFakes", "FakeMode"]

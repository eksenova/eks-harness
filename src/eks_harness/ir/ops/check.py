from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eks_harness.ir.catalog import durations
from eks_harness.ir.ops.ui import LEASE, MOBILE, UI, WEB
from eks_harness.ir.plan import CheckUse
from eks_harness.ir.refs import RJson, RMedia, RStr
from eks_harness.ir.registry import Args, check, op
from eks_harness.ir.target import Target
from eks_harness.ir.time import Span, StreamName, Time
from eks_harness.ir.types import RefTypeError, parse_ref_type

ExploreAction = Literal["tap", "fill", "scroll", "swipe", "back", "key"]
ExploreFailure = Literal[
    "crash", "page_error", "console_error", "failed_request", "error_toast", "layout_spill", "horizontal_scroll",
    "broken_image",
]
DEFAULT_EXPLORE_ACTIONS: tuple[ExploreAction, ...] = ("tap", "fill", "scroll", "back")
DEFAULT_EXPLORE_FAILURES: tuple[ExploreFailure, ...] = ("crash", "page_error", "error_toast")
OcrEngine = Literal["auto", "vision", "tesseract"]
DEFAULT_OCR_LANGUAGES = ("tr-TR", "en-US")
OCR_LANGUAGE = r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$"


class _Assertion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _Bounded(_Assertion):
    equals: int | None = Field(default=None, ge=0, description="Exact number required")
    at_least: int | None = Field(default=None, ge=0, description="Smallest number that passes")
    at_most: int | None = Field(default=None, ge=0, description="Largest number that passes")

    @model_validator(mode="after")
    def _consistent_bounds(self) -> Self:
        if self.equals is None and self.at_least is None and self.at_most is None:
            raise ValueError("needs a bound: equals, at_least or at_most")
        if self.at_least is not None and self.at_most is not None and self.at_least > self.at_most:
            raise ValueError(f"at_least {self.at_least} is above at_most {self.at_most}, so it can never pass")
        if self.equals is not None and (
            (self.at_least is not None and self.equals < self.at_least)
            or (self.at_most is not None and self.equals > self.at_most)
        ):
            raise ValueError(
                f"equals {self.equals} lies outside at_least {self.at_least}, at_most {self.at_most}, "
                "so it can never pass"
            )
        return self


class IsVisible(_Assertion):
    kind: Literal["visible"] = Field(default="visible", description="The target is visible")


class IsHidden(_Assertion):
    kind: Literal["hidden"] = Field(default="hidden", description="The target is hidden or absent")


class IsEnabled(_Assertion):
    kind: Literal["enabled"] = Field(default="enabled", description="The target is enabled")


class IsDisabled(_Assertion):
    kind: Literal["disabled"] = Field(default="disabled", description="The target is disabled")


class IsChecked(_Assertion):
    kind: Literal["checked"] = Field(default="checked", description="The checkbox or switch has a state")
    on: bool = Field(default=True, description="Required state: checked when true, unchecked when false")


class _Matched(_Assertion):
    equals: RStr | None = Field(default=None, description="Exact text required")
    contains: RStr | None = Field(default=None, description="Text that must occur")
    matches: str | None = Field(default=None, description="Regular expression that must match")

    @model_validator(mode="after")
    def _has_criterion(self) -> Self:
        if self.equals is None and self.contains is None and self.matches is None:
            raise ValueError("needs at least one of equals, contains or matches")
        return self


class HasText(_Matched):
    kind: Literal["text"] = Field(default="text", description="The target's visible text matches")


class HasValue(_Matched):
    kind: Literal["value"] = Field(default="value", description="The input's value matches")


class HasCount(_Bounded):
    kind: Literal["count"] = Field(default="count", description="The number of visible matches is within bounds")


class HasAttr(_Matched):
    kind: Literal["attr"] = Field(default="attr", description="An attribute of the target matches")
    name: str = Field(min_length=1, description="Attribute name")


TargetAssertion = Annotated[
    IsVisible | IsHidden | IsEnabled | IsDisabled | IsChecked | HasText | HasValue | HasCount | HasAttr,
    Field(discriminator="kind"),
]


class Eq(_Assertion):
    kind: Literal["eq"] = Field(default="eq", description="The value equals the expected one")
    value: RJson = Field(description="Expected value")


class Ne(_Assertion):
    kind: Literal["ne"] = Field(default="ne", description="The value differs from the given one")
    value: RJson = Field(description="Value it must not equal")


class Contains(_Assertion):
    kind: Literal["contains"] = Field(default="contains", description="A string, list or object contains the item")
    value: RJson = Field(description="Substring, list item or object key that must be present")


class Matches(_Assertion):
    kind: Literal["matches"] = Field(default="matches", description="A string value matches a regular expression")
    pattern: str = Field(description="Regular expression searched in the value")


class Gt(_Assertion):
    kind: Literal["gt"] = Field(default="gt", description="The value is greater than the bound")
    value: RJson = Field(description="Exclusive lower bound")


class Ge(_Assertion):
    kind: Literal["ge"] = Field(default="ge", description="The value is greater than or equal to the bound")
    value: RJson = Field(description="Inclusive lower bound")


class Lt(_Assertion):
    kind: Literal["lt"] = Field(default="lt", description="The value is less than the bound")
    value: RJson = Field(description="Exclusive upper bound")


class Le(_Assertion):
    kind: Literal["le"] = Field(default="le", description="The value is less than or equal to the bound")
    value: RJson = Field(description="Inclusive upper bound")


class Truthy(_Assertion):
    kind: Literal["truthy"] = Field(default="truthy", description="The value is truthy")


class Falsy(_Assertion):
    kind: Literal["falsy"] = Field(default="falsy", description="The value is falsy")


class Length(_Bounded):
    kind: Literal["length"] = Field(default="length", description="The length of a list, string or object is within bounds")


ValueAssertion = Annotated[
    Eq | Ne | Contains | Matches | Gt | Ge | Lt | Le | Truthy | Falsy | Length,
    Field(discriminator="kind"),
]


SIZED_TYPES = frozenset({"str", "list", "json"})
TEXT_TYPES = frozenset({"str", "json"})
NUMBER_TYPES = frozenset({"int", "float", "json"})
ORDER_KINDS = frozenset({"gt", "ge", "lt", "le"})
VALUE_TYPES_BY_KIND: Mapping[str, frozenset[str]] = {
    "contains": SIZED_TYPES,
    "length": SIZED_TYPES,
    "matches": TEXT_TYPES,
    **dict.fromkeys(ORDER_KINDS, NUMBER_TYPES),
}
TYPE_WORDS: Mapping[frozenset[str], str] = {
    SIZED_TYPES: "a str, list or json value",
    TEXT_TYPES: "a str or json value",
    NUMBER_TYPES: "an int, float or json value",
}


def literal_type(value: object) -> str | None:
    match value:
        case bool():
            return "bool"
        case int():
            return "int"
        case float():
            return "float"
        case str():
            return "str"
        case list() | tuple():
            return "list[json]"
        case dict():
            return "json"
    return None


def _type_name(type_: str | None) -> str | None:
    if type_ is None:
        return None
    try:
        return parse_ref_type(type_).name
    except RefTypeError:
        return None


def assertion_problem(kind: str, produced: str, compared: str | None = None) -> str | None:
    name = _type_name(produced)
    accepted = VALUE_TYPES_BY_KIND.get(kind)
    if name is None or accepted is None:
        return None
    if name not in accepted:
        return (f"assertion {kind!r} needs {TYPE_WORDS[accepted]}, but the value under test is {produced}, "
                "so it can never pass")
    other = _type_name(compared)
    if other is None or "json" in (name, other):
        return None
    if kind in ORDER_KINDS and other not in NUMBER_TYPES:
        return f"assertion {kind!r} compares the {produced} value under test with a {compared}, which is not a number"
    if kind == "contains" and name == "str" and other != "str":
        return f"assertion 'contains' looks for a {compared} inside a str, which can only hold text"
    return None


@op("expect", name="target", out="json", platforms=UI, resources=LEASE, location="worker",
    description="Assert something about a target; waits up to the timeout and fails the run unless soft",
    examples=[{"target": {"by": "text", "args": {"text": "Kadıköy", "exact": True}}, "assertion": {"kind": "visible"}}])
class ExpectTarget(Args):
    target: Target = Field(description="Element the assertion is about")
    assertion: TargetAssertion = Field(description="What must hold for the target")
    soft: bool = Field(default=False, description="Record a failure without failing the run")
    eventually: bool = Field(default=False, description="Retry until the timeout instead of checking once matched")
    timeout: durations.Duration | None = Field(default=None, description="How long to wait; the session default when unset")
    message: str | None = Field(default=None, description="Text shown with the failure")


@op("expect", name="value", out="json",
    description="Assert something about a value produced earlier; eventually re-reads idempotent producers",
    examples=[
        {"value": {"$ref": "n0000000001", "path": ["total"]}, "assertion": {"kind": "gt", "value": 0}},
        {
            "value": {"$ref": "n0000000001", "path": []},
            "assertion": {"kind": "contains", "value": "Kadıköy"},
            "soft": True,
        },
    ])
class ExpectValue(Args):
    value: RJson = Field(description="Value under test, usually a $ref to an earlier node")
    assertion: ValueAssertion = Field(description="What must hold for the value")
    soft: bool = Field(default=False, description="Record a failure without failing the run")
    eventually: bool = Field(default=False, description="Re-run the idempotent producer until the assertion holds")
    timeout: durations.Duration | None = Field(default=None, description="How long to retry; the session default when unset")
    message: str | None = Field(default=None, description="Text shown with the failure")


@op("check", name="checkpoint", out="json", platforms=UI, resources=LEASE, location="worker",
    description="Run checkpoint checks (layout, errors, requests, visual, performance) and take evidence shots",
    examples=[{"label": "liste"}, {"label": "liste-telefon", "viewport": "phone"}])
class Checkpoint(Args):
    label: RStr = Field(description="Name of the checkpoint in the report and of its screenshots")
    viewport: str | None = Field(default=None, description="Viewport preset to check in; the session viewport when unset")
    checks: tuple[CheckUse, ...] | None = Field(
        default=None, description="Checks to run; the default checks of the platform and app profile when unset")
    shot: bool = Field(default=True, description="Take an evidence screenshot")
    full: bool = Field(default=False, description="Take the screenshot of the full scrollable page")
    soft: bool = Field(default=False, description="Record failing checks without failing the run")


@op("check", name="explore", out="json", platforms=UI, resources=LEASE, location="worker",
    description="Explore the app's screens model-based from the current screen; every crash or error found is "
                "reported with a reproducing flow",
    examples=[{"budget": "120s", "depth": 4, "avoid": [{"by": "text", "args": {"text": "Çıkış", "exact": True}}]}])
class Explore(Args):
    budget: durations.Duration = Field(description="Wall-clock time the exploration may take")
    steps: int | None = Field(default=None, ge=1, description="Largest number of actions; unbounded within the budget when unset")
    depth: int = Field(default=3, ge=1, description="Largest number of screens away from the start screen")
    seed: int | None = Field(
        default=None, description="Random seed of the exploration; derived from the plan's root seed when unset")
    allow: tuple[Target, ...] = Field(
        default=(), description="Only these targets may be acted on; every actionable element when empty")
    avoid: tuple[Target, ...] = Field(default=(), description="Targets that are never acted on, such as a logout button")
    actions: tuple[ExploreAction, ...] = Field(default=DEFAULT_EXPLORE_ACTIONS, min_length=1,
                                               description="Kinds of input the explorer may use")
    fail_on: tuple[ExploreFailure, ...] = Field(default=DEFAULT_EXPLORE_FAILURES, min_length=1,
                                                description="Findings that fail the run")
    checks: tuple[CheckUse, ...] = Field(default=(), description="Checkpoint checks run on every new screen")
    label: str | None = Field(default=None, description="Name of the exploration in the report")
    soft: bool = Field(default=False, description="Report findings without failing the run")


@op("check", name="media", out="json", pure=True, location="hub",
    description="Run a media review check (cut, beats, loudness, text and more) on rendered media",
    examples=[
        {
            "media": {"$ref": "n0000000001", "path": []},
            "kind": "cut",
            "params": {"threshold": 0.1},
            "at": "beat:8",
            "tolerance": "2f",
        },
    ])
class MediaCheck(Args):
    media: RMedia = Field(description="Rendered media to review, a $ref to a render or capture node")
    kind: str = Field(min_length=1, description="Registered media check kind such as cut, beats or loudness")
    params: dict[str, RJson] = Field(default_factory=dict, description="Parameters of the check's params model")
    at: Time | Span | None = Field(default=None, description="Time or span the check looks at; the whole media when unset")
    tolerance: durations.Length | None = Field(
        default=None, description="How far from at the expected event may land; the check's default when unset")
    label: str | None = Field(default=None, description="Name of the check in the report")
    soft: bool = Field(default=False, description="Record a failure without failing the run")


@check("layout_spill", platforms=WEB, default=True, description="Elements that spill past the viewport edge")
class LayoutSpill(Args):
    ignore: tuple[Target, ...] = Field(default=(), description="Elements allowed to spill past the viewport edge")


@check("horizontal_scroll", platforms=WEB, default=True, description="The page scrolls horizontally")
class HorizontalScroll(Args):
    pass


@check("broken_images", platforms=WEB, default=True, description="Images that failed to load")
class BrokenImages(Args):
    pass


@check("console_errors", platforms=WEB, default=True, description="console.error output since the last checkpoint")
class ConsoleErrors(Args):
    noise: tuple[str, ...] = Field(default=(), description="Substrings of messages to ignore")


@check("page_errors", platforms=UI, default=True, description="Uncaught page or JS errors since the last checkpoint")
class PageErrors(Args):
    noise: tuple[str, ...] = Field(default=(), description="Substrings of error messages to ignore")


@check("failed_requests", platforms=WEB, default=True, description="Requests that failed since the last checkpoint")
class FailedRequests(Args):
    noise: tuple[str, ...] = Field(default=(), description="Substrings of request URLs to ignore")
    allow_status: tuple[int, ...] = Field(default=(), description="HTTP status codes that do not count as failures")


@check("error_toasts", platforms=UI, default=True, description="Visible error toasts or alerts")
class ErrorToasts(Args):
    selector: Target | None = Field(default=None, description="Target matching the app's error toasts; the app profile's when unset")


@check("route", platforms=UI, description="The screen is on the expected route or path")
class RouteIs(Args):
    expected: RStr = Field(description="Expected route name on React Native or path on web")


@check("visual", family="visual", platforms=UI,
       description="Perceptual diff against the approved golden of this checkpoint and viewport")
class VisualGolden(Args):
    golden: str | None = Field(default=None, description="Golden name; the checkpoint label and viewport when unset")
    threshold: float = Field(default=0.001, ge=0.0, le=1.0, description="Largest share of differing pixels that passes")
    masks: tuple[Target, ...] = Field(default=(), description="Regions excluded from the diff")


@check("perf", family="perf", platforms=WEB,
       description="Web performance budget (web vitals, requests, bytes) for the step since the last checkpoint")
class PerfBudget(Args):
    lcp: durations.Duration | None = Field(default=None, description="Largest Contentful Paint budget")
    cls: float | None = Field(default=None, ge=0.0, description="Cumulative Layout Shift budget")
    inp: durations.Duration | None = Field(default=None, description="Interaction to Next Paint budget")
    requests: int | None = Field(default=None, ge=0, description="Largest number of requests")
    bytes: int | None = Field(default=None, ge=0, description="Largest number of transferred bytes")


@check("perf_rn", family="perf", platforms=MOBILE,
       description="React Native performance budget (JS frame drops, requests, bytes) for the step since the last "
                   "checkpoint")
class NativePerfBudget(Args):
    frame_drops: int | None = Field(default=None, ge=0, description="Largest number of dropped JS frames")
    requests: int | None = Field(default=None, ge=0, description="Largest number of requests")
    bytes: int | None = Field(default=None, ge=0, description="Largest number of transferred bytes")


@check("cut", family="media", description="A hard cut lands at the expected time")
class CutCheck(Args):
    threshold: float = Field(default=0.1, gt=0.0, description="Smallest frame difference that counts as a cut")


@check("beats", family="media", description="Cuts and motion line up with a beat stream")
class BeatsCheck(Args):
    stream: StreamName = Field(default="beat", description="Stream the events must line up with: beat, downbeat or bar of the clock tempo, or a media.markers stream")
    min_ratio: float = Field(default=0.8, ge=0.0, le=1.0, description="Smallest share of events on the stream that passes")


@check("onset", family="media", description="An audio onset lands at the expected time")
class OnsetCheck(Args):
    sensitivity: float = Field(default=0.5, ge=0.0, le=1.0, description="Onset detector sensitivity")


@check("loudness", family="media", description="Integrated loudness and true peak are within limits")
class LoudnessCheck(Args):
    lufs: float = Field(description="Target integrated loudness in LUFS")
    tolerance: float = Field(default=1.0, ge=0.0, description="Allowed distance from the target in LU")
    true_peak: float | None = Field(default=None, description="Highest allowed true peak in dBTP")


@check("text", family="media", description="Text is readable on screen (OCR)")
class TextCheck(Args):
    text: str = Field(min_length=1, description="Text that must be recognized")
    region: Target | None = Field(default=None, description="Region to read; the whole frame when unset")
    engine: OcrEngine = Field(
        default="auto",
        description="OCR engine: vision (macOS Vision) or tesseract, failing when it is unavailable; auto picks "
                    "Vision on macOS and Tesseract elsewhere, so the result is keyed per machine")
    languages: tuple[Annotated[str, Field(pattern=OCR_LANGUAGE)], ...] = Field(
        default=DEFAULT_OCR_LANGUAGES, min_length=1, description="Language tags the OCR engine reads, in priority order")

    def pinned(self) -> bool:
        return self.engine != "auto"


@check("motion", family="media", description="Frame-to-frame motion stays within bounds")
class MotionCheck(Args):
    min: float | None = Field(default=None, ge=0.0, description="Smallest mean motion")
    max: float | None = Field(default=None, ge=0.0, description="Largest mean motion")


@check("black", family="media", description="No black frames longer than allowed")
class BlackCheck(Args):
    longest: durations.Length | None = Field(
        default=None, description="Longest black stretch allowed; any black frame fails when zero")


@check("frozen", family="media", description="No frozen frames longer than allowed")
class FrozenCheck(Args):
    longest: durations.Length | None = Field(
        default=None, description="Longest frozen stretch allowed; any repeated frame fails when zero")


@check("safe_area", family="media", description="Text and key content stay inside the platform safe area")
class SafeAreaCheck(Args):
    margins: tuple[float, float, float, float] = Field(
        default=(0.1, 0.05, 0.2, 0.05), description="Unsafe margins as fractions of the frame: top, right, bottom, left")
    region: Target | None = Field(default=None, description="Content that must stay inside; all text when unset")
    engine: OcrEngine = Field(
        default="auto",
        description="OCR engine that finds the text (all text when region is unset): vision (macOS Vision) or "
                    "tesseract, failing when it is unavailable; auto picks Vision on macOS and Tesseract elsewhere, "
                    "so the result is keyed per machine")

    def pinned(self) -> bool:
        return self.engine != "auto"


@check("template", family="media", description="An image template appears in the frame")
class TemplateCheck(Args):
    template: RMedia = Field(description="Template image, a $ref to a media.source node")
    threshold: float = Field(default=0.8, ge=0.0, le=1.0, description="Smallest match score that passes")
    region: Target | None = Field(default=None, description="Region to search; the whole frame when unset")


__all__ = [
    "DEFAULT_EXPLORE_ACTIONS",
    "DEFAULT_EXPLORE_FAILURES",
    "DEFAULT_OCR_LANGUAGES",
    "NUMBER_TYPES",
    "OCR_LANGUAGE",
    "ORDER_KINDS",
    "SIZED_TYPES",
    "TEXT_TYPES",
    "VALUE_TYPES_BY_KIND",
    "BeatsCheck",
    "BlackCheck",
    "BrokenImages",
    "CheckUse",
    "Checkpoint",
    "ConsoleErrors",
    "Contains",
    "CutCheck",
    "Eq",
    "ErrorToasts",
    "ExpectTarget",
    "ExpectValue",
    "Explore",
    "ExploreAction",
    "ExploreFailure",
    "FailedRequests",
    "Falsy",
    "FrozenCheck",
    "Ge",
    "Gt",
    "HasAttr",
    "HasCount",
    "HasText",
    "HasValue",
    "HorizontalScroll",
    "IsChecked",
    "IsDisabled",
    "IsEnabled",
    "IsHidden",
    "IsVisible",
    "LayoutSpill",
    "Le",
    "Length",
    "LoudnessCheck",
    "Lt",
    "Matches",
    "MediaCheck",
    "MotionCheck",
    "NativePerfBudget",
    "Ne",
    "OcrEngine",
    "OnsetCheck",
    "PageErrors",
    "PerfBudget",
    "RouteIs",
    "SafeAreaCheck",
    "TargetAssertion",
    "TemplateCheck",
    "TextCheck",
    "Truthy",
    "ValueAssertion",
    "VisualGolden",
    "assertion_problem",
    "literal_type",
]

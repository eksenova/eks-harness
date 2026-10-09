"""Three-camera multicam with scene-boundary switching (forward-looking).

Three camera angles ride on three stacked tracks with ascending ``z``.
:class:`SceneMarkers` declared on the shared audio source produce cut
points whenever the underlying frame difference exceeds the configured
threshold. :class:`EveryNth` (a :class:`TriggerSource`) is constructed
to express "use every second scene boundary as a switch event".

Note: :class:`EveryNth` and :class:`RandomTrigger` are currently IR-only.
They round-trip through JSON and validate against the schema, but no
effect field yet accepts a :class:`TriggerSource` - so the orchestrator
will not actually switch cameras at those events in the present
release. This recipe is forward-looking: it freezes the intended shape
so that when orchestrator support lands, projects authored today
upgrade in place. The trigger is parked in :attr:`Project.metadata` so
it serializes alongside the rest of the IR.
"""

from eks_harness.video import (
    AudioFile,
    AudioSegment,
    AudioTrack,
    EveryNth,
    MarkerRef,
    Project,
    SceneMarkers,
    Seconds,
    Segment,
    Track,
    VideoFile,
)

_DURATION = 36.0

_switch_trigger = EveryNth(
    source=MarkerRef(name="scenes"),
    n=2,
    offset=0,
    seed=11,
)


def _camera_track(index: int, z: int) -> Track:
    return Track(
        name=f"cam_{index}",
        z=z,
        segments=[
            Segment(
                id=f"cam_{index}_take",
                start=Seconds(t=0.0),
                media=VideoFile(path=f"assets/cam_{index}.mp4"),
                in_=Seconds(t=0.0),
                out=Seconds(t=_DURATION),
            ),
        ],
    )


project = Project(
    fps=30,
    resolution=(1920, 1080),
    duration=_DURATION,
    markers=[
        SceneMarkers(name="scenes", source="audio_tracks[0]", threshold=27.0),
    ],
    tracks=[
        _camera_track(0, z=0),
        _camera_track(1, z=1),
        _camera_track(2, z=2),
    ],
    audio_tracks=[
        AudioTrack(
            name="floor_mic",
            segments=[
                AudioSegment(
                    id="floor",
                    start=Seconds(t=0.0),
                    media=AudioFile(path="assets/floor_mic.wav"),
                    in_=Seconds(t=0.0),
                    out=Seconds(t=_DURATION),
                ),
            ],
        ),
    ],
    metadata={"switch_trigger": _switch_trigger},
)


if __name__ == "__main__":
    from pathlib import Path

    Path("./out").mkdir(exist_ok=True)
    Path("./out/project.json").write_text(
        project.model_dump_json(by_alias=True, indent=2),
        encoding="utf-8",
    )

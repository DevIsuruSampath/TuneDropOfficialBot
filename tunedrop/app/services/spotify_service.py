from __future__ import annotations

from dataclasses import dataclass

from tunedrop.app.utils.validators import InputType


@dataclass(slots=True)
class SpotifyTarget:
    source: str
    input_type: InputType

    @property
    def is_playlist(self) -> bool:
        return self.input_type == InputType.SPOTIFY_PLAYLIST


def build_spotify_target(source: str, input_type: InputType) -> SpotifyTarget:
    return SpotifyTarget(source=source, input_type=input_type)

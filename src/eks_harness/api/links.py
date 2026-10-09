from __future__ import annotations

from urllib.parse import quote

from eks_harness.config import Config


class Links:
    def __init__(self, config: Config) -> None:
        self.config = config

    @property
    def base(self) -> str:
        return self.config.public_url().rstrip("/")

    def absolute(self, path: str) -> str:
        return self.base + (path if path.startswith("/") else "/" + path)

    def project(self, project_id: str) -> str:
        owner, _, name = project_id.partition("/")
        return self.absolute(f"/p/{quote(owner)}/{quote(name)}")

    def session(self, project_id: str, slug: str | None) -> str:
        if not slug:
            return self.project(project_id)
        return self.project(project_id) + f"/s/{quote(slug, safe='')}"

    def sessions(self) -> str:
        return self.absolute("/sessions")

    def shared_session(self, slug: str) -> str:
        return self.absolute(f"/sessions/{quote(slug, safe='')}")

    def artifact(self, project_id: str, slug: str | None, artifact_id: str) -> str:
        return self.session(project_id, slug or "_project") + f"/a/{artifact_id}"

    def raw(self, artifact_id: str, filename: str) -> str:
        return self.absolute(f"/raw/{artifact_id}/{quote(filename, safe='')}")

    def download(self, artifact_id: str, filename: str) -> str:
        return self.raw(artifact_id, filename) + "?download=1"

    def thumbnail(self, artifact_id: str) -> str:
        return self.absolute(f"/thumb/{artifact_id}.jpg")

    def site(self, artifact_id: str, entry: str = "index.html") -> str:
        return self.absolute(f"/site/{artifact_id}/{quote(entry, safe='/')}")

    def shared(self, path: str) -> str:
        return self.config.share_url().rstrip("/") + (path if path.startswith("/") else "/" + path)

    def share(self, token: str) -> str:
        return self.shared(f"/s/{token}")

    def share_raw(self, token: str) -> str:
        return self.shared(f"/s/{token}/raw")

    def share_direct(self, token: str, path: str) -> str:
        return self.shared(f"/d/{token}/{quote(path, safe='/')}")

    def device(self, kind: str, index: int) -> str:
        return self.absolute(f"/devices/{kind}/{index}")

    def device_live(self, kind: str, index: int) -> str:
        return self.absolute(f"/api/devices/{kind}/{index}/live")

    def profile(self, profile_id: str) -> str:
        return self.absolute(f"/browsers/profiles/{quote(profile_id, safe='')}")

    def profile_live(self, profile_id: str) -> str:
        return self.absolute(f"/api/profiles/{quote(profile_id, safe='')}/live")

    def ui(self) -> str:
        return self.absolute("/")
